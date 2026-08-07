"""
holdings.py — full ETF holdings, per issuer, with an as-of date.

This is the heart of the backend: given a ticker it returns **100% of the
fund's holdings** using the most efficient method per issuer, tagged with the
date the issuer published them and a `is_stale` flag for quarterly (N-PORT)
sources.

    get_holdings("IVV") -> HoldingsResult(
        ticker="IVV", as_of="2026-07-31", is_stale=False,
        source="ishares", holdings=[Holding("AAPL", "APPLE INC", 0.0705), ...])

Design notes
------------
* **Stdlib only.** Parsing uses csv / json / xml / urllib — no pandas — so the
  core function and its tests run in a bare virtualenv with zero installs. The
  FastAPI web layer (app.py) is the only thing that needs extra packages.
* **Testable without the backend.** Every issuer fetch is split into a *raw*
  step (fetch bytes) and a *parse* step. In offline mode the raw step reads a
  bundled fixture from ``fixtures/`` instead of hitting the network, so the real
  parsers and routing are exercised end-to-end with no server and no internet.
  Turn it on with ``get_holdings(ticker, offline=True)`` or ``OFFLINE=1``.
* **# VERIFY** tags mark the handful of live URL patterns to confirm on your
  first real run (issuer sites occasionally move these).

Run it directly as the "ticker in, holdings out" tool:

    python holdings.py IVV            # live (needs network)
    python holdings.py IVV --offline  # from bundled fixtures, no network
    python holdings.py QQQ --offline --limit 15
"""

from __future__ import annotations

import csv
import io
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path

from ratelimit import polite_get
import issuer_catalog

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi is optional; fall back to the system default.
    _SSL_CONTEXT = ssl.create_default_context()

BASE_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BASE_DIR / "fixtures"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (etf-backend; +holdings)"}
# Some issuer CDNs reject non-browser clients; use a fuller header set for those.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "text/csv,application/csv,application/vnd.ms-excel,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Holding:
    ticker: str          # holding ticker (may be blank for unlisted names)
    name: str
    weight: float        # decimal fraction of the fund (0.0705 == 7.05%)

    def as_dict(self):
        return {"ticker": self.ticker, "name": self.name,
                "weight": round(self.weight, 8)}


@dataclass
class HoldingsResult:
    ticker: str
    as_of: str                       # ISO date the issuer published the file
    source: str                      # ishares | spdr | invesco | vanguard | nport
    is_stale: bool                   # True for quarterly (N-PORT) sources
    holdings: list = field(default_factory=list)
    # Disclosure caveats the caller MUST surface — e.g. a semi-transparent
    # active ETF whose published file is a proxy portfolio, not real holdings.
    warnings: list = field(default_factory=list)
    # Why the winning source won: every issuer tried and how it failed. Without
    # this a WAF 403 on the daily feed looks identical to a fund that simply has
    # no daily file — you silently get quarterly data and never know why.
    attempts: list = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """True when we wanted a daily feed and settled for quarterly N-PORT."""
        return self.source == "nport" and any(
            not a.startswith("nport") for a in self.attempts)

    @property
    def count(self):
        return len(self.holdings)

    @property
    def total_weight(self):
        return sum(h.weight for h in self.holdings)

    def as_dict(self):
        d = asdict(self)
        d["holdings"] = [h.as_dict() for h in self.holdings]
        d["count"] = self.count
        d["total_weight"] = round(self.total_weight, 6)
        d["degraded"] = self.degraded
        return d


