"""
diagnose.py — why did this ticker return stale data?

A quarter-end as-of (9/30, 12/31, 3/31, 6/30) means the daily feed lost and
N-PORT won. This prints, per ticker, exactly which route was tried, what the
issuer actually returned, and what to do about it.

    python diagnose.py IVV SMH            # the two that looked wrong
    python diagnose.py --catalogs         # catalog cache health only
    python diagnose.py IVV --raw          # dump the first bytes iShares returns

Run this ON THE DEPLOYED SERVICE (or anywhere with network + the same IP), since
the whole question is what the issuer's WAF returns to *that* caller.
"""

from __future__ import annotations

import sys
import urllib.error

import holdings
import issuer_catalog
from ratelimit import LIMITER, polite_get

QUARTER_ENDS = {"03-31", "06-30", "09-30", "12-31", "03-30", "06-29", "09-29", "12-30"}


def show_catalogs():
    print("\nCATALOG CACHE")
    print("-" * 68)
    for iss in issuer_catalog.CATALOGS:
        cat = issuer_catalog.load_cached(iss)
        if cat:
            print(f"  \u2713 {iss:<14} {len(cat):>4} funds cached")
        else:
            stale = issuer_catalog.load_cached(iss, max_age_hours=24 * 365)
            state = f"STALE ({len(stale)} funds)" if stale else "EMPTY \u2014 never fetched"
            print(f"  \u2717 {iss:<14} {state}")
    print("\n  Empty catalog => every lookup falls to N-PORT.")
    print("  Fix: python issuer_catalog.py --refresh <issuer>")


def probe_raw(ticker: str):
    """Hit the iShares CSV endpoint directly and show what comes back. This is
    the single most useful signal: CSV = working, HTML = bot challenge,
    403 = WAF block, 404 = wrong product id."""
    pid = holdings.resolve_ishares_pid(ticker, offline=False)
    print(f"\n  resolved product id: {pid or 'NONE (this alone forces N-PORT)'}")
    if not pid:
        return
    url = (f"https://www.ishares.com/us/products/{pid}/fund/"
           f"1467271812596.ajax?fileType=csv&fileName={ticker}_holdings&dataType=fund")
    print(f"  GET {url}")
    try:
        raw = polite_get(url, headers=holdings._BROWSER_HEADERS, max_retries=0)
    except urllib.error.HTTPError as e:
        print(f"  -> HTTP {e.code} {e.reason}")
        if e.code == 403:
            print("     DIAGNOSIS: WAF block. Slow down (RATE_LIMITS), set")
            print("     HTTP_CONTACT, and if it persists request access from")
            print("     BlackRock. Do NOT retry harder or disguise the client.")
        elif e.code == 404:
            print("     DIAGNOSIS: wrong product id or URL template. Re-check the")
            print("     .ajax path in devtools -> Network on the fund page.")
        return
    except Exception as e:
        print(f"  -> {type(e).__name__}: {e}")
        return
    head = raw[:300].decode("utf-8", "replace")
    if "<html" in head.lower():
        print("  -> HTML, not CSV. DIAGNOSIS: bot-challenge/interstitial page.")
        print(f"     first bytes: {head[:160]!r}")
    else:
        print(f"  -> {len(raw)} bytes of CSV. Endpoint is HEALTHY.")
        print(f"     first bytes: {head[:160]!r}")


def diagnose(ticker: str, raw: bool = False):
    t = ticker.upper()
    print(f"\n{'=' * 68}\n{t}\n{'=' * 68}")

    guess = holdings.classify_issuer(t)
    order = holdings._candidate_order(t, offline=False)
    print(f"  classified as : {guess}")
    print(f"  will try      : {' -> '.join(order)}")

    for iss in order:
        if iss in issuer_catalog.CATALOGS:
            cat = issuer_catalog.load_cached(iss)
            if cat is None:
                print(f"  catalog[{iss}] : NOT CACHED (will fetch, or fail to N-PORT)")
            elif t in cat:
                print(f"  catalog[{iss}] : found, product id {cat[t]}")
            else:
                print(f"  catalog[{iss}] : {len(cat)} funds cached, {t} NOT among them")

    if raw and guess == "ishares":
        probe_raw(t)

    print("\n  fetching...")
    try:
        res = holdings.get_holdings(t, offline=False)
    except holdings.HoldingsError as e:
        print(f"  FAILED entirely: {e}")
        return
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return

    flag = "  \u26a0 STALE" if res.is_stale else "  \u2713 daily"
    print(f"\n  source={res.source}  as_of={res.as_of}  n={res.count}{flag}")

    mmdd = (res.as_of or "")[5:]
    if mmdd in QUARTER_ENDS and res.source != "nport":
        print("  NOTE: quarter-end date from a non-N-PORT source \u2014 verify the parser.")

    if res.attempts:
        print("\n  what failed before this:")
        for a in res.attempts:
            print(f"    \u2717 {a}")
    if res.degraded:
        print("\n  DIAGNOSIS: silent degradation \u2014 the daily feed failed and you")
        print("  got quarterly N-PORT. The failures above are the real bug.")
    elif res.source == "nport":
        print("\n  DIAGNOSIS: went straight to N-PORT (no daily route configured).")
    else:
        print("\n  DIAGNOSIS: daily feed worked.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    raw = "--raw" in argv
    argv = [a for a in argv if not a.startswith("-")]

    show_catalogs()
    for t in (argv or ["IVV", "SMH"]):
        diagnose(t, raw=raw)

    print(f"\n{'=' * 68}")
    print("Buckets remaining this window:")
    for host in ("www.ishares.com", "www.vaneck.com", "www.sec.gov"):
        b = LIMITER.bucket(host)
        print(f"  {host:<22} {b._tokens:.1f}/{b.max_calls} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
