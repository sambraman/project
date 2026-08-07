"""
healthcheck.py — one command that tells you whether the system is actually OK.

Runs offline checks always, and live checks when a network is available. Use it
after any change, and on the deployed service to see what production sees.

    python healthcheck.py           # offline structural checks
    python healthcheck.py --live    # also probe issuers + SEC (uses requests)

Exit code is 0 only when nothing is BROKEN. Warnings don't fail the run.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("OFFLINE", "1")

OK, WARN, BAD = [], [], []


def ok(m):
    OK.append(m)
    print(f"  \u2713 {m}")


def warn(m):
    WARN.append(m)
    print(f"  \u26a0 {m}")


def bad(m):
    BAD.append(m)
    print(f"  \u2717 {m}")


def section(t):
    print(f"\n{t}\n" + "-" * 68)


def check_imports():
    section("MODULES")
    mods = ["holdings", "issuer_catalog", "ratelimit", "datastore",
            "sector_kpis", "nport_source", "refresh", "refresh_prices",
            "fundamentals.quarterly", "cache", "store", "app"]
    for m in mods:
        try:
            __import__(m)
            ok(f"{m} imports")
        except Exception as e:
            bad(f"{m} FAILED to import: {type(e).__name__}: {e}")


def check_storage():
    section("STORAGE")
    try:
        from datastore import STORE
    except Exception as e:
        bad(f"datastore unavailable: {e}")
        return
    if getattr(STORE, "degraded", False):
        bad("datastore is on a TEMP fallback — data will vanish on restart. "
            "Set DATA_DIR to a writable persistent disk.")
    else:
        ok(f"datastore writable at {STORE.path}")
    try:
        s = STORE.stats()
        ok(f"stats: {s.get('holdings', 0)} holdings rows, "
           f"{s.get('prices', 0)} price rows, {s.get('kpis', 0)} kpi rows, "
           f"{s.get('db_mb')} MB")
        if not s.get("funds"):
            warn("no funds stored yet — run refresh.py, or the web app will be "
                 "cold on every request")
        for f in (s.get("funds") or []):
            if f.get("degraded"):
                warn(f"{f['fund']}: degraded (daily feed failed, serving "
                     f"N-PORT as of {f.get('as_of')})")
    except Exception as e:
        bad(f"stats failed: {type(e).__name__}: {e}")

    # round-trip proof
    try:
        STORE.put_json("healthcheck", "probe", {"t": time.time()}, ttl_hours=1)
        assert STORE.get_json("healthcheck", "probe") is not None
        STORE.delete("healthcheck", "probe")
        ok("datastore read/write round-trip")
    except Exception as e:
        bad(f"datastore round-trip failed: {type(e).__name__}: {e}")


def check_issuers():
    section("ISSUER CONFIG")
    import holdings
    import issuer_catalog as ic
    verified = ic.verified_issuers()
    if verified:
        ok(f"verified issuers: {', '.join(verified)}")
    else:
        bad("NO verified issuers — every ticker will fall back to quarterly "
            "N-PORT. Fix: issuer_catalog.py --probe <issuer>")
    unver = [i for i in ic.load_endpoints() if i not in verified]
    if unver:
        warn(f"unverified (excluded from cascade): {', '.join(unver)}")

    for t in ("IVV", "SPY", "SCHD", "SMH"):
        order = holdings._candidate_order(t, offline=False)
        if len(order) > 7:
            warn(f"{t} cascade is {len(order)} hops — unverified issuers leaking in?")
        else:
            ok(f"{t} cascade: {' -> '.join(order)}")

    for iss in ic.CATALOGS:
        cat = ic.load_cached(iss)
        if cat:
            ok(f"catalog[{iss}]: {len(cat)} funds cached")
        else:
            warn(f"catalog[{iss}]: not cached — run "
                 f"issuer_catalog.py --refresh {iss}")


def check_kpis():
    section("KPI ENGINE")
    import sector_kpis as sk
    ok(f"{len(sk.SECTOR_TAGS)} sectors: {', '.join(sorted(sk.SECTOR_TAGS))}")
    for sec, specs in sk.SECTOR_TAGS.items():
        derived = sk.DERIVED.get(sec, [])
        if not derived:
            warn(f"{sec}: {len(specs)} tags but NO derived KPIs")
        else:
            ok(f"{sec}: {len(specs)} tags + {len(derived)} derived")
    # every derived formula must survive empty input
    for sec in sk.SECTOR_TAGS:
        try:
            sk.compute_derived(sec, {}, "")
            ok(f"{sec}: derived formulas safe on empty data")
        except Exception as e:
            bad(f"{sec}: derived formula raised on empty input: {e}")


def check_config():
    section("CONFIG")
    for var, why in [
        ("DATA_DIR", "where the datastore lives (must be persistent)"),
        ("REFRESH_TOKEN", "guards POST /refresh; unset = all refreshes 401"),
        ("EODHD_API_KEY", "price provider (yfinance fallback if unset)"),
        ("HTTP_CONTACT", "identifies you to issuers; helps avoid blocks"),
        ("TRACKED_TICKERS", "what the nightly refresh pulls"),
    ]:
        val = os.environ.get(var)
        if val:
            shown = val if var in ("DATA_DIR", "TRACKED_TICKERS", "HTTP_CONTACT") \
                else f"set ({len(val)} chars)"
            ok(f"{var}={shown}")
        else:
            (bad if var == "REFRESH_TOKEN" else warn)(f"{var} unset — {why}")


def check_live():
    section("LIVE PROBES")
    import urllib.error
    from ratelimit import polite_get
    for name, url in [
        ("SEC EDGAR", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                      "&CIK=S000004310&type=NPORT-P&count=1&output=atom"),
        ("iShares screener",
         "https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn"
         "?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/"
         "ishares-product-screener-backend-config&siteEntPassthrough=true"),
    ]:
        try:
            raw = polite_get(url, timeout=45, max_retries=0)
            head = raw[:200].decode("utf-8", "replace").lower()
            if "<html" in head:
                bad(f"{name}: returned HTML (bot challenge / wrong URL)")
            else:
                ok(f"{name}: {len(raw):,} bytes")
        except urllib.error.HTTPError as e:
            bad(f"{name}: HTTP {e.code}")
        except Exception as e:
            bad(f"{name}: {type(e).__name__}: {e}")


def main():
    live = "--live" in sys.argv
    print("=" * 68)
    print("LookThrough health check" + ("  [live]" if live else "  [offline]"))
    print("=" * 68)
    check_imports()
    check_storage()
    check_issuers()
    check_kpis()
    check_config()
    if live:
        check_live()

    print("\n" + "=" * 68)
    print(f"{len(OK)} OK   {len(WARN)} warnings   {len(BAD)} broken")
    if BAD:
        print("\nBROKEN:")
        for b in BAD:
            print(f"  - {b}")
    if WARN:
        print("\nWARNINGS:")
        for w in WARN:
            print(f"  - {w}")
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
