"""
smoke_test.py — offline logic checks. No network, no server, stdlib only.

    python smoke_test.py            # prints "ALL SMOKE CHECKS PASS" on success

This is the "test the backend function without going through the backend" path:
it drives holdings.get_holdings() directly against the bundled fixtures and
asserts routing, per-issuer parsing, the as-of tag, the stale flag, weight
normalization, sorting, and the SQLite cache round-trip. If this passes, the
logic is sound before you touch the live network.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from holdings import (get_holdings, classify_issuer, HoldingsError,
                      parse_ishares_csv, _normalize_weights, Holding)
from cache import HoldingsCache
from nport_source import parse_nport_xml

PASS, FAIL = "  ✓", "  ✗"
_failures = []


def check(label, cond):
    print((PASS if cond else FAIL), label)
    if not cond:
        _failures.append(label)


def main():
    print("Routing")
    check("IVV -> ishares", classify_issuer("IVV") == "ishares")
    check("SPY -> spdr", classify_issuer("SPY") == "spdr")
    check("QQQ -> invesco", classify_issuer("QQQ") == "invesco")
    check("VTI -> vanguard", classify_issuer("VTI") == "vanguard")
    check("unknown ZZZZ -> nport fallback", classify_issuer("ZZZZ") == "nport")

    print("Per-issuer offline fetch + parse")
    ivv = get_holdings("IVV", offline=True)
    check("IVV parsed 10 holdings", ivv.count == 10)
    check("IVV as-of parsed (2026-07-31)", ivv.as_of == "2026-07-31")
    check("IVV not stale", ivv.is_stale is False)
    check("IVV top holding is AAPL", ivv.holdings[0].ticker == "AAPL")
    check("IVV weights are decimals (<=1)", ivv.holdings[0].weight <= 1.0)
    check("IVV sorted by weight desc",
          all(ivv.holdings[i].weight >= ivv.holdings[i + 1].weight
              for i in range(ivv.count - 1)))

    qqq = get_holdings("QQQ", offline=True)
    check("QQQ (invesco) as-of parsed", qqq.as_of == "2026-07-31")
    check("QQQ source is invesco", qqq.source == "invesco")

    spy = get_holdings("SPY", offline=True)
    check("SPY (spdr) as-of parsed 2026-07-31", spy.as_of == "2026-07-31")

    vti = get_holdings("VTI", offline=True)
    check("VTI (vanguard, real data) > 1000 holdings", vti.count > 1000)
    check("VTI coverage > 90%", vti.total_weight > 0.90)
    check("VTI as-of parsed", vti.as_of == "2026-06-30")

    print("N-PORT fallback (quarterly / stale)")
    schd = get_holdings("SCHD", offline=True)
    check("SCHD routed to nport", schd.source == "nport")
    check("SCHD flagged stale", schd.is_stale is True)
    check("SCHD as-of = repPdDate 2026-06-30", schd.as_of == "2026-06-30")
    check("SCHD dropped zero/blank-ticker private placement kept by name",
          any(h.name.startswith("Some Private") for h in schd.holdings))

    print("Parser units")
    as_of, hs = parse_ishares_csv(
        'Fund Holdings as of,"Jan 2, 2026"\n\nTicker,Name,Weight (%)\nAAA,Alpha,50\nBBB,Beta,50\n')
    check("iShares parser reads as-of", as_of == "2026-01-02")
    check("iShares parser reads 2 rows", len(hs) == 2)
    norm = _normalize_weights([Holding("A", "", 50.0), Holding("B", "", 50.0)])
    check("percent weights normalized to decimals", abs(sum(h.weight for h in norm) - 1.0) < 1e-9)
    nas, nrows = parse_nport_xml(
        b'<?xml version="1.0"?><edgarSubmission xmlns="x"><formData><genInfo>'
        b'<repPdDate>2026-03-31</repPdDate></genInfo><invstOrSecs><invstOrSec>'
        b'<name>Foo</name><identifiers><ticker value="FOO"/></identifiers>'
        b'<pctVal>12.5</pctVal></invstOrSec></invstOrSecs></formData></edgarSubmission>')
    check("N-PORT parser reads repPdDate", nas == "2026-03-31")
    check("N-PORT parser reads a security", nrows and nrows[0][0] == "FOO")

    print("Error handling")
    try:
        get_holdings("", offline=True)
        check("empty ticker raises", False)
    except HoldingsError:
        check("empty ticker raises", True)

    print("Cache round-trip (sqlite)")
    with tempfile.TemporaryDirectory() as d:
        cache = HoldingsCache(Path(d) / "t.db")
        cache.put(ivv)
        hit = cache.get("IVV")
        check("cache stores + returns payload", hit is not None and hit["count"] == 10)
        check("cache tags fetched_at", "_fetched_at" in hit)
        check("cache list_tickers works", cache.list_tickers()[0]["ticker"] == "IVV")

    print()
    if _failures:
        print(f"{len(_failures)} CHECK(S) FAILED:")
        for f in _failures:
            print("   -", f)
        return 1
    print("ALL SMOKE CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