class HoldingsError(Exception):
    """Raised when holdings can't be fetched/parsed for a ticker."""


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _env_offline() -> bool:
    return os.environ.get("OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def _http_get(url: str, timeout: int = 60, headers: dict | None = None) -> bytes:
    """GET a URL and return the raw bytes. Raises on failure.

    Every issuer request funnels through here, so this is where per-host pacing
    belongs: ratelimit.polite_get blocks until the host's token bucket allows
    the call, then backs off on 429/503 honoring Retry-After. iShares is capped
    at 8/60s (their ceiling is 10) so a retry can't tip us over.
    """
    hdrs = dict(HTTP_HEADERS)
    if headers:
        hdrs.update(headers)
    return polite_get(url, timeout=timeout, headers=hdrs)


def _fixture_bytes(name: str) -> bytes:
    path = FIXTURES_DIR / name
    if not path.exists():
        raise HoldingsError(
            f"offline fixture '{name}' not found under {FIXTURES_DIR}. "
            f"Add one, or run without --offline for a live fetch."
        )
    return path.read_bytes()


def _to_decimal_weight(value) -> float:
    """Coerce a weight cell to a float. Percentages (>1) are normalized later in
    one pass so mixed rows can't be half-converted."""
    if value is None:
        return 0.0
    s = str(value).strip().replace("%", "").replace(",", "")
    if not s or s.upper() in ("NA", "N/A", "-", "--"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _normalize_weights(holdings: list) -> list:
    """If the largest weight looks like a percentage (>1), divide the whole set
    by 100 so weights are always decimal fractions."""
    if holdings and max(h.weight for h in holdings) > 1.0:
        for h in holdings:
            h.weight /= 100.0
    return holdings


def _finalize(holdings: list) -> list:
    """Drop empty/zero rows, normalize to decimals, sort by weight desc."""
    holdings = [h for h in holdings if h.weight and (h.ticker or h.name)]
    holdings = _normalize_weights(holdings)
    holdings.sort(key=lambda h: h.weight, reverse=True)
    return holdings


def _parse_date(text: str) -> str:
    """Best-effort parse of an issuer's as-of string into an ISO date.
    Accepts 'Jul 31, 2026', '31-Jul-2026', '2026-07-31', '07/31/2026',
    'As of 03-Aug-2026', and ISO datetimes like '2026-06-30T00:00:00-04:00'."""
    text = (text or "").strip().strip('"')
    if text.lower().startswith("as of"):
        text = text[5:].strip(" :")
    if "T" in text and text[:4].isdigit():        # ISO datetime -> date part
        text = text.split("T", 1)[0]
    fmts = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%B-%Y",
            "%m/%d/%Y", "%Y%m%d")
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text  # leave as-is if unrecognized; caller still gets *something*


# --------------------------------------------------------------------------- #
# Issuer routing
# --------------------------------------------------------------------------- #
# These sets are only a *first guess* at which issuer owns a ticker — they make
# the common case a single request. They do NOT limit coverage: get_holdings()
# cascades through every daily-feed issuer for any ticker (see _candidate_order),
# and three of the four feeds are addressed purely by ticker in the URL, so *any*
# ETF from SPDR / Invesco / Vanguard resolves without being pre-registered here.
# iShares needs a product-id lookup (resolved dynamically below); anything no
# daily feed covers falls through to the quarterly N-PORT fallback (flagged stale).
DAILY_ISSUERS = ("vanguard", "ishares", "invesco", "spdr")

ISHARES_TICKERS = {"IVV", "IJH", "IJR", "ITOT", "IWV", "IWB", "IWM", "AGG",
                   "IEFA", "IEMG", "IWF", "IWD", "IVW", "IVE", "IUSB", "IXUS",
                   "IJK", "IJJ", "IJS", "IJT", "IWN", "IWO", "IWP", "IWS"}
SPDR_TICKERS    = {"SPY", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI",
                   "XLB", "XLU", "XLRE", "XLC", "MDY", "SPYG", "SPYV", "SPSM",
                   "SPMD", "DIA", "XBI", "XOP", "XHB", "XRT", "KRE"}
INVESCO_TICKERS = {"QQQ", "QQQM", "RSP", "SPHQ", "SPLV", "SPHD", "PDP", "QQQJ",
                   "RPG", "RPV", "XLG"}
VANGUARD_TICKERS = {"VOO", "VTI", "VEA", "VXUS", "VUG", "VTV", "VO", "VB",
                    "VIG", "VYM", "BND", "VGT", "VHT", "VNQ", "VWO", "VEU",
                    "VBR", "VBK", "VOE", "VOT", "MGK", "VV", "VYMI"}

# --- passive shops added via the product-catalog route ---------------------- #
# SCHD is in the default TRACKED_TICKERS and was previously falling all the way
# through to quarterly N-PORT; Schwab publishes daily.
SCHWAB_TICKERS  = {"SCHD", "SCHX", "SCHB", "SCHG", "SCHF", "SCHE", "SCHA",
                   "SCHV", "SCHM", "SCHH", "SCHP", "SCHR", "SCHZ", "SCHI",
                   "SCHJ", "SCHK", "SCHY", "SCHQ", "FNDX", "FNDA", "FNDF"}
VANECK_TICKERS  = {"SMH", "GDX", "GDXJ", "MOAT", "PPH", "OIH", "ESPO", "BBH",
                   "REMX", "SMOG", "IGRA", "MOTI", "AMLP"}
GLOBALX_TICKERS = {"LIT", "URA", "BOTZ", "SNSR", "QYLD", "XYLD", "RYLD",
                   "COPX", "SIL", "PAVE", "AIQ", "DRIV", "CLOU"}
WISDOMTREE_TICKERS = {"DGRW", "DXJ", "HEDJ", "DES", "DLN", "EPS", "DFE",
                      "DEM", "DGS", "USFR", "AGGY", "WTV"}
FIRSTTRUST_TICKERS = {"FDN", "FXL", "FXH", "FTCS", "FTSM", "SKYY", "CIBR",
                      "QCLN", "GRID", "FPX", "FDL", "FVD", "FTGC", "RDVY"}

# --- active shops ------------------------------------------------------------ #
# Fully transparent active ETFs: real holdings, published daily. Their sibling
# MUTUAL funds are quarterly N-PORT only — a completely different data contract.
CAPGROUP_TICKERS = {"CGGR", "CGDV", "CGUS", "CGXU", "CGGO", "CGIE", "CGCP",
                    "CGMS", "CGSM", "CGCB", "CGIB", "CGNG", "CGHM", "CGVE",
                    "CGBL", "CGDG", "CGRO", "CGW"}
# T. Rowe Price: TCAF is transparent; the semi-transparent ones publish a PROXY
# portfolio and are flagged at fetch time (see issuer_catalog.ACTIVE_DISCLOSURE).
TROWE_TICKERS = {"TCAF", "TCHP", "TDVG", "TEQI", "TGRT", "TSPA", "TSEC", "THYF"}

CATALOG_ISSUERS = {
    "schwab": SCHWAB_TICKERS, "vaneck": VANECK_TICKERS,
    "globalx": GLOBALX_TICKERS, "wisdomtree": WISDOMTREE_TICKERS,
    "firsttrust": FIRSTTRUST_TICKERS, "capitalgroup": CAPGROUP_TICKERS,
    "troweprice": TROWE_TICKERS,
}

# iShares needs a numeric product id per fund to build the daily CSV URL. This is
# a *seed* map for the most common funds; resolve_ishares_pid() falls back to the
# live iShares product screener (cached to .ishares_map.json) for everything else,
# so any iShares ETF resolves once the screener is confirmed.  # VERIFY
ISHARES_PRODUCT_IDS = {
    "IVV": "239726", "IJH": "239763", "IJR": "239774", "ITOT": "239724",
    "IWV": "239714", "IWB": "239707", "IWM": "239710", "AGG": "239458",
    "IEFA": "244049", "IEMG": "244050",
}

# Live catalog of every US iShares fund (ticker -> product id). The exact URL is
# the one thing to confirm on your machine; the parser below tolerates the two
# shapes the screener has shipped.  # VERIFY
ISHARES_SCREENER_URL = (
    "https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn"
    "?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/"
    "ishares-product-screener-backend-config&siteEntPassthrough=true"
)
_ISHARES_MAP_CACHE = BASE_DIR / ".ishares_map.json"


def classify_issuer(ticker: str) -> str:
    """First-guess issuer for a ticker (used to order the cascade). Returns one
    of the daily issuers if the ticker is pre-registered, else 'nport'."""
    t = ticker.upper()
    if t in ISHARES_TICKERS:
        return "ishares"
    if t in SPDR_TICKERS:
        return "spdr"
    if t in INVESCO_TICKERS:
        return "invesco"
    if t in VANGUARD_TICKERS:
        return "vanguard"
    for issuer, tickers in CATALOG_ISSUERS.items():
        if t in tickers:
            return issuer
    return "nport"          # universal fallback for every other issuer


def _candidate_order(ticker: str, offline: bool) -> list:
    """The ordered list of issuer methods to try for a ticker.

    Offline is fixture-bound (one file per issuer), so we try only the known
    route — deterministic for tests. Live, we lead with the best guess, then try
    the other daily feeds (all safe: SPDR/Invesco/Vanguard are addressed by
    ticker; a wrong guess just returns nothing and we move on), and finally the
    N-PORT fallback so nothing dead-ends."""
    guess = classify_issuer(ticker)
    if offline:
        # Fixture-bound and deterministic. The catalog issuers ship no fixtures,
        # so they also allow the N-PORT fixture route rather than dead-ending
        # (this keeps SCHD et al. resolvable offline).
        return [guess, "nport"] if guess in CATALOG_ISSUERS else [guess]
    # Every daily feed stays in the cascade: a wrong first guess must still
    # resolve. Requests are NOT wasted, because each catalog-backed fetcher
    # short-circuits without a network call when its cached catalog positively
    # says it doesn't list the ticker (see _catalog_lists / fetch_ishares).
    # Only issuers marked verified=true in issuer_endpoints.json enter the live
    # cascade. An unconfirmed URL would otherwise add a guaranteed-failing hop
    # to EVERY lookup — which is exactly what diagnose.py caught.
    live_catalog = [i for i in CATALOG_ISSUERS
                    if i in issuer_catalog.verified_issuers()]
    if guess == "nport":
        return list(DAILY_ISSUERS) + live_catalog + ["nport"]
    if guess in CATALOG_ISSUERS and guess not in live_catalog:
        # Classified to an unverified issuer: skip it, use the daily feeds.
        return list(DAILY_ISSUERS) + ["nport"]
    return ([guess] + [i for i in DAILY_ISSUERS if i != guess]
            + [i for i in live_catalog if i != guess] + ["nport"])


def _load_ishares_map() -> dict:
    """Seed map merged over the cached screener catalog (uppercased tickers)."""
    m = {k.upper(): str(v) for k, v in ISHARES_PRODUCT_IDS.items()}
    if _ISHARES_MAP_CACHE.exists():
        try:
            cached = json.loads(_ISHARES_MAP_CACHE.read_text())
            m.update({k.upper(): str(v) for k, v in cached.items()})
        except Exception:
            pass
    return m


def resolve_ishares_pid(ticker: str, offline: bool):
    """iShares ticker -> product id. Checks the seed+cache first; on a live miss,
    fetches the full iShares screener, caches ticker->pid, and retries. Returns
    None if it can't be resolved (the caller then falls through to N-PORT)."""
    ticker = ticker.upper()
    m = _load_ishares_map()
    if ticker in m:
        return m[ticker]
    if offline:
        return None
    # One rate-limited screener call per CATALOG_TTL_HOURS, shared across every
    # ticker, instead of a fetch per cache miss. This is the fix for the
    # 10-req/60s ceiling: N discovery calls collapse to 1.
    pid = issuer_catalog.resolve("ishares", ticker)
    if pid:
        return pid
    print(f"Note: {ticker} not in the iShares catalog; falling through to N-PORT.")
    return None


def _parse_ishares_screener(data) -> dict:
    """Build {ticker: product_id} from the screener JSON. The payload has shipped
    two shapes; we tolerate both and skip anything we can't read.  # VERIFY"""
    out = {}
    if not isinstance(data, dict):
        return out

    def _cell(v):
        # Fields are sometimes scalars, sometimes {"r": <value>, "d": <display>}.
        if isinstance(v, dict):
            return v.get("r", v.get("d", ""))
        return v

    for pid, rec in data.items():
        if not isinstance(rec, dict):
            continue
        tick = str(_cell(rec.get("localExchangeTicker"))
                   or _cell(rec.get("fundTicker")) or "").strip().upper()
        # Some shapes nest the id in the record instead of using the key.
        real_pid = str(_cell(rec.get("productPageUrl")) or pid).rstrip("/").split("/")[-1]
        real_pid = real_pid if real_pid.isdigit() else str(pid)
        if tick and real_pid.isdigit():
            out[tick] = real_pid
    return out


# --------------------------------------------------------------------------- #
# Parsers  (pure functions: raw text/bytes -> (as_of, [Holding]))
# Kept separate from fetching so they're trivially unit-testable offline.
# --------------------------------------------------------------------------- #
def parse_ishares_csv(text: str) -> tuple[str, list]:
    """iShares daily holdings CSV: a metadata preamble (incl. a 'Fund Holdings
    as of' line) then a blank line, then the real header row containing
    'Ticker' and 'Weight (%)'."""
    lines = text.splitlines()
    as_of = ""
    header_idx = None
    for i, line in enumerate(lines):
        low = line.lower()
        if "holdings as of" in low or (as_of == "" and "as of" in low and "," in line):
            # e.g.  Fund Holdings as of,"Jul 31, 2026"
            parts = next(csv.reader([line]))
            if len(parts) >= 2:
                as_of = _parse_date(parts[1])
        if "ticker" in low and "weight" in low:
            header_idx = i
            break
    if header_idx is None:
        raise HoldingsError("iShares CSV: could not locate the holdings header row")

    reader = csv.DictReader(lines[header_idx:])
    tcol = _find_col(reader.fieldnames, ("ticker",))
    ncol = _find_col(reader.fieldnames, ("name",))
    wcol = _find_col(reader.fieldnames, ("weight (%)", "weight", "% of net assets"))
    holdings = []
    for row in reader:
        holdings.append(Holding(
            ticker=(row.get(tcol) or "").strip().upper(),
            name=(row.get(ncol) or "").strip(),
            weight=_to_decimal_weight(row.get(wcol)),
        ))
    return as_of, holdings


def _row_text(row: dict, col) -> str:
    """Safely read a DictReader cell.

    Two traps: (1) if `col` is None (column never matched), row.get(None)
    returns csv's *restkey* — a LIST of overflow values — and .strip() on it
    raises AttributeError; (2) a short/ragged row yields None. Both are common
    when an issuer serves an error page instead of the expected CSV.
    """
    if col is None:
        return ""
    val = row.get(col)
    if isinstance(val, list):
        val = val[0] if val else ""
    return str(val or "").strip()


def parse_invesco_csv(text: str) -> tuple[str, list]:
    """Invesco daily holdings CSV: a flat table with a Holding Ticker, Name,
    Weight and a per-row Date (used as the as-of)."""
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    tcol = _find_col(fields, ("holding ticker", "ticker", "symbol"))
    ncol = _find_col(fields, ("name", "security name", "description"))
    wcol = _find_col(fields, ("weight", "weighting", "% weight"))
    dcol = _find_col(fields, ("date", "as of date", "holdings date"))
    # Fail fast and cleanly rather than parsing garbage: if neither an
    # identifier nor a weight column is present, this isn't a holdings CSV
    # (usually an HTML error page). The cascade then moves on properly.
    if tcol is None and ncol is None:
        raise HoldingsError(
            "Invesco response has no ticker/name column — not a holdings CSV "
            f"(headers seen: {fields[:6]})")
    as_of, holdings = "", []
    for row in reader:
        if dcol and not as_of:
            as_of = _parse_date(_row_text(row, dcol))
        holdings.append(Holding(
            ticker=_row_text(row, tcol).upper(),
            name=_row_text(row, ncol),
            weight=_to_decimal_weight(_row_text(row, wcol) or None),
        ))
    return as_of, holdings


def parse_vanguard_json(raw: bytes) -> tuple[str, list]:
    """Vanguard's public portfolio-holding API returns every equity constituent
    under fund.entity, each with a percentWeight. as-of is on the payload."""
    data = json.loads(raw)
    fund = data.get("fund") or {}
    entities = fund.get("entity") or []
    as_of = _parse_date(data.get("asOfDate") or fund.get("asOfDate") or "")
    holdings = []
    for e in entities:
        holdings.append(Holding(
            ticker=str(e.get("ticker") or "").strip().upper(),
            name=str(e.get("longName") or e.get("shortName") or "").strip(),
            weight=_to_decimal_weight(e.get("percentWeight")),
        ))
    return as_of, holdings


def _find_col(fieldnames, aliases):
    """Case-insensitive column match: exact first, then substring."""
    if not fieldnames:
        return None
    lowered = {(c or "").lower().strip(): c for c in fieldnames}
    for a in aliases:
        if a in lowered:
            return lowered[a]
    for a in aliases:
        for low, orig in lowered.items():
            if a in low:
                return orig
    return None


# --------------------------------------------------------------------------- #
# Fetchers  (raw step: offline reads a fixture, live hits the issuer)
# --------------------------------------------------------------------------- #
def fetch_ishares(ticker: str, offline: bool) -> HoldingsResult:
    if offline:
        text = _fixture_bytes(f"{ticker.upper()}.ishares.csv").decode("utf-8", "replace")
    else:
        # iShares is the strict-WAF host (8/60s). If the cached catalog proves
        # this isn't an iShares fund, decline before spending a token.
        if _catalog_lists("ishares", ticker) is False and \
                ticker.upper() not in ISHARES_PRODUCT_IDS:
            raise HoldingsError(
                f"{ticker.upper()} is not in the cached iShares catalog "
                f"(skipped without a request)")
        pid = resolve_ishares_pid(ticker, offline)
        if not pid:
            raise HoldingsError(
                f"iShares product id for {ticker} could not be resolved (not in "
                f"the seed map and the screener lookup didn't return it)."
            )
        # Use the fund's REAL page path (with slug) from the screener. The old
        # hardcoded "/fund/" segment is kept as a fallback pattern, but it is
        # the likely cause of the HTML-instead-of-CSV response.
        path = issuer_catalog.resolve_path("ishares", ticker) or ""
        text, pattern = fetch_with_pattern_discovery(
            "ishares", ticker, ISHARES_URL_PATTERNS,
            {"pid": pid, "ticker": ticker.upper(), "path": path})
        print(f"  iShares {ticker}: pattern {ISHARES_URL_PATTERNS.index(pattern) + 1} "
              f"of {len(ISHARES_URL_PATTERNS)} worked")
    as_of, holdings = parse_ishares_csv(text)
    return HoldingsResult(ticker.upper(), as_of, "ishares", False, _finalize(holdings))


def fetch_invesco(ticker: str, offline: bool) -> HoldingsResult:
    if offline:
        text = _fixture_bytes(f"{ticker.upper()}.invesco.csv").decode("utf-8", "replace")
    else:
        # # VERIFY: Invesco daily holdings CSV. Currently returns HTTP 406 to
        # non-browser clients; if it keeps rejecting, the cascade falls through to
        # N-PORT. Browser headers are our best-effort to get the CSV.
        url = ("https://www.invesco.com/us/financial-products/etfs/holdings/main/"
               "holdings/0?audienceType=Investor&action=download&ticker="
               + urllib.parse.quote(ticker))
        text = _http_get(url, headers=_BROWSER_HEADERS).decode("utf-8", "replace")
    as_of, holdings = parse_invesco_csv(text)
    return HoldingsResult(ticker.upper(), as_of, "invesco", False, _finalize(holdings))


def parse_spdr_rows(rows: list) -> tuple[str, list]:
    """Parse SPDR holdings from a list of spreadsheet rows (each a list of cell
    values). The real file is: a few metadata rows (one is 'Holdings:', 'As of
    <date>'), a blank row, a header row (Name/Ticker/Identifier/SEDOL/Weight/...),
    then the holdings. The CSV fixture has the same layout, so this serves both."""
    as_of = ""
    header_idx = None
    for i, r in enumerate(rows):
        cells = [str(c).strip() if c is not None else "" for c in r]
        joined = " ".join(cells).lower()
        if not as_of and "as of" in joined:
            for c in cells:
                if "as of" in c.lower() or any(ch.isdigit() for ch in c):
                    d = _parse_date(c)
                    if d and d[:4].isdigit():
                        as_of = d
                        break
        low = [c.lower() for c in cells]
        if "ticker" in low and ("weight" in low or "name" in low):
            header_idx = i
            header = cells
            break
    if header_idx is None:
        raise HoldingsError("SPDR table: could not locate the holdings header row")

    col = {name.lower(): j for j, name in enumerate(header)}
    ti = col.get("ticker")
    ni = col.get("name")
    wi = col.get("weight", col.get("weight (%)"))
    holdings = []
    for r in rows[header_idx + 1:]:
        cells = list(r)
        def cell(idx):
            return cells[idx] if idx is not None and idx < len(cells) else None
        tk = str(cell(ti) or "").strip().upper()
        nm = str(cell(ni) or "").strip()
        if not tk and not nm:
            continue
        holdings.append(Holding(tk, nm, _to_decimal_weight(cell(wi))))
    return as_of, holdings


def fetch_spdr(ticker: str, offline: bool) -> HoldingsResult:
    if offline:
        text = _fixture_bytes(f"{ticker.upper()}.spdr.csv").decode("utf-8", "replace")
        rows = list(csv.reader(io.StringIO(text)))
    else:
        # SPDR/State Street daily XLSX. urllib follows the 301 to the CDN host
        # automatically; we parse with openpyxl (lighter than pandas).  # VERIFY
        url = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
               f"fund-data/etfs/us/holdings-daily-us-en-{ticker.lower()}.xlsx")
        try:
            import openpyxl
        except ImportError as e:
            raise HoldingsError("SPDR holdings need openpyxl (pip install openpyxl).") from e
        raw = _http_get(url, headers=_BROWSER_HEADERS)
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        rows = [[c.value for c in row] for row in wb.active.iter_rows()]
    as_of, holdings = parse_spdr_rows(rows)
    return HoldingsResult(ticker.upper(), as_of, "spdr", False, _finalize(holdings))


