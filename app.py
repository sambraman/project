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

from cache import HoldingsCache
from holdings import get_holdings, HoldingsError, classify_issuer
import refresh as refresh_mod

app = FastAPI(title="ETF Holdings Backend", version="1.0")

origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=origins or ["*"],
    allow_methods=["*"], allow_headers=["*"],
)

CACHE = HoldingsCache()
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")
OFFLINE = os.environ.get("OFFLINE", "").lower() in ("1", "true", "yes", "on")


@app.get("/health")
def health():
    return {"status": "ok", "offline": OFFLINE}


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


@app.get("/tickers")
def tickers():
    return {"tickers": CACHE.list_tickers()}


@app.post("/refresh")
def do_refresh(x_refresh_token: str = Header(default="")):
    if not REFRESH_TOKEN or x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing x-refresh-token")
    results = refresh_mod.refresh(offline=OFFLINE, cache=CACHE)
    return {"refreshed": results}
