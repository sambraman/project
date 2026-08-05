"""
metrics.py — annual series -> valuation / profitability / growth / leverage
metrics, for a specific fiscal year or as a full multi-year history.

Everything is pure arithmetic on the {field: {year: value}} series from
extract.py, so it's fully unit-testable offline. Growth/CAGR compare a year to
year-1 / year-3 by actual fiscal year (not list position), so gaps in a
company's filing history don't corrupt a YoY. Price-based multiples
(PE/PB/PS/PEG/earnings-yield) fill only when a `price` is supplied — EDGAR has no
market price — and are only meaningful for the latest year.
"""

from __future__ import annotations

from .extract import all_years, latest_year


def _div(a, b):
    if a is None or b is None:
        return None
    try:
        if abs(b) < 1e-12:
            return None
        return a / b
    except (TypeError, ZeroDivisionError):
        return None


def _growth(cur, prev):
    if cur is None or prev is None or abs(prev) < 1e-12:
        return None
    return (cur - prev) / abs(prev)


def _round(x, n=6):
    return round(x, n) if isinstance(x, (int, float)) else x


def _at(series: dict, field: str, year: int):
    return series.get(field, {}).get(year)


# The metric keys every year-row carries (order preserved for CSV/DB columns).
METRIC_KEYS = [
    "revenue", "cost_of_revenue", "gross_profit", "operating_income",
    "net_income", "interest_expense", "assets", "equity", "debt",
    "shares_outstanding",
    "net_margin", "gross_margin", "operating_margin", "roe", "roa",
    "revenue_growth_yoy", "earnings_growth_yoy", "eps_growth_yoy", "revenue_cagr_3y",
    "debt_to_equity", "debt_to_assets", "liabilities_to_assets", "current_ratio",
    "interest_coverage", "eps_diluted", "book_value_per_share", "revenue_per_share",
    "pe_ratio", "pb_ratio", "ps_ratio", "peg_ratio", "earnings_yield",
]


def metrics_for_year(series: dict, year: int, price: float | None = None) -> dict:
    """All metrics as of fiscal `year`. Growth vs year-1; 3y CAGR vs year-3."""
    rev = _at(series, "revenue", year)
    rev_prev = _at(series, "revenue", year - 1)
    rev_3y = _at(series, "revenue", year - 3)
    ni = _at(series, "net_income", year)
    ni_prev = _at(series, "net_income", year - 1)
    assets = _at(series, "assets", year)
    assets_cur = _at(series, "assets_current", year)
    liab = _at(series, "liabilities", year)
    liab_cur = _at(series, "liabilities_current", year)
    equity = _at(series, "equity", year)
    ltd = _at(series, "long_term_debt", year) or 0.0
    std = _at(series, "short_term_debt", year) or 0.0
    debt = (ltd + std) or None
    shares = _at(series, "shares_outstanding", year)
    op_inc = _at(series, "operating_income", year)
    int_exp = _at(series, "interest_expense", year)
    eps = _at(series, "eps_diluted", year) or _at(series, "eps_basic", year)
    eps_prev = _at(series, "eps_diluted", year - 1) or _at(series, "eps_basic", year - 1)

    gross = _at(series, "gross_profit", year)
    if gross is None:
        cogs = _at(series, "cost_of_revenue", year)
        gross = (rev - cogs) if (rev is not None and cogs is not None) else None

    bvps = _div(equity, shares)
    rps = _div(rev, shares)
    earnings_growth = _growth(ni, ni_prev)
    revenue_cagr_3y = None
    if rev is not None and rev_3y and rev_3y > 0 and rev > 0:
        revenue_cagr_3y = (rev / rev_3y) ** (1 / 3) - 1

    m = {
        "revenue": rev, "cost_of_revenue": _at(series, "cost_of_revenue", year),
        "gross_profit": gross, "operating_income": op_inc, "net_income": ni,
        "interest_expense": int_exp, "assets": assets, "equity": equity,
        "debt": debt, "shares_outstanding": shares,
        "net_margin": _div(ni, rev),
        "gross_margin": _div(gross, rev),
        "operating_margin": _div(op_inc, rev),
        "roe": _div(ni, equity),
        "roa": _div(ni, assets),
        "revenue_growth_yoy": _growth(rev, rev_prev),
        "earnings_growth_yoy": earnings_growth,
        "eps_growth_yoy": _growth(eps, eps_prev),
        "revenue_cagr_3y": revenue_cagr_3y,
        "debt_to_equity": _div(debt, equity),
        "debt_to_assets": _div(debt, assets),
        "liabilities_to_assets": _div(liab, assets),
        "current_ratio": _div(assets_cur, liab_cur),
        "interest_coverage": _div(op_inc, abs(int_exp)) if int_exp else None,
        "eps_diluted": eps,
        "book_value_per_share": bvps,
        "revenue_per_share": rps,
        "pe_ratio": None, "pb_ratio": None, "ps_ratio": None,
        "peg_ratio": None, "earnings_yield": None,
    }
    if price is not None and price > 0:
        m["pe_ratio"] = _div(price, eps) if (eps and eps > 0) else None
        m["pb_ratio"] = _div(price, bvps) if (bvps and bvps > 0) else None
        m["ps_ratio"] = _div(price, rps) if (rps and rps > 0) else None
        m["earnings_yield"] = _div(eps, price) if eps else None
        if m["pe_ratio"] and earnings_growth and earnings_growth > 0:
            m["peg_ratio"] = m["pe_ratio"] / (earnings_growth * 100)

    return {k: _round(v) for k, v in m.items()}


def compute_metrics(series: dict, price: float | None = None) -> dict:
    """Metrics for the most recent fiscal year (back-compat wrapper)."""
    y = latest_year(series)
    if y is None:
        return {k: None for k in METRIC_KEYS}
    return metrics_for_year(series, y, price=price)


def compute_history(series: dict, max_years: int = 10) -> list:
    """The last `max_years` fiscal years, newest first: [{fiscal_year, ...metrics}]."""
    years = all_years(series)[-max_years:]
    rows = [{"fiscal_year": y, **metrics_for_year(series, y)} for y in years]
    rows.sort(key=lambda r: r["fiscal_year"], reverse=True)
    return rows