def fetch_vanguard(ticker: str, offline: bool) -> HoldingsResult:
    if offline:
        raw = _fixture_bytes(f"{ticker.upper()}.vanguard.json")
    else:
        # # VERIFY: Vanguard public portfolio-holding API (full equity holdings).
        url = ("https://investor.vanguard.com/investment-products/etfs/profile/"
               f"api/{ticker.lower()}/portfolio-holding/stock?start=1&count=20000")
        raw = _http_get(url)
    as_of, holdings = parse_vanguard_json(raw)
    return HoldingsResult(ticker.upper(), as_of, "vanguard", False, _finalize(holdings))


def fetch_nport(ticker: str, offline: bool) -> HoldingsResult:
    """Universal fallback for any issuer without a clean daily feed (Schwab,
    Vanguard bond funds, everything unknown). Sourced from the fund's latest SEC
    N-PORT filing, so quarterly and flagged stale.

    N-PORT identifies holdings by name + CUSIP/ISIN, not ticker, so we enrich to
    tickers via OpenFIGI (skipped offline / when it can't map a security)."""
    from nport_source import nport_fetch   # local import to avoid a hard cycle
    from figi import enrich_tickers

    as_of, rows = nport_fetch(ticker, offline=offline, fixtures_dir=FIXTURES_DIR)
    if not offline:
        enrich_tickers(rows)                # fills row["ticker"] in place, best-effort
    holdings = [Holding(str(r.get("ticker") or "").upper(),
                        str(r.get("name") or "").strip(),
                        _to_decimal_weight(r.get("weight")))
                for r in rows]
    return HoldingsResult(ticker.upper(), as_of, "nport", True, _finalize(holdings))


