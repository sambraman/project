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

from .edgar_client import company_facts, company_submissions, ticker_to_cik
from .extract import annual_series, latest_year
from .metrics import compute_metrics
from .prices import get_price
from .classify import sic_to_sector, resolve_country

__all__ = ["get_fundamentals", "get_classification", "compute_metrics",
           "annual_series", "company_facts", "ticker_to_cik", "get_price"]


def _classification(cik: str) -> dict:
    """sector / industry / country / HQ / name from SEC submissions (best-effort)."""
    sub = company_submissions(cik)
    if not sub:
        return {"name": "", "sector": "", "industry": "", "country": "", "hq": "",
                "state_of_incorporation": ""}
    biz = (sub.get("addresses") or {}).get("business") or {}
    soc = sub.get("stateOfIncorporation") or ""
    state_or_country = biz.get("stateOrCountry") or ""
    city = biz.get("city") or ""
    return {
        "name": sub.get("name") or "",
        "sector": sic_to_sector(sub.get("sic")),
        "industry": sub.get("sicDescription") or "",
        "country": resolve_country(state_or_country, soc),
        "hq": ", ".join(p for p in (city.title() if city else "", state_or_country) if p),
        "state_of_incorporation": soc,
    }


def get_classification(ticker: str) -> dict:
    """Lightweight sector/geography only — one cheap, week-cached SEC submissions
    call (no companyfacts download). This is what the holdings list's sector/
    country columns and the By-Sector view need. Raises LookupError if the ticker
    has no CIK (foreign issuer / fund)."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise LookupError("ticker is required")
    cik = ticker_to_cik(ticker)
    if not cik:
        raise LookupError(f"{ticker}: no CIK in SEC company_tickers.json")
    return {"ticker": ticker, "cik": int(cik), **_classification(cik)}


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

    data = {
        "ticker": ticker,
        "cik": int(cik),
        "fiscal_year": latest_year(series),
        "price": round(price, 4) if isinstance(price, (int, float)) else None,
        **_classification(cik),
        **metrics,
    }
    data["name"] = data.get("name") or facts.get("entityName", "")
    return data
