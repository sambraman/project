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
    entries = issuer_catalog._all_catalog_entries()
    for iss in holdings.CATALOG_HOLDINGS_URLS:
        assert iss in entries, f"{iss} has no catalog endpoint in the registry"
    print("  \u2713 every dispatch issuer has a registry endpoint")

    # Unverified issuers must NOT pollute the live cascade — diagnose.py showed
    # all seven failing on every single lookup.
    verified = issuer_catalog.verified_issuers()
    for tick in ("IVV", "SMH", "ZZZZ"):
        order = holdings._candidate_order(tick, offline=False)
        bad = [i for i in order
               if i in holdings.CATALOG_ISSUERS and i not in verified]
        assert not bad, f"{tick} cascade includes unverified {bad}"
    print(f"  \u2713 unverified issuers excluded from the cascade "
          f"(verified: {verified})")
    assert len(holdings._candidate_order("IVV", offline=False)) <= 6
    print("  \u2713 IVV cascade trimmed to <=6 hops (was 12)")

    # The Invesco crash: DictReader restkey is a LIST, and .strip() on it blew
    # up on EVERY ticker because invesco sits in every cascade.
    try:
        holdings.parse_invesco_csv("a,b,c\n1,2,3,4,5\n")
        raise AssertionError("should have raised HoldingsError")
    except holdings.HoldingsError:
        pass
    print("  \u2713 Invesco parser fails cleanly on a non-CSV response")
    _a, _h = holdings.parse_invesco_csv(
        "Holding Ticker,Name,Weight,Date\nAAPL,Apple,7.05,08/04/2026\n")
    assert _h[0].ticker == "AAPL" and _a == "2026-08-04"
    print("  \u2713 Invesco parser still parses a valid CSV")

    test_degradation()

    print("\nISSUER CHECKS PASS")
    return 0




def test_degradation():
    """The actual bug behind 'IVV shows 9/30/2025': a daily feed that fails must
    not silently masquerade as a fund with no daily file."""
    print("\nSilent-degradation detection")

    # nport result reached only after other issuers were tried == degraded
    r = holdings.HoldingsResult("IVV", "2025-09-30", "nport", True, [],
                                attempts=["ishares: HTTP 403"])
    assert r.degraded is True
    print("  \u2713 nport-after-failures is flagged degraded")

    # nport with nothing else tried is a legitimate route, not a degradation
    r2 = holdings.HoldingsResult("ZZZZ", "2025-09-30", "nport", True, [],
                                 attempts=[])
    assert r2.degraded is False
    print("  \u2713 straight-to-nport is not falsely flagged")

    # a working daily feed is never degraded
    r3 = holdings.HoldingsResult("IVV", "2026-08-04", "ishares", False, [],
                                 attempts=[])
    assert r3.degraded is False
    print("  \u2713 healthy daily feed is not flagged")

    d = r.as_dict()
    assert d["degraded"] is True and d["attempts"] == ["ishares: HTTP 403"], d
    assert "attempts" in d, "the failure trail must reach the API layer"
    print("  \u2713 degraded + attempts serialize to the API")

    # catalog short-circuit: cached catalog that excludes the ticker => skip
    import issuer_catalog as ic, tempfile, pathlib
    orig = ic.CACHE_DIR
    try:
        ic.CACHE_DIR = pathlib.Path(tempfile.mkdtemp())
        ic.save_cached("vaneck", {"GDX": "1"})
        assert holdings._catalog_lists("vaneck", "SMH") is False
        assert holdings._catalog_lists("vaneck", "GDX") is True
        assert holdings._catalog_lists("globalx", "SMH") is None, \
            "no cache must read as unknown, not as absent"
        print("  \u2713 catalog membership is tri-state (present/absent/unknown)")
    finally:
        ic.CACHE_DIR = orig

    # the cascade contract from smoke_test must survive
    order = holdings._candidate_order("VOO", offline=False)
    assert set(holdings.DAILY_ISSUERS).issubset(set(order)), order
    assert order[-1] == "nport" and order[0] == "vanguard"
    print("  \u2713 cascade still offers every daily feed, ending at N-PORT")

    print("\nDEGRADATION CHECKS PASS")


if __name__ == "__main__":
    raise SystemExit(main())
