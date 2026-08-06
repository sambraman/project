"""
test_issuers.py — offline checks for the catalog/rate-limit expansion.
No network, no API keys.

    python test_issuers.py
"""

from __future__ import annotations

import holdings
import issuer_catalog
from ratelimit import LIMITER


def main():
    # --- routing ------------------------------------------------------------ #
    assert holdings.classify_issuer("IVV") == "ishares"
    assert holdings.classify_issuer("SCHD") == "schwab", \
        "SCHD is a tracked default and must not fall to quarterly N-PORT"
    print("  \u2713 SCHD now routes to Schwab (was falling through to N-PORT)")

    for tick, iss in [("SMH", "vaneck"), ("BOTZ", "globalx"), ("DGRW", "wisdomtree"),
                      ("SKYY", "firsttrust"), ("CGGR", "capitalgroup"),
                      ("TCAF", "troweprice")]:
        got = holdings.classify_issuer(tick)
        assert got == iss, f"{tick} -> {got}, expected {iss}"
    print("  \u2713 new passive + active issuers route correctly")

    assert holdings.classify_issuer("ZZZZ") == "nport"
    print("  \u2713 unknown tickers still fall back to N-PORT")

    for iss in holdings.CATALOG_HOLDINGS_URLS:
        assert iss in holdings.ISSUER_DISPATCH, f"{iss} missing from dispatch"
    print("  \u2713 every catalog issuer has a dispatch entry")

    # --- rate limits -------------------------------------------------------- #
    b = LIMITER.bucket("www.ishares.com")
    assert b.max_calls <= 10 and b.per_seconds >= 60, \
        "iShares bucket must stay within the 10/60s ceiling"
    print(f"  \u2713 iShares paced at {b.max_calls}/{b.per_seconds:.0f}s (ceiling 10/60)")

    for host in ("www.capitalgroup.com", "www.troweprice.com",
                 "www.schwabassetmanagement.com"):
        assert LIMITER.bucket(host).max_calls <= 10
    print("  \u2713 every new issuer host has a conservative bucket")

    # --- disclosure warnings ------------------------------------------------ #
    w = holdings._disclosure_warnings("troweprice", "TCHP")
    assert w and "PROXY" in w[0].upper(), w
    print("  \u2713 semi-transparent TRP ETF (TCHP) carries a proxy-portfolio warning")

    w2 = holdings._disclosure_warnings("troweprice", "TCAF")
    assert not any("PROXY" in x.upper() for x in w2), w2
    print("  \u2713 transparent TRP ETF (TCAF) is not falsely flagged")

    w3 = holdings._disclosure_warnings("capitalgroup", "CGGR")
    assert w3 and "N-PORT" in w3[0]
    print("  \u2713 Capital Group carries the ETF-vs-mutual-fund cadence caveat")

    r = holdings.HoldingsResult("TCHP", "2026-08-04", "troweprice", False, [],
                                ["proxy portfolio"])
    assert r.as_dict()["warnings"] == ["proxy portfolio"], \
        "warnings must survive serialization to the API layer"
    print("  \u2713 warnings serialize through as_dict() to the API")

    # --- catalog wiring ----------------------------------------------------- #
    for iss in holdings.CATALOG_HOLDINGS_URLS:
        assert iss in issuer_catalog.CATALOGS, f"{iss} has no catalog entry"
    print("  \u2713 every dispatch issuer has a catalog endpoint configured")

    print("\nISSUER CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
