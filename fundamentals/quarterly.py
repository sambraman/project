"""
quarterly.py — quarterly series from companyfacts (the 10-Q path).

extract.py deliberately keeps only annual 10-K facts. Sector KPIs are mostly
quarterly animals — hyperscaler capex and RPO are read out of the 10-Q, bank
credit costs turn quarter to quarter — so this module does the same job on a
quarterly grid.

Three fact shapes matter:

  flow    period measures (capex, revenue, provisions). ~90-day duration only,
          so year-to-date and annual stubs are rejected. Q4 is usually NOT filed
          as a discrete quarter (there's no Q4 10-Q), so derive_q4() backs it
          out as FY minus Q1-Q3.
  instant point-in-time measures (RPO, deposits, total assets, CET1 ratio).
  ttm     trailing four quarters of a flow, for ratios that need a full year.

Periods are keyed "YYYYQn" off the period END date, so they sort lexically.

    python fundamentals/quarterly.py     # offline self-test
"""

from __future__ import annotations

from datetime import date

QUARTER_MIN_DAYS = 80
QUARTER_MAX_DAYS = 100
ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380


def _days(start: str, end: str):
    try:
        y1, m1, d1 = map(int, str(start).split("-"))
        y2, m2, d2 = map(int, str(end).split("-"))
        return (date(y2, m2, d2) - date(y1, m1, d1)).days
    except Exception:
        return None


def period_key(end: str) -> str:
    """'2026-03-31' -> '2026Q1'. Keyed off the period end date."""
    try:
        y, m, _ = str(end).split("-")
        return f"{int(y)}Q{(int(m) - 1) // 3 + 1}"
    except Exception:
        return ""


def _iter_facts(node: dict):
    for unit, facts in (node.get("units") or {}).items():
        for f in facts:
            yield unit, f


def quarterly_flow(node: dict) -> dict:
    """{period: value} for discrete ~quarterly durations. Latest filing wins,
    so restatements supersede."""
    out = {}
    for _unit, f in _iter_facts(node):
        form = str(f.get("form", ""))
        if not (form.startswith("10-Q") or form.startswith("10-K")):
            continue
        start, end, val = f.get("start"), f.get("end"), f.get("val")
        if not (start and end) or val is None:
            continue
        d = _days(start, end)
        if d is None or not (QUARTER_MIN_DAYS <= d <= QUARTER_MAX_DAYS):
            continue
        key = period_key(end)
        if not key:
            continue
        filed = str(f.get("filed", ""))
        prev = out.get(key)
        if prev is None or filed >= prev[1]:
            out[key] = (float(val), filed)
    return {k: v[0] for k, v in out.items()}


def annual_flow(node: dict) -> dict:
    """{fiscal_year: value} for ~annual durations — needed to back out Q4."""
    out = {}
    for _unit, f in _iter_facts(node):
        form = str(f.get("form", ""))
        if not form.startswith("10-K"):
            continue
        start, end, val = f.get("start"), f.get("end"), f.get("val")
        if not (start and end) or val is None:
            continue
        d = _days(start, end)
        if d is None or not (ANNUAL_MIN_DAYS <= d <= ANNUAL_MAX_DAYS):
            continue
        year = int(str(end)[:4])
        filed = str(f.get("filed", ""))
        prev = out.get(year)
        if prev is None or filed >= prev[1]:
            out[year] = (float(val), filed)
    return {k: v[0] for k, v in out.items()}


def instant(node: dict) -> dict:
    """{period: value} for point-in-time facts (no start date)."""
    out = {}
    for _unit, f in _iter_facts(node):
        form = str(f.get("form", ""))
        if not (form.startswith("10-Q") or form.startswith("10-K")):
            continue
        end, val = f.get("end"), f.get("val")
        if not end or val is None or f.get("start"):
            continue
        key = period_key(end)
        if not key:
            continue
        filed = str(f.get("filed", ""))
        prev = out.get(key)
        if prev is None or filed >= prev[1]:
            out[key] = (float(val), filed)
    return {k: v[0] for k, v in out.items()}


