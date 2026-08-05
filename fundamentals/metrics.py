"""
metrics.py — annual series -> the valuation / profitability / growth / leverage
metrics the look-through app displays.

Everything here is pure arithmetic on the series from extract.py, so it's fully
unit-testable offline. Price-based multiples (PE/PB/PS/PEG/earnings-yield) are
filled only when a `price` is supplied; without one they're left None, exactly
as the README describes (EDGAR has no market price).
"""

from __future__ import annotations

from .extract import latest, latest_year


def _div(a, b):
    """Safe divide: None if either side is missing or the denominator ~0."""
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


def compute_metrics(series: dict, price: float | None = None) -> dict:
    rev = latest(series["revenue"])
    rev_prev = latest(series["revenue"], 1)
    rev_3y = latest(series["revenue"], 3)
    ni = latest(series["net_income"])
    ni_prev = latest(series["net_income"], 1)
    assets = latest(series["assets"])
    assets_cur = latest(series["assets_current"])
    liab = latest(series["liabilities"])
    liab_cur = latest(series["liabilities_current"])
    equity = latest(series["equity"])
    ltd = latest(series["long_term_debt"]) or 0.0
    std = latest(series["short_term_debt"]) or 0.0
    debt = (ltd + std) or None
    shares = latest(series["shares_outstanding"])
    op_inc = latest(series["operating_income"])
    int_exp = latest(series["interest_expense"])
    eps = latest(series["eps_diluted"]) or latest(series["eps_basic"])
    eps_prev = latest(series["eps_diluted"], 1) or latest(series["eps_basic"], 1)

    gross = latest(series["gross_profit"])
    if gross is None:
        cogs = latest(series["cost_of_revenue"])
        gross = (rev - cogs) if (rev is not None and cogs is not None) else None

    bvps = _div(equity, shares)
    rps = _div(rev, shares)

    revenue_growth = _growth(rev, rev_prev)
    earnings_growth = _growth(ni, ni_prev)
    revenue_cagr_3y = None
    if rev is not None and rev_3y and rev_3y > 0 and rev > 0:
        revenue_cagr_3y = (rev / rev_3y) ** (1 / 3) - 1

    m = {
        # raw magnitudes (sanity-check columns)
        "revenue": rev, "net_income": ni, "assets": assets, "equity": equity,
        "debt": debt, "shares_outstanding": shares,
        # profitability
        "net_margin": _div(ni, rev),
        "gross_margin": _div(gross, rev),
        "operating_margin": _div(op_inc, rev),
        "roe": _div(ni, equity),
        "roa": _div(ni, assets),
        # growth
        "revenue_growth_yoy": revenue_growth,
        "earnings_growth_yoy": earnings_growth,
        "eps_growth_yoy": _growth(eps, eps_prev),
        "revenue_cagr_3y": revenue_cagr_3y,
        # leverage
        "debt_to_equity": _div(debt, equity),
        "debt_to_assets": _div(debt, assets),
        "liabilities_to_assets": _div(liab, assets),
        "current_ratio": _div(assets_cur, liab_cur),
        "interest_coverage": _div(op_inc, abs(int_exp)) if int_exp else None,
        # per-share
        "eps_diluted": eps,
        "book_value_per_share": bvps,
        "revenue_per_share": rps,
        # valuation multiples (need a price)
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