# --------------------------------------------------------------------------- #
# Catalog-driven issuers (Schwab, VanEck, Global X, WisdomTree, First Trust,
# Capital Group, T. Rowe Price)
#
# All share one shape: resolve ticker -> product id from the cached catalog
# (one rate-limited call per issuer per day), then pull that fund's daily
# holdings file. Failure is non-fatal by design — the cascade in get_holdings
# falls through to N-PORT, so a wrong endpoint degrades to quarterly data
# rather than breaking the ticker.
#
# # VERIFY — confirm each holdings URL template against live traffic (devtools
# -> Network -> XHR on the fund's holdings tab) before trusting it.
# --------------------------------------------------------------------------- #
CATALOG_HOLDINGS_URLS = {
    "schwab":       "https://www.schwabassetmanagement.com/api/fund/{pid}/holdings?format=csv",
    "vaneck":       "https://www.vaneck.com/api/products/us/etf/{pid}/holdings/?format=csv",
    "globalx":      "https://www.globalxetfs.com/api/funds/{pid}/holdings.csv",
    "wisdomtree":   "https://www.wisdomtree.com/api/etfs/{pid}/holdings.csv",
    "firsttrust":   "https://www.ftportfolios.com/api/products/etf/{pid}/holdings.csv",
    "capitalgroup": "https://www.capitalgroup.com/api/etf/{pid}/holdings?format=csv",
    "troweprice":   "https://www.troweprice.com/api/products/etf/{pid}/holdings.csv",
}


