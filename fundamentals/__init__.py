"""
fundamentals — company fundamentals from SEC EDGAR, keyed by ticker.

The one function the web service calls:

    from fundamentals import get_fundamentals
    data = get_fundamentals("AAPL")          # EDGAR-only metrics (free)
    data = get_fundamentals("AAPL", with_price=True)   # + PE/PB/PS/PEG if a price is available

Returns a flat dict: identity (ticker, cik, name, fiscal_year) + every metric
from metrics.compute_metrics. Raises LookupError if the ticker isn't an SEC
operating company with XBRL facts (foreign issuers, funds, etc.).

This is the shared core: build_dataset.py uses it for the batch fundamentals.db,
and app.py's /fundamentals endpoint uses it live per ticker.
"""

from __future__ import annotations

from .edgar_client import company_facts, ticker_to_cik
from .extract import annual_series, latest_year
from .metrics import compute_metrics
from .prices import get_price

__all__ = ["get_fundamentals", "compute_metrics", "annual_series",
           "company_facts", "ticker_to_cik", "get_price"]


def get_fundamentals(ticker: str, with_price: bool = False,
                     price: float | None = None, max_age_hours: float = 24.0) -> dict:
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise LookupError("ticker is required")

    cik, facts = company_facts(ticker, max_age_hours=max_age_hours)
    if not ((facts.get("facts") or {}).get("us-gaap")):
        raise LookupError(f"{ticker}: no us-gaap XBRL facts (not a US filer?)")

    series = annual_series(facts)
    if price is None and with_price:
        price = get_price(ticker)
    metrics = compute_metrics(series, price=price)

    return {
        "ticker": ticker,
        "cik": int(cik),
        "name": facts.get("entityName", ""),
        "fiscal_year": latest_year(series),
        "price": round(price, 4) if isinstance(price, (int, float)) else None,
        **metrics,
    }
