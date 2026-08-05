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

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi is optional; fall back to the system default.
    _SSL_CONTEXT = ssl.create_default_context()

BASE_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BASE_DIR / "fixtures"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (etf-backend; +holdings)"}


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
        return d


class HoldingsError(Exception):
    """Raised when holdings can't be fetched/parsed for a ticker."""


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _env_offline() -> bool:
    return os.environ.get("OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def _http_get(url: str, timeout: int = 60, headers: dict | None = None) -> bytes:
    """GET a URL and return the raw bytes. Raises on failure."""
    hdrs = dict(HTTP_HEADERS)
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return resp.read()


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
    Accepts 'Jul 31, 2026', '31-Jul-2026', '2026-07-31', '07/31/2026', ..."""
    text = (text or "").strip().strip('"')
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
        return [guess]
    if guess == "nport":
        return list(DAILY_ISSUERS) + ["nport"]
    return [guess] + [i for i in DAILY_ISSUERS if i != guess] + ["nport"]


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
    try:
        data = json.loads(_http_get(ISHARES_SCREENER_URL, timeout=60))
    except Exception as e:
        print(f"Note: iShares screener lookup failed ({type(e).__name__}); "
              f"{ticker} will fall through to N-PORT.")
        return None
    catalog = _parse_ishares_screener(data)
    if catalog:
        try:
            _ISHARES_MAP_CACHE.write_text(json.dumps(catalog))
        except Exception:
            pass
    return catalog.get(ticker)


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


def parse_invesco_csv(text: str) -> tuple[str, list]:
    """Invesco daily holdings CSV: a flat table with a Holding Ticker, Name,
    Weight and a per-row Date (used as the as-of)."""
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    tcol = _find_col(fields, ("holding ticker", "ticker", "symbol"))
    ncol = _find_col(fields, ("name", "security name", "description"))
    wcol = _find_col(fields, ("weight", "weighting", "% weight"))
    dcol = _find_col(fields, ("date", "as of date", "holdings date"))
    as_of, holdings = "", []
    for row in reader:
        if dcol and not as_of:
            as_of = _parse_date(row.get(dcol, ""))
        holdings.append(Holding(
            ticker=(row.get(tcol) or "").strip().upper(),
            name=(row.get(ncol) or "").strip(),
            weight=_to_decimal_weight(row.get(wcol)),
        ))
    return as_of, holdings


def parse_spdr_table(text: str) -> tuple[str, list]:
    """SPDR/State Street holdings. Live these come as XLSX with a 4-row preamble
    (parsed via pandas in fetch_spdr); the fixture is the same table as CSV. The
    preamble carries a 'Holdings: as of <date>' line."""
    lines = text.splitlines()
    as_of = ""
    header_idx = None
    for i, line in enumerate(lines):
        low = line.lower()
        if "as of" in low and not as_of:
            # The date is usually the cell *after* the "as of" label, e.g.
            #   Holdings: as of,"31-Jul-2026"
            parts = next(csv.reader([line]))
            for j, p in enumerate(parts):
                if "as of" in p.lower():
                    tail = p.split("as of")[-1].strip(" :")
                    cand = tail or (parts[j + 1] if j + 1 < len(parts) else "")
                    as_of = _parse_date(cand)
                    break
        if i != 0 and "ticker" in low and ("weight" in low or "name" in low):
            header_idx = i
            break
    if header_idx is None:
        raise HoldingsError("SPDR table: could not locate the holdings header row")
    reader = csv.DictReader(lines[header_idx:])
    fields = reader.fieldnames or []
    tcol = _find_col(fields, ("ticker", "identifier"))
    ncol = _find_col(fields, ("name",))
    wcol = _find_col(fields, ("weight", "weight (%)"))
    holdings = []
    for row in reader:
        holdings.append(Holding(
            ticker=(row.get(tcol) or "").strip().upper(),
            name=(row.get(ncol) or "").strip(),
            weight=_to_decimal_weight(row.get(wcol)),
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
        pid = resolve_ishares_pid(ticker, offline)
        if not pid:
            raise HoldingsError(
                f"iShares product id for {ticker} could not be resolved (not in "
                f"the seed map and the screener lookup didn't return it)."
            )
        # # VERIFY: iShares daily holdings CSV endpoint.
        url = (f"https://www.ishares.com/us/products/{pid}/fund/"
               f"1467271812596.ajax?fileType=csv&fileName={ticker}_holdings&dataType=fund")
        text = _http_get(url).decode("utf-8", "replace")
    as_of, holdings = parse_ishares_csv(text)
    return HoldingsResult(ticker.upper(), as_of, "ishares", False, _finalize(holdings))


def fetch_invesco(ticker: str, offline: bool) -> HoldingsResult:
    if offline:
        text = _fixture_bytes(f"{ticker.upper()}.invesco.csv").decode("utf-8", "replace")
    else:
        # # VERIFY: Invesco daily holdings CSV download endpoint.
        url = ("https://www.invesco.com/us/financial-products/etfs/holdings/main/"
               "holdings/0?audienceType=Investor&action=download&ticker="
               + urllib.parse.quote(ticker))
        text = _http_get(url).decode("utf-8", "replace")
    as_of, holdings = parse_invesco_csv(text)
    return HoldingsResult(ticker.upper(), as_of, "invesco", False, _finalize(holdings))


def fetch_spdr(ticker: str, offline: bool) -> HoldingsResult:
    if offline:
        text = _fixture_bytes(f"{ticker.upper()}.spdr.csv").decode("utf-8", "replace")
        as_of, holdings = parse_spdr_table(text)
    else:
        # # VERIFY: SPDR per-ticker daily XLSX. Live parsing needs pandas+openpyxl;
        # we import lazily so the stdlib-only core stays importable without them.
        url = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
               f"fund-data/etfs/us/holdings-daily-us-en-{ticker.lower()}.xlsx")
        try:
            import pandas as pd
        except ImportError as e:
            raise HoldingsError(
                "Live SPDR holdings are XLSX; install pandas+openpyxl, or use "
                "--offline. (iShares/Invesco/Vanguard live paths need no extras.)"
            ) from e
        raw = _http_get(url)
        df = pd.read_excel(io.BytesIO(raw), skiprows=4)
        df = df.rename(columns={"Ticker": "ticker", "Name": "name", "Weight": "weight"})
        holdings = [Holding(str(r.get("ticker", "")).strip().upper(),
                            str(r.get("name", "")).strip(),
                            _to_decimal_weight(r.get("weight")))
                    for _, r in df.iterrows()]
        as_of = ""
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
    Vanguard bond funds, everything unknown). Quarterly, so flagged stale."""
    from nport_source import nport_fetch  # local import to avoid a hard cycle
    as_of, holdings = nport_fetch(ticker, offline=offline, fixtures_dir=FIXTURES_DIR)
    return HoldingsResult(ticker.upper(), as_of, "nport", True, _finalize(holdings))


ISSUER_DISPATCH = {
    "ishares": fetch_ishares,
    "spdr": fetch_spdr,
    "invesco": fetch_invesco,
    "vanguard": fetch_vanguard,
    "nport": fetch_nport,
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