def _disclosure_warnings(issuer: str, ticker: str) -> list:
    """Caveats that must travel with the data, not sit in a README."""
    warns = []
    if ticker.upper() in issuer_catalog.SEMI_TRANSPARENT_TICKERS:
        warns.append(
            f"{ticker.upper()} is a SEMI-TRANSPARENT active ETF: the published "
            f"file is a PROXY portfolio, not the fund's actual holdings. Do not "
            f"present it as real look-through exposure.")
    note = issuer_catalog.ACTIVE_DISCLOSURE.get(issuer)
    if note and ticker.upper() not in issuer_catalog.SEMI_TRANSPARENT_TICKERS:
        warns.append(note[2])
    return warns


# --------------------------------------------------------------------------- #
# URL pattern auto-discovery
#
# The endpoint problem is that issuers change URL shapes and nobody can verify
# them all by hand. So rather than betting on one hardcoded template, try a
# short ranked list, validate that the RESPONSE IS ACTUALLY A HOLDINGS CSV
# (not an HTML challenge or a redirect), and remember which pattern won so the
# next call goes straight to it.
#
# Pattern 1 for iShares uses the real product page path (with slug) taken from
# the screener. That is the most likely correct form and the one the previous
# hardcoded "/fund/" guess got wrong.
# --------------------------------------------------------------------------- #
ISHARES_URL_PATTERNS = [
    # Real page path from the catalog + the .ajax data endpoint.
    "https://www.ishares.com{path}/1467271812596.ajax"
    "?fileType=csv&fileName={ticker}_holdings&dataType=fund",
    # Legacy literal-"fund" segment (what we had before).
    "https://www.ishares.com/us/products/{pid}/fund/1467271812596.ajax"
    "?fileType=csv&fileName={ticker}_holdings&dataType=fund",
    # Some funds expose the file under a plain download path.
    "https://www.ishares.com{path}/1467271812596.ajax"
    "?fileType=csv&fileName={ticker}_holdings&dataType=fund&asOfDate=",
]


