"""
issuer_catalog.py — dynamic ticker -> product-id resolution, one call per issuer.

THE POINT: the fix for a 10-req/60s WAF budget is making fewer requests, not
sneaking extra ones through. Every issuer here exposes a *product finder* feed
that returns their entire fund lineup in a single response. We fetch that once,
cache it to disk with a TTL, and resolve every ticker from the cached catalog.

    Before: 40 iShares tickers -> up to 40 discovery calls -> blocked.
    After:  40 iShares tickers -> 1 catalog call -> 0 further discovery calls
            for CATALOG_TTL_HOURS (default 24).

Each entry in CATALOGS is (url, parser). Parsers are pure functions over the
decoded payload, so they unit-test offline against fixtures.

    python issuer_catalog.py            # offline self-test
    python issuer_catalog.py --refresh ishares   # live refresh one catalog

# VERIFY — endpoint URLs follow the repo's existing convention: the shapes below
# match what these product finders have shipped, but each one should be
# confirmed against live traffic (devtools -> Network -> XHR on the issuer's
# fund-list page) before you rely on it. Parsers are written defensively so an
# unexpected shape yields {} rather than an exception.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from ratelimit import polite_get

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".catalog_cache"
CATALOG_TTL_HOURS = float(os.environ.get("CATALOG_TTL_HOURS", "24"))


# --------------------------------------------------------------------------- #
# Parsers: payload -> {TICKER: product_id}
# --------------------------------------------------------------------------- #
def _cell(v):
    """Issuer feeds mix scalars with {"r": raw, "d": display} cells."""
    if isinstance(v, dict):
        return v.get("r", v.get("d", ""))
    return v


def _walk_records(data):
    """Yield dict records from the shapes these feeds actually ship:
    {id: {...}}, [ {...} ], or {"data"/"items"/"funds"/"results": [...]}."""
    if isinstance(data, dict):
        for key in ("data", "items", "funds", "results", "products", "rows"):
            inner = data.get(key)
            if isinstance(inner, list):
                for r in inner:
                    if isinstance(r, dict):
                        yield None, r
                return
        for k, v in data.items():
            if isinstance(v, dict):
                yield k, v
    elif isinstance(data, list):
        for r in data:
            if isinstance(r, dict):
                yield None, r


TICKER_KEYS = ("localExchangeTicker", "fundTicker", "ticker", "symbol",
               "tickerSymbol", "exchangeTicker", "fundSymbol", "productTicker")
ID_KEYS = ("productPageUrl", "portfolioId", "fundId", "productId", "id",
           "fundNumber", "cusip", "seriesId")


def parse_generic_catalog(data) -> dict:
    """Best-effort {ticker: id} over the common product-finder shapes."""
    out = {}
    for key, rec in _walk_records(data):
        tick = ""
        for k in TICKER_KEYS:
            tick = str(_cell(rec.get(k)) or "").strip().upper()
            if tick:
                break
        if not tick or len(tick) > 8:
            continue
        pid = ""
        for k in ID_KEYS:
            raw = str(_cell(rec.get(k)) or "").strip()
            if not raw:
                continue
            # productPageUrl -> trailing path segment is the numeric id
            pid = raw.rstrip("/").split("/")[-1] if "/" in raw else raw
            if pid:
                break
        pid = pid or (str(key) if key else "")
        if tick and pid:
            out[tick] = pid
    return out


def parse_ishares_catalog(data) -> dict:
    """iShares product screener -> {TICKER: {"pid": ..., "path": ...}}.

    WHY THE PATH MATTERS: the holdings CSV lives under the fund's real page
    path, which includes a SLUG — /us/products/239726/ishares-core-sp-500-etf —
    not a literal "fund" segment. Requesting the wrong path redirects to a
    landing page, which is why the endpoint returned HTML instead of CSV. The
    screener hands us the correct path in productPageUrl, so we stop guessing
    and use it.

    Values are dicts; callers that only want the id use resolve(), which stays
    backward compatible with the old {ticker: pid} string form.
    """
    out = {}
    if not isinstance(data, dict):
        return out
    for pid, rec in data.items():
        if not isinstance(rec, dict):
            continue
        tick = str(_cell(rec.get("localExchangeTicker"))
                   or _cell(rec.get("fundTicker")) or "").strip().upper()
        if not tick:
            continue
        path = str(_cell(rec.get("productPageUrl")) or "").strip().rstrip("/")
        real = path.split("/")[-1] if path else str(pid)
        if not real.isdigit():
            # productPageUrl ends in the slug, so pull the numeric id from the
            # path segments; fall back to the JSON key.
            digits = [seg for seg in path.split("/") if seg.isdigit()]
            real = digits[0] if digits else str(pid)
        if real.isdigit():
            out[tick] = {"pid": real, "path": path}
    return out


# --------------------------------------------------------------------------- #
# Catalog registry
# --------------------------------------------------------------------------- #
CATALOGS = {
    # BlackRock / iShares — the strict-WAF case this module exists for.
    "ishares": (
        "https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn"
        "?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/"
        "ishares-product-screener-backend-config&siteEntPassthrough=true",
        parse_ishares_catalog,
    ),
}

# --------------------------------------------------------------------------- #
# Endpoint registry (issuer_endpoints.json) — data, not code.
#
# Every non-iShares URL was an unverified guess and ALL of them failed in
# diagnose.py. Rather than keep guessing, endpoints now live in an editable
# JSON file with a `verified` flag, and UNVERIFIED ISSUERS ARE EXCLUDED FROM
# THE LIVE CASCADE. A broken URL therefore costs nothing instead of adding a
# failing hop to every single lookup.
#
# Flip one on:  edit issuer_endpoints.json -> python issuer_catalog.py --probe X
# --------------------------------------------------------------------------- #
ENDPOINTS_FILE = BASE_DIR / "issuer_endpoints.json"


def load_endpoints() -> dict:
    try:
        raw = json.loads(ENDPOINTS_FILE.read_text())
    except Exception:
        return {}
    return {k: v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, dict)}


def verified_issuers() -> list:
    return [k for k, v in load_endpoints().items() if v.get("verified")]


def catalog_url(issuer: str):
    return (load_endpoints().get(issuer) or {}).get("catalog_url")


def holdings_url_template(issuer: str):
    return (load_endpoints().get(issuer) or {}).get("holdings_url")


def _all_catalog_entries() -> dict:
    """Registry entries merged over the built-in CATALOGS (registry wins)."""
    out = dict(CATALOGS)
    for iss, cfg in load_endpoints().items():
        url = cfg.get("catalog_url")
        if not url:
            continue
        parser = (parse_ishares_catalog if cfg.get("parser") == "ishares"
                  and iss == "ishares" else parse_generic_catalog)
        out[iss] = (url, parser)
    return out


# --------------------------------------------------------------------------- #
# Disclosure reality check — matters more than the plumbing
# --------------------------------------------------------------------------- #
ACTIVE_DISCLOSURE = {
    # issuer -> (vehicle, cadence, caveat)
    "capitalgroup": (
        "etf", "daily",
        "Capital Group's ETFs (CGGR, CGDV, CGUS, CGXU, CGGO, ...) are FULLY "
        "TRANSPARENT active ETFs: real holdings, daily. Their mutual funds "
        "(American Funds) disclose only via N-PORT — quarterly, with up to a "
        "60-day lag. Do not present a stale N-PORT snapshot as current."),
    "troweprice": (
        "etf", "mixed",
        "T. Rowe Price ETF holdings are daily, but the lineup is mixed: some "
        "active ETFs are semi-transparent and are flagged individually at fetch "
        "time. T. Rowe mutual funds disclose only via N-PORT — quarterly, with "
        "up to a 60-day lag."),
}

SEMI_TRANSPARENT_TICKERS = {"TCHP", "TDVG", "TEQI", "TGRT", "TSPA"}


# --------------------------------------------------------------------------- #
# Cache + resolution
# --------------------------------------------------------------------------- #
def _cache_path(issuer: str) -> Path:
    return CACHE_DIR / f"{issuer}.json"


def load_cached(issuer: str, max_age_hours: float | None = None):
    """Cached {ticker: id} if present and fresh, else None."""
    ttl = CATALOG_TTL_HOURS if max_age_hours is None else max_age_hours
    p = _cache_path(issuer)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text())
        if time.time() - float(blob.get("fetched_at", 0)) > ttl * 3600:
            return None
        cat = blob.get("catalog")
        return cat if isinstance(cat, dict) and cat else None
    except Exception:
        return None


def save_cached(issuer: str, catalog: dict):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(issuer).write_text(
            json.dumps({"fetched_at": time.time(), "catalog": catalog}))
    except Exception:
        pass


def fetch_catalog(issuer: str, force: bool = False):
    """Whole-lineup catalog for one issuer. ONE rate-limited request, then
    cached for CATALOG_TTL_HOURS. Returns {} on any failure — callers fall
    through to their existing route (e.g. N-PORT)."""
    issuer = issuer.lower()
    if not force:
        cached = load_cached(issuer)
        if cached:
            return cached
    entry = _all_catalog_entries().get(issuer)
    if not entry:
        return {}
    url, parser = entry
    try:
        raw = polite_get(url, timeout=60)
        catalog = parser(json.loads(raw.decode("utf-8", "replace")))
    except Exception as e:
        print(f"Note: {issuer} catalog fetch failed ({type(e).__name__}: {e}); "
              f"falling back to cache/N-PORT.")
        stale = load_cached(issuer, max_age_hours=24 * 365)   # stale beats nothing
        return stale or {}
    if catalog:
        save_cached(issuer, catalog)
    return catalog


def _entry(catalog: dict, ticker: str):
    """Catalog values are either a bare id (legacy/generic parser) or a dict
    with pid+path (iShares). Normalize to a dict."""
    v = catalog.get(ticker.upper())
    if v is None:
        return None
    return v if isinstance(v, dict) else {"pid": str(v), "path": ""}


def resolve(issuer: str, ticker: str, force: bool = False):
    """ticker -> issuer product id, or None."""
    e = _entry(fetch_catalog(issuer, force=force), ticker)
    return e["pid"] if e else None


def resolve_path(issuer: str, ticker: str, force: bool = False):
    """ticker -> the fund's real product page path (with slug), or "".

    This is what lets us build a holdings URL that actually resolves instead
    of guessing at the path segment.
    """
    e = _entry(fetch_catalog(issuer, force=force), ticker)
    return (e or {}).get("path", "")


def warm_all(issuers=None, force: bool = False):
    """Pre-fetch every catalog — one call per issuer. Run this nightly BEFORE
    the holdings refresh so the per-ticker work needs no discovery calls."""
    # Only VERIFIED issuers. Warming a known-broken endpoint just burns
    # requests and fills the log with noise.
    issuers = issuers or verified_issuers() or list(CATALOGS)
    results = []
    for iss in issuers:
        cat = fetch_catalog(iss, force=force)
        results.append({"issuer": iss, "funds": len(cat), "ok": bool(cat)})
        mark = "\u2713" if cat else "\u2717"
        print(f"  {mark} {iss:<14} {len(cat):>4} funds")
    return results


def _self_test():
    # iShares screener shape
    ishares_payload = {
        "239726": {"localExchangeTicker": "IVV",
                   "productPageUrl": "/us/products/239726/fund"},
        "239763": {"localExchangeTicker": {"r": "IJH"}, "productPageUrl": "x/239763/"},
        "bad": {"localExchangeTicker": ""},
    }
    cat = parse_ishares_catalog(ishares_payload)
    assert cat["IVV"]["pid"] == "239726" and cat["IJH"]["pid"] == "239763", cat
    print("  \u2713 iShares screener parses both cell shapes")
    # The slug path is what lets us build a holdings URL that resolves.
    assert cat["IVV"]["path"] == "/us/products/239726/fund", cat["IVV"]
    print("  \u2713 product page PATH captured, not just the numeric id")
    assert _entry(cat, "IVV")["pid"] == "239726"
    assert _entry({"X": "999"}, "X") == {"pid": "999", "path": ""}
    print("  \u2713 legacy string-valued catalogs still resolve (back compat)")

    # list-of-records shape
    assert parse_generic_catalog(
        {"data": [{"ticker": "SCHD", "fundId": "1234"},
                  {"symbol": "schx", "id": "5678"}]}
    ) == {"SCHD": "1234", "SCHX": "5678"}
    print("  \u2713 generic parser handles list-of-records")

    # bare list + url-shaped id
    assert parse_generic_catalog(
        [{"tickerSymbol": "CGGR", "productPageUrl": "/etf/cggr/40001"}]
    ) == {"CGGR": "40001"}
    print("  \u2713 generic parser extracts id from a product URL")

    assert parse_generic_catalog({"nope": 1}) == {}
    assert parse_generic_catalog(None) == {}
    print("  \u2713 unknown shapes degrade to {} (never raise)")

    # cache round-trip + TTL
    import tempfile
    global CACHE_DIR
    orig = CACHE_DIR
    try:
        CACHE_DIR = Path(tempfile.mkdtemp())
        save_cached("t", {"AAA": "1"})
        assert load_cached("t") == {"AAA": "1"}
        assert load_cached("t", max_age_hours=0) is None
        print("  \u2713 catalog cache round-trips and honors TTL")
    finally:
        CACHE_DIR = orig

    assert "TCHP" in SEMI_TRANSPARENT_TICKERS
    print("  \u2713 semi-transparent TRP ETFs are flagged")

    print("\nCATALOG CHECKS PASS")
    return 0


def probe(issuers) -> int:
    """Test candidate endpoints WITHOUT committing anything. Use this after
    editing issuer_endpoints.json to confirm a URL before flipping verified."""
    cfgs = load_endpoints()
    bad = 0
    for iss in issuers:
        cfg = cfgs.get(iss)
        if not cfg:
            print(f"  ? {iss:<14} not in issuer_endpoints.json")
            bad += 1
            continue
        url = cfg.get("catalog_url")
        mark = "verified" if cfg.get("verified") else "UNVERIFIED"
        print(f"\n  {iss}  [{mark}]\n  GET {url}")
        try:
            raw = polite_get(url, timeout=45, max_retries=0)
        except Exception as e:
            print(f"  -> FAIL {type(e).__name__}: {e}")
            bad += 1
            continue
        head = raw[:200].decode("utf-8", "replace")
        if "<html" in head.lower():
            print("  -> HTML, not JSON (bot challenge or wrong URL)")
            bad += 1
            continue
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            print(f"  -> not JSON: {head[:120]!r}")
            bad += 1
            continue
        _url, parser = _all_catalog_entries().get(iss, (None, parse_generic_catalog))
        cat = parser(data)
        if cat:
            print(f"  -> OK: parsed {len(cat)} funds. "
                  f"Sample: {list(cat.items())[:3]}")
            print(f"     Set \"verified\": true for {iss} in issuer_endpoints.json")
        else:
            print(f"  -> reachable but parsed 0 funds; payload keys: "
                  f"{list(data)[:8] if isinstance(data, dict) else type(data).__name__}")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--probe" in args:
        i = args.index("--probe")
        names = args[i + 1:] or list(load_endpoints())
        raise SystemExit(probe(names))
    if "--refresh" in args:
        i = args.index("--refresh")
        names = args[i + 1:] or None
        raise SystemExit(0 if warm_all(names, force=True) else 1)
    raise SystemExit(_self_test())
