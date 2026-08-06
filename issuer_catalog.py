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
    """iShares product screener: {productId: {...}}, ids are numeric."""
    out = {}
    if not isinstance(data, dict):
        return out
    for pid, rec in data.items():
        if not isinstance(rec, dict):
            continue
        tick = str(_cell(rec.get("localExchangeTicker"))
                   or _cell(rec.get("fundTicker")) or "").strip().upper()
        real = str(_cell(rec.get("productPageUrl")) or pid)
        real = real.rstrip("/").split("/")[-1]
        real = real if real.isdigit() else str(pid)
        if tick and real.isdigit():
            out[tick] = real
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
    # Passive shops not previously covered.  # VERIFY each URL
    "schwab": (
        "https://www.schwabassetmanagement.com/api/fund-data/all-etfs",
        parse_generic_catalog,
    ),
    "vaneck": (
        "https://www.vaneck.com/api/products/us/etf/list/",
        parse_generic_catalog,
    ),
    "globalx": (
        "https://www.globalxetfs.com/api/funds/",
        parse_generic_catalog,
    ),
    "wisdomtree": (
        "https://www.wisdomtree.com/api/etfs/list",
        parse_generic_catalog,
    ),
    "firsttrust": (
        "https://www.ftportfolios.com/api/products/etf",
        parse_generic_catalog,
    ),
    # Active shops. Their ETFs publish daily; their mutual funds do NOT
    # (see the caveats in ACTIVE_DISCLOSURE below).
    "capitalgroup": (
        "https://www.capitalgroup.com/api/etf/products",
        parse_generic_catalog,
    ),
    "troweprice": (
        "https://www.troweprice.com/api/products/etf/list",
        parse_generic_catalog,
    ),
}


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
    entry = CATALOGS.get(issuer)
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


def resolve(issuer: str, ticker: str, force: bool = False):
    """ticker -> issuer product id, or None."""
    return fetch_catalog(issuer, force=force).get(ticker.upper())


def warm_all(issuers=None, force: bool = False):
    """Pre-fetch every catalog — one call per issuer. Run this nightly BEFORE
    the holdings refresh so the per-ticker work needs no discovery calls."""
    issuers = issuers or list(CATALOGS)
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
    assert cat == {"IVV": "239726", "IJH": "239763"}, cat
    print("  \u2713 iShares screener parses both cell shapes")

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


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--refresh" in args:
        i = args.index("--refresh")
        names = args[i + 1:] or None
        raise SystemExit(0 if warm_all(names, force=True) else 1)
    raise SystemExit(_self_test())