def _looks_like_holdings_csv(raw: bytes) -> bool:
    """Reject HTML challenge pages and empty bodies before we try to parse."""
    if not raw or len(raw) < 200:
        return False
    head = raw[:400].decode("utf-8", "replace").lower()
    if "<html" in head or "<!doctype" in head:
        return False
    # A holdings file names its columns somewhere near the top.
    return any(tok in head for tok in
               ("ticker", "name", "weight", "cusip", "isin", "sedol", "asset class"))


def _remembered_pattern_key(issuer: str) -> str:
    return f"urlpattern:{issuer}"


def fetch_with_pattern_discovery(issuer: str, ticker: str, patterns: list,
                                 subs: dict):
    """Try patterns until one returns a real CSV; remember the winner.

    Returns (text, pattern_used). Raises HoldingsError if none work, listing
    what each attempt actually returned so the failure is diagnosable.
    """
    try:
        from datastore import STORE as _DS
    except Exception:
        _DS = None

    ordered = list(patterns)
    if _DS is not None:
        hit = _DS.get_json("urlpattern", _remembered_pattern_key(issuer))
        if hit and hit.value in ordered:
            # Winner first; keep the rest as fallback in case it rotates.
            ordered.remove(hit.value)
            ordered.insert(0, hit.value)

    problems = []
    for pat in ordered:
        try:
            url = pat.format(**subs)
        except KeyError as e:
            problems.append(f"{pat[:48]}...: missing substitution {e}")
            continue
        if "{" in url or "//1467" in url:      # unfilled slot / empty path
            problems.append(f"{pat[:48]}...: incomplete URL")
            continue
        try:
            raw = _http_get(url, headers=_BROWSER_HEADERS)
        except Exception as e:
            problems.append(f"{url[:70]}...: {type(e).__name__}")
            continue
        if not _looks_like_holdings_csv(raw):
            problems.append(f"{url[:70]}...: not a holdings CSV "
                            f"(HTML challenge or empty)")
            continue
        if _DS is not None:
            try:
                _DS.put_json("urlpattern", _remembered_pattern_key(issuer), pat,
                             ttl_hours=168)
            except Exception:
                pass
        return raw.decode("utf-8", "replace"), pat

    raise HoldingsError(
        f"no working {issuer} holdings URL for {ticker}. Tried "
        f"{len(ordered)} patterns: " + " | ".join(problems[:3]))