def derive_q4(quarters: dict, annuals: dict) -> dict:
    """Fill missing Q4s as FY minus Q1+Q2+Q3.

    There is no Q4 10-Q, so a naive quarterly series has a hole every fourth
    period — which silently breaks TTM and any YoY comparison. Only fills when
    all three earlier quarters are present.
    """
    filled = dict(quarters)
    for year, fy in annuals.items():
        q4 = f"{year}Q4"
        if q4 in filled:
            continue
        parts = [filled.get(f"{year}Q{i}") for i in (1, 2, 3)]
        if any(p is None for p in parts):
            continue
        filled[q4] = fy - sum(parts)
    return filled


def ttm(series: dict, end_period: str | None = None):
    """Trailing four quarters ending at end_period (default: latest). None if
    four consecutive quarters aren't available."""
    if not series:
        return None
    keys = sorted(series)
    end_period = end_period or keys[-1]
    if end_period not in series:
        return None
    idx = keys.index(end_period)
    if idx < 3:
        return None
    window = keys[idx - 3: idx + 1]
    return sum(series[k] for k in window)


def latest_period(series: dict):
    return sorted(series)[-1] if series else None


def yoy(series: dict, period: str | None = None):
    """Year-over-year change vs the same quarter a year earlier."""
    if not series:
        return None
    period = period or latest_period(series)
    if not period or len(period) != 6:
        return None
    try:
        prior = f"{int(period[:4]) - 1}{period[4:]}"
    except ValueError:
        return None
    cur, prev = series.get(period), series.get(prior)
    if cur is None or prev in (None, 0):
        return None
    return cur / prev - 1.0


def _self_test():
    node = {"units": {"USD": [
        {"form": "10-Q", "start": "2026-01-01", "end": "2026-03-31",
         "val": 100, "filed": "2026-04-20"},
        {"form": "10-Q", "start": "2026-04-01", "end": "2026-06-30",
         "val": 110, "filed": "2026-07-20"},
        {"form": "10-Q", "start": "2026-07-01", "end": "2026-09-30",
         "val": 120, "filed": "2026-10-20"},
        # a year-to-date stub that must be rejected
        {"form": "10-Q", "start": "2026-01-01", "end": "2026-09-30",
         "val": 330, "filed": "2026-10-20"},
        # restatement of Q1, filed later -> should win
        {"form": "10-K", "start": "2026-01-01", "end": "2026-03-31",
         "val": 105, "filed": "2027-02-01"},
        {"form": "10-K", "start": "2026-01-01", "end": "2026-12-31",
         "val": 500, "filed": "2027-02-01"},
    ]}}

    q = quarterly_flow(node)
    assert q["2026Q2"] == 110 and q["2026Q3"] == 120, q
    print("  \u2713 discrete quarters extracted")
    assert "2026Q4" not in q or q.get("2026Q4") != 330
    assert len([k for k in q if k.startswith("2026")]) == 3, q
    print("  \u2713 year-to-date stub rejected (not mistaken for a quarter)")
    assert q["2026Q1"] == 105, "later-filed restatement must win"
    print("  \u2713 restatement supersedes original")

    a = annual_flow(node)
    assert a == {2026: 500}, a
    print("  \u2713 annual series isolated for Q4 derivation")

    filled = derive_q4(q, a)
    assert filled["2026Q4"] == 500 - (105 + 110 + 120), filled["2026Q4"]
    print("  \u2713 Q4 derived as FY minus Q1-Q3 (no Q4 10-Q exists)")

    assert ttm(filled, "2026Q4") == 500
    assert ttm(filled, "2026Q2") is None, "insufficient history must be None"
    print("  \u2713 TTM sums four quarters, None when short")

    inst = instant({"units": {"USD": [
        {"form": "10-Q", "end": "2026-06-30", "val": 900, "filed": "2026-07-20"},
        {"form": "10-Q", "start": "2026-04-01", "end": "2026-06-30",
         "val": 5, "filed": "2026-07-20"},
    ]}})
    assert inst == {"2026Q2": 900}, inst
    print("  \u2713 instants exclude duration facts")

    assert abs(yoy({"2025Q3": 100, "2026Q3": 120}, "2026Q3") - 0.2) < 1e-9
    print("  \u2713 YoY compares like quarter to like quarter")

    print("\nQUARTERLY CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
