"""
app.py — the FastAPI web layer.

Thin wrapper over holdings.get_holdings + the SQLite cache. Serving is separate
from the logic on purpose: the holdings function is fully usable (and testable)
without ever starting this server — see holdings.py's CLI and smoke_test.py.

    uvicorn app:app --reload        # http://127.0.0.1:8000/docs

Endpoints
  GET  /health                 liveness
  GET  /holdings?ticker=IVV     full holdings (cache-first, live on miss)
  GET  /tickers                 what's cached and how fresh
  POST /refresh                 refresh tracked tickers (needs x-refresh-token)
  GET  /kpis?ticker=MSFT        sector-specific KPIs (capex/RPO, NIM, combined ratio)
  GET  /stats                   what's in the datastore + freshness (ops)
  GET  /prices?ticker=IVV       daily price history (omit ticker for a summary)
  POST /refresh-prices          pull daily EOD bars (needs x-refresh-token)

Set OFFLINE=1 to serve everything from the bundled fixtures (handy for a demo
with no network).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel

from cache import HoldingsCache
from price_cache import PriceCache
from holdings import get_holdings, HoldingsError, classify_issuer
from fundamentals import get_fundamentals, get_classification, get_history
from store import FundamentalsStore, DEFAULT_PATH as STORE_PATH
from sector_kpis import compute_sector_kpis
from datastore import STORE as DATA_STORE   # unified cache; NOT FundamentalsStore
import refresh as refresh_mod
import refresh_prices as refresh_prices_mod

BATCH_MAX = 60          # tickers per POST /fundamentals request
BATCH_WORKERS = 8       # concurrent lookups (SEC's own throttle still paces to ~5/s)

app = FastAPI(title="ETF Holdings Backend", version="1.0")

origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=origins or ["*"],
    allow_methods=["*"], allow_headers=["*"],
)

CACHE = HoldingsCache()
PRICE_CACHE = PriceCache()
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")
OFFLINE = os.environ.get("OFFLINE", "").lower() in ("1", "true", "yes", "on")

# The catalogued fundamentals dataset (committed at data/fundamentals.db). Opened
# read-only if present; endpoints fall back to live SEC for anything not in it.
STORE = FundamentalsStore(read_only=True) if STORE_PATH.exists() else None


@app.get("/health")
def health():
    return {"status": "ok", "offline": OFFLINE,
            "catalog": (STORE.stats() if STORE else {"companies": 0})}


@app.get("/holdings")
def holdings(ticker: str = Query(..., min_length=1),
             refresh: bool = Query(False, description="skip cache, fetch live")):
    """Full holdings for a ticker, tagged with the issuer's as-of date.

    Store-first. A stored result returns with zero network calls. On a cold miss
    (or ?refresh=true) it fetches, persists, and returns. If a live fetch fails
    but we hold a previous copy, the STALE COPY IS SERVED rather than a 5xx —
    dated data beats an error page.
    """
    ticker = ticker.upper().strip()
    if not refresh:
        stored = DATA_STORE.get_holdings(ticker)
        if stored:
            stored["issuer_route"] = classify_issuer(ticker)
            return stored
        cached = CACHE.get(ticker)
        if cached:
            cached["cached"] = True
            return cached
    try:
        result = get_holdings(ticker, offline=OFFLINE)
    except HoldingsError as e:
        # Last resort: anything previously stored beats a hard failure.
        stored = DATA_STORE.get_holdings(ticker)
        if stored:
            stored["issuer_route"] = classify_issuer(ticker)
            stored["warnings"] = list(stored.get("warnings") or []) + [
                f"Live fetch failed ({e}); serving the last stored copy."]
            return stored
        raise HTTPException(status_code=404, detail=str(e))
    CACHE.put(result)
    try:
        DATA_STORE.put_holdings(result)
    except Exception:
        pass                      # persistence must never break a response
    payload = result.as_dict()
    payload["cached"] = False
    payload["issuer_route"] = classify_issuer(ticker)
    return payload


@app.get("/fundamentals")
def fundamentals(ticker: str = Query(..., min_length=1),
                 with_price: bool = Query(False, description="also compute PE/PB/PS "
                                          "(needs EODHD_API_KEY or yfinance)")):
    """Company fundamentals from SEC EDGAR for one ticker — valuation,
    profitability, growth, and leverage metrics. This is the join partner to
    /holdings: the frontend looks up each underlying holding's metrics here.

    Always live from SEC (companyfacts are cached to raw_cache/ for a day). Not
    affected by OFFLINE — there are no fundamentals fixtures; run build_dataset.py
    if you want a prebuilt fundamentals.db instead."""
    try:
        return get_fundamentals(ticker, with_price=with_price)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"fundamentals fetch failed: {e}")


class FundamentalsBatch(BaseModel):
    tickers: list[str]
    mode: str = "classification"     # "classification" (fast, sector/geo) | "full"
    with_price: bool = False         # only used when mode == "full"


@app.post("/fundamentals")
def fundamentals_batch(req: FundamentalsBatch):
    """Enrich many tickers in one request — for the holdings table's sector/
    country columns and the By-Sector view.

    mode="classification" (default) returns just ticker/name/sector/industry/
    country/hq using one cheap, week-cached SEC submissions call per ticker —
    far lighter than the full financials. mode="full" returns everything
    /fundamentals?ticker= returns. Capped at BATCH_MAX tickers; unknown/foreign
    tickers come back as null rather than failing the batch."""
    seen, tickers = set(), []
    for t in req.tickers:
        t = (t or "").strip().upper()
        if t and t not in seen:
            seen.add(t); tickers.append(t)
    if len(tickers) > BATCH_MAX:
        raise HTTPException(status_code=413,
                            detail=f"too many tickers ({len(tickers)}); max {BATCH_MAX} per request")

    def one(t):
        try:
            if req.mode == "full":
                return t, get_fundamentals(t, with_price=req.with_price)
            return t, get_classification(t)
        except Exception:
            return t, None

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        results = dict(pool.map(one, tickers))
    return {"mode": req.mode, "count": len(results), "results": results}


@app.get("/company")
def company(ticker: str = Query(..., min_length=1)):
    """Full multi-year (up to 10) fundamentals for one company, for the
    company-search page. Served from the committed catalog when present (instant,
    no SEC call); otherwise computed live from SEC and returned the same shape.
    Includes `coverage_pct` — the % of expected data points actually mapped."""
    ticker = ticker.upper().strip()
    if STORE:
        hit = STORE.get_company(ticker)
        if hit:
            hit["source"] = "catalog"
            return hit
    try:
        data = get_history(ticker)
        data["source"] = "live"
        return data
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"history fetch failed: {e}")


@app.get("/search")
def search(q: str = Query(..., min_length=1, description="ticker or company name"),
           limit: int = Query(20, ge=1, le=100)):
    """Search the catalogued companies by ticker or name (for the search box).
    Empty if no catalog has been built/committed yet."""
    if not STORE:
        return {"query": q, "results": [], "note": "no catalog built yet — run build_dataset.py"}
    return {"query": q, "results": STORE.search(q, limit=limit)}


@app.get("/tickers")
def tickers():
    return {"tickers": CACHE.list_tickers()}


@app.post("/refresh")
def do_refresh(x_refresh_token: str = Header(default="")):
    if not REFRESH_TOKEN or x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing x-refresh-token")
    results = refresh_mod.refresh(offline=OFFLINE, cache=CACHE)
    return {"refreshed": results}


@app.get("/kpis")
def kpis(ticker: str = Query(..., min_length=1),
         sector: str = Query(default="", description="override auto-detection: "
                             "hyperscalers | banks | insurance | semiconductors "
                             "| reits | utilities")):
    """Sector-specific KPIs from XBRL — the metrics an analyst covering that
    vertical actually pulls (hyperscaler capex/RPO, bank NIM/efficiency,
    insurance combined ratio) rather than sector-blind generic ratios.

    Every KPI carries `basis` (tag | derived | unavailable) and the `tag` it was
    read from, so each number is auditable. `coverage` is the share that
    resolved. Read `warnings` — they flag approximations (bank NIM) and data
    that genuinely isn't in companyfacts (cloud segment revenue).
    """
    t = ticker.upper().strip()
    # Store-first: filings are quarterly, so a cached KPI payload is valid for
    # a week. This turns a multi-second EDGAR walk into a single SQLite read.
    if sector:
        hit = DATA_STORE.get_kpis(t, sector)
        if hit and hit.get("fresh"):
            return hit
    try:
        res = compute_sector_kpis(t, sector=sector or None)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        for sec in (sector, "hyperscalers", "banks", "insurance"):
            hit = DATA_STORE.get_kpis(t, sec) if sec else None
            if hit:
                hit["warnings"] = list(hit.get("warnings") or []) + [
                    f"Live computation failed ({type(e).__name__}); serving "
                    f"the last stored copy."]
                return hit
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
    payload = res.as_dict()
    try:
        DATA_STORE.put_kpis(t, res.sector, res.period, payload)
    except Exception:
        pass
    return payload


@app.get("/stats")
def stats():
    """What's actually in the store and how fresh — the ops view. Use this to
    confirm the nightly refresh is landing instead of guessing from the UI."""
    return DATA_STORE.stats()


@app.get("/prices")
def prices(ticker: str = Query(default="", description="omit for a per-ticker summary"),
           limit: int = Query(default=30, ge=1, le=365)):
    if not ticker:
        return {"tickers": PRICE_CACHE.list_tickers()}
    return {"ticker": ticker.upper(),
            "bars": PRICE_CACHE.get_history(ticker, limit=limit)}


@app.post("/refresh-prices")
def do_refresh_prices(x_refresh_token: str = Header(default=""),
                      days: int = Query(default=7, ge=1, le=365)):
    if not REFRESH_TOKEN or x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing x-refresh-token")
    results = refresh_prices_mod.refresh_prices(cache=PRICE_CACHE, days=days)
    return {"priced": results}