def _catalog_lists(issuer: str, ticker: str):
    """Tri-state: True/False if a cached catalog positively answers, else None
    (unknown — no cache yet, so the caller must try the network).

    This is where the rate-limit saving lives. The cascade still *offers* every
    issuer, but a fetcher can decline instantly when the cached catalog proves
    the ticker isn't in that lineup — no request spent, no WAF budget burned.
    """
    cat = issuer_catalog.load_cached(issuer)
    if not cat:
        return None
    return ticker.upper() in cat


def _make_catalog_fetcher(issuer: str):
    def _fetch(ticker: str, offline: bool) -> HoldingsResult:
        t = ticker.upper()
        if offline:
            text = _fixture_bytes(f"{t}.{issuer}.csv").decode("utf-8", "replace")
        else:
            if _catalog_lists(issuer, t) is False:
                raise HoldingsError(
                    f"{t} is not in the cached {issuer} catalog (skipped without "
                    f"a request)")
            pid = issuer_catalog.resolve(issuer, t)
            if not pid:
                raise HoldingsError(f"{t} not found in the {issuer} catalog")
            url = CATALOG_HOLDINGS_URLS[issuer].format(pid=pid)
            text = _http_get(url, headers=_BROWSER_HEADERS).decode("utf-8", "replace")
        # These feeds ship the same preamble+header CSV shape as iShares.
        as_of, holdings = parse_ishares_csv(text)
        if not holdings:
            raise HoldingsError(f"no holdings parsed for {t} from {issuer}")
        return HoldingsResult(t, as_of, issuer, False, _finalize(holdings),
                              _disclosure_warnings(issuer, t))
    _fetch.__name__ = f"fetch_{issuer}"
    return _fetch


