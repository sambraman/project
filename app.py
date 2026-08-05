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
from holdings import get_holdings, HoldingsError, classify_issuer
from fundamentals import get_fundamentals, get_classification, get_history
from store import FundamentalsStore, DEFAULT_PATH as STORE_PATH
import refresh as refresh_mod

BATCH_MAX = 60          # tickers per POST /fundamentals request
BATCH_WORKERS = 8       # concurrent lookups (SEC's own throttle still paces to ~5/s)

app = FastAPI(title="ETF Holdings Backend", version="1.0")

origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=origins or ["*"],
    allow_methods=["*"], allow_headers=["*"],
)

CACHE = HoldingsCache()
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

    Cache-first: a cached result returns instantly. On a miss (or ?refresh=true)
    it fetches from the issuer, caches, and returns. Unknown tickers route to the
    N-PORT fallback (flagged is_stale) rather than erroring.
    """
    ticker = ticker.upper().strip()
    if not refresh:
        cached = CACHE.get(ticker)
        if cached:
            cached["cached"] = True
            return cached
    try:
        result = get_holdings(ticker, offline=OFFLINE)
    except HoldingsError as e:
        raise HTTPException(status_code=404, detail=str(e))
    CACHE.put(result)
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
