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
                      parse_ishares_csv, _normalize_weights, Holding,
                      _candidate_order, resolve_ishares_pid, DAILY_ISSUERS)
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

    print("Cascade order (any-ETF coverage)")
    check("offline is fixture-bound to a single route",
          _candidate_order("VTI", offline=True) == ["vanguard"])
    live_voo = _candidate_order("VOO", offline=False)
    check("live known ticker leads with its issuer", live_voo[0] == "vanguard")
    check("live cascade tries every daily feed",
          set(DAILY_ISSUERS).issubset(set(live_voo)))
    check("live cascade ends at N-PORT fallback", live_voo[-1] == "nport")
    unknown = _candidate_order("XYZ", offline=False)
    check("live unknown ticker still tries all feeds + nport",
          set(DAILY_ISSUERS).issubset(set(unknown)) and unknown[-1] == "nport")
    check("iShares pid resolves from seed map (IVV)",
          resolve_ishares_pid("IVV", offline=True) == "239726")
    check("iShares pid None offline for unknown (falls through)",
          resolve_ishares_pid("ZZZZ", offline=True) is None)

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
    check("SCHD as-of = repPdDate 2026-05-31", schd.as_of == "2026-05-31")
    check("SCHD holdings kept by name (N-PORT carries no ticker)",
          any(h.name.startswith("Texas Instruments") for h in schd.holdings))
    check("SCHD weights normalized to decimals", schd.holdings[0].weight <= 1.0)

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
        b'<name>Foo Corp</name><cusip>111111111</cusip>'
        b'<identifiers><isin value="US1111111111"/></identifiers>'
        b'<pctVal>12.5</pctVal></invstOrSec></invstOrSecs></formData></edgarSubmission>')
    check("N-PORT parser reads repPdDate", nas == "2026-03-31")
    check("N-PORT parser reads name + cusip", nrows and nrows[0]["name"] == "Foo Corp"
          and nrows[0]["cusip"] == "111111111")

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

    print("Fundamentals — extract + metric math (synthetic companyfacts, no network)")
    from fundamentals.extract import annual_series
    from fundamentals.metrics import compute_metrics

    def _flow(vals):   # {year: val} -> annual 10-K duration facts
        return {"units": {"USD": [
            {"form": "10-K", "start": f"{y}-01-01", "end": f"{y}-12-31",
             "val": v, "filed": f"{y + 1}-02-15"} for y, v in vals.items()]}}

    def _stock(vals, unit="USD"):
        return {"units": {unit: [
            {"form": "10-K", "end": f"{y}-12-31", "val": v, "filed": f"{y + 1}-02-15"}
            for y, v in vals.items()]}}

    facts = {"entityName": "Test Co", "facts": {"us-gaap": {
        "Revenues": _flow({2021: 800, 2022: 900, 2023: 1000, 2024: 1200}),
        "NetIncomeLoss": _flow({2023: 100, 2024: 150}),
        "GrossProfit": _flow({2024: 480}),
        "OperatingIncomeLoss": _flow({2024: 300}),
        "InterestExpense": _flow({2024: 20}),
        "EarningsPerShareDiluted": _flow({2023: 1.0, 2024: 1.5}),
        "Assets": _stock({2024: 2000}),
        "AssetsCurrent": _stock({2024: 800}),
        "Liabilities": _stock({2024: 1000}),
        "LiabilitiesCurrent": _stock({2024: 400}),
        "StockholdersEquity": _stock({2024: 1000}),
        "LongTermDebtNoncurrent": _stock({2024: 500}),
        "CommonStockSharesOutstanding": _stock({2024: 100}, unit="shares"),
    }}}
    # A quarterly stub that must NOT be picked up as an annual value.
    facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        {"form": "10-Q", "start": "2024-01-01", "end": "2024-03-31", "val": 300,
         "filed": "2024-04-15"})

    s = annual_series(facts)
    check("annual revenue series has 4 years", len(s["revenue"]) == 4)
    check("quarterly stub excluded from annual revenue", 1200 in s["revenue"].values()
          and 300 not in s["revenue"].values())
    m = compute_metrics(s, price=30.0)
    check("net_margin = 150/1200", abs(m["net_margin"] - 0.125) < 1e-6)
    check("gross_margin = 480/1200", abs(m["gross_margin"] - 0.40) < 1e-6)
    check("roe = 150/1000", abs(m["roe"] - 0.15) < 1e-6)
    check("revenue_growth_yoy = 1200/1000-1", abs(m["revenue_growth_yoy"] - 0.20) < 1e-6)
    check("earnings_growth_yoy = 150/100-1", abs(m["earnings_growth_yoy"] - 0.50) < 1e-6)
    check("revenue_cagr_3y = (1200/800)^(1/3)-1",
          abs(m["revenue_cagr_3y"] - ((1200 / 800) ** (1 / 3) - 1)) < 1e-6)
    check("debt_to_equity = 500/1000", abs(m["debt_to_equity"] - 0.5) < 1e-6)
    check("current_ratio = 800/400", abs(m["current_ratio"] - 2.0) < 1e-6)
    check("interest_coverage = 300/20", abs(m["interest_coverage"] - 15.0) < 1e-6)
    check("book_value_per_share = 1000/100", abs(m["book_value_per_share"] - 10.0) < 1e-6)
    check("pe_ratio = price 30 / eps 1.5", abs(m["pe_ratio"] - 20.0) < 1e-6)
    check("ps_ratio = 30 / (1200/100)", abs(m["ps_ratio"] - 2.5) < 1e-6)
    check("multiples blank without a price", compute_metrics(s)["pe_ratio"] is None)

    print("Fundamentals — 10-year history, coverage %, and store round-trip")
    from fundamentals.metrics import compute_history
    from fundamentals import CORE_METRICS
    from store import FundamentalsStore
    hist = compute_history(s, max_years=10)
    check("history spans all revenue years", {r["fiscal_year"] for r in hist}
          == {2021, 2022, 2023, 2024})
    check("history newest-first", hist[0]["fiscal_year"] == 2024)
    check("history per-year growth (2024 vs 2023)",
          abs(hist[0]["revenue_growth_yoy"] - 0.20) < 1e-6)
    check("history earliest year has no YoY (no 2020 revenue)",
          hist[-1]["fiscal_year"] == 2021 and hist[-1]["revenue_growth_yoy"] is None)
    # Coverage: 2 years x CORE_METRICS, minus the earliest-year growth cells.
    mapped = sum(1 for r in hist for k in CORE_METRICS if r.get(k) is not None)
    total = len(hist) * len(CORE_METRICS)
    pct = round(100 * mapped / total, 1)
    check("coverage is a sensible 0-100%", 0 < pct <= 100)

    record = {"ticker": "TEST", "cik": 1, "name": "Test Co", "sector": "Information Technology",
              "industry": "x", "country": "United States", "hq": "Nowhere, NY",
              "currency": "USD", "first_year": 2021, "last_year": 2024, "years_count": len(hist),
              "coverage_pct": pct, "mapped_cells": mapped, "total_cells": total,
              "history": hist}
    with tempfile.TemporaryDirectory() as d:
        st = FundamentalsStore(Path(d) / "f.db")
        st.write_company(record)
        got = st.get_company("TEST")
        check("store round-trips a company", got and got["name"] == "Test Co")
        check("store round-trips all history rows", got and len(got["history"]) == len(hist))
        check("store search finds it by name", st.search("Test")[0]["ticker"] == "TEST")
        check("store stats count", st.stats()["companies"] == 1)

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