ISSUER_DISPATCH = {
    "ishares": fetch_ishares,
    "spdr": fetch_spdr,
    "invesco": fetch_invesco,
    "vanguard": fetch_vanguard,
    "nport": fetch_nport,
    **{iss: _make_catalog_fetcher(iss) for iss in CATALOG_HOLDINGS_URLS},
}


# --------------------------------------------------------------------------- #
# The public entry point
# --------------------------------------------------------------------------- #
def get_holdings(ticker: str, offline: bool | None = None) -> HoldingsResult:
    """Return the full holdings for `ticker`, tagged with the issuer's as-of date.

    Works for *any* ETF from an accepted issuer (iShares, SPDR, Invesco,
    Vanguard), not just pre-registered tickers: it tries each daily feed in turn
    and returns the first that yields real holdings, then falls back to N-PORT.

    offline: None (default) reads the OFFLINE env var; True forces the bundled
    fixtures (no network); False forces a live fetch. Raises HoldingsError only
    if no method — daily feeds or N-PORT — could resolve the ticker.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise HoldingsError("ticker is required")
    if offline is None:
        offline = _env_offline()

    errors = []
    for issuer in _candidate_order(ticker, offline):
        try:
            result = ISSUER_DISPATCH[issuer](ticker, offline)
        except HoldingsError as e:
            errors.append(f"{issuer}: {e}")
            continue
        except Exception as e:                       # network / parse hiccup
            errors.append(f"{issuer}: {type(e).__name__}: {e}")
            continue
        if result.holdings:                          # first real hit wins
            if not result.as_of:
                # Never return an untagged set; fall back to today so downstream
                # joins and the UI always have an as-of (source flags freshness).
                result.as_of = date.today().isoformat()
            # Carry the trail of what failed onto the winner. A daily feed that
            # got WAF-blocked must not look like a fund that has no daily file.
            result.attempts = list(errors)
            if result.degraded:
                msg = (f"{ticker}: daily feed unavailable, served STALE "
                       f"quarterly N-PORT (as of {result.as_of}). Failures: "
                       + "; ".join(errors))
                result.warnings.append(msg)
                print(f"  \u26a0 {msg}")
            return result
        errors.append(f"{issuer}: no holdings parsed")

    raise HoldingsError(
        f"could not resolve holdings for {ticker} from any issuer. Tried: "
        + "; ".join(errors)
    )


# --------------------------------------------------------------------------- #
# CLI — "ticker in, full holdings + as-of out"
# --------------------------------------------------------------------------- #
def _print_result(res: HoldingsResult, limit: int | None, as_json: bool):
    if as_json:
        print(json.dumps(res.as_dict(), indent=2))
        return
    stale = "  ⚠ STALE (quarterly)" if res.is_stale else ""
    print(f"\n{res.ticker}  —  {res.count} holdings  —  as of {res.as_of}  "
          f"(source: {res.source}){stale}")
    print(f"  total weight covered: {res.total_weight * 100:.2f}%")
    print("-" * 64)
    shown = res.holdings if limit is None else res.holdings[:limit]
    print(f"  {'#':>3}  {'TICKER':<10} {'WEIGHT':>9}  NAME")
    for i, h in enumerate(shown, 1):
        print(f"  {i:>3}  {h.ticker:<10} {h.weight * 100:>8.4f}%  {h.name[:44]}")
    if limit is not None and res.count > limit:
        print(f"  … and {res.count - limit} more (use --limit 0 or --json for all)")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    ticker = argv[0]
    offline = "--offline" in argv or _env_offline()
    as_json = "--json" in argv
    limit = 25
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (ValueError, IndexError):
            print("--limit needs an integer (0 = all)"); return 2
    if limit == 0:
        limit = None
    try:
        res = get_holdings(ticker, offline=offline)
    except HoldingsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_result(res, limit, as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
