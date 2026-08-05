"""
extract.py — turn a companyfacts payload into clean annual series.

For each logical field in concepts.FIELDS we walk its candidate US-GAAP concepts
(and the dei namespace for share counts), keep only annual 10-K facts, dedupe to
one value per fiscal year (the most recently *filed* wins, so restatements
supersede originals), and return {field: {year: value}}.

Flow fields (revenue, net income, EPS…) require a ~1-year duration so we don't
pick up quarterly or year-to-date stubs. Stock fields (assets, equity, shares…)
are point-in-time, keyed by their period-end year.
"""

from __future__ import annotations

from datetime import date

from .concepts import FIELDS


def _days(start: str, end: str):
    try:
        y1, m1, d1 = map(int, start.split("-"))
        y2, m2, d2 = map(int, end.split("-"))
        return (date(y2, m2, d2) - date(y1, m1, d1)).days
    except Exception:
        return None


def _annual_from_units(units: dict, kind: str) -> dict:
    """Collapse a concept's per-unit fact lists into {year: value}."""
    by_year = {}   # year -> (value, filed_date_str)
    for facts in units.values():
        for f in facts:
            if not str(f.get("form", "")).startswith("10-K"):
                continue
            end = f.get("end")
            val = f.get("val")
            if not end or val is None:
                continue
            if kind == "flow":
                start = f.get("start")
                d = _days(start, end) if start else None
                if d is None or d < 350 or d > 380:
                    continue
            year = int(str(end)[:4])
            filed = str(f.get("filed", ""))
            prev = by_year.get(year)
            if prev is None or filed >= prev[1]:
                by_year[year] = (float(val), filed)
    return {y: v[0] for y, v in by_year.items()}


def annual_series(facts: dict) -> dict:
    """Return {field: {year: value}} for every field we can populate."""
    ns = facts.get("facts") or {}
    gaap = ns.get("us-gaap") or {}
    dei = ns.get("dei") or {}
    series = {}
    for field, (candidates, kind) in FIELDS.items():
        picked = {}
        for concept in candidates:
            node = gaap.get(concept) or dei.get(concept)
            if not node:
                continue
            picked = _annual_from_units(node.get("units") or {}, kind)
            if picked:
                break
        series[field] = picked
    return series


def latest(series_field: dict, offset: int = 0):
    """The value `offset` years back from the most recent (0 = latest). None if
    that year isn't present."""
    if not series_field:
        return None
    years = sorted(series_field)
    idx = len(years) - 1 - offset
    return series_field[years[idx]] if 0 <= idx < len(years) else None


def latest_year(series: dict):
    """The most recent fiscal year across the revenue/net-income series."""
    years = set()
    for f in ("revenue", "net_income", "assets"):
        years.update(series.get(f, {}).keys())
    return max(years) if years else None
