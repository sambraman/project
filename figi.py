"""
figi.py — map a holding's CUSIP/ISIN to a ticker via the OpenFIGI API.

N-PORT (and some other feeds) identify securities by CUSIP/ISIN, not ticker.
OpenFIGI resolves those to tickers. This module enriches a list of holding rows
in place, filling row["ticker"] where it can.

    from figi import enrich_tickers
    enrich_tickers(rows)   # rows: [{"cusip", "isin", "ticker", ...}, ...]

Config
------
* Works with **no key** at OpenFIGI's anonymous rate (small batches, slower).
* Set ``OPENFIGI_API_KEY`` for larger batches and a higher rate limit — strongly
  recommended for funds with hundreds of holdings. Get a free key at
  https://www.openfigi.com/api (Account -> API key).

Results are cached to ``.figi_cache.json`` (CUSIP/ISIN -> ticker), so a ticker is
only ever looked up once across all funds and re-runs are instant.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request
from pathlib import Path

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = ssl.create_default_context()

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
_CACHE_PATH = Path(__file__).resolve().parent / ".figi_cache.json"

# US composite / primary exchange codes, preferred when a CUSIP maps to listings
# on several exchanges (we want the US ticker, e.g. AAPL not APC.DE).
_US_EXCH = {"US", "UN", "UW", "UQ", "UA", "UR", "UP", "UV", "UT", "PQ"}


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    try:
        _CACHE_PATH.write_text(json.dumps(cache))
    except Exception:
        pass


def _pick_ticker(matches: list) -> str:
    """Choose the best ticker from OpenFIGI's matches: prefer a US-listed equity."""
    if not matches:
        return ""
    for m in matches:
        if m.get("exchCode") in _US_EXCH and m.get("ticker"):
            return str(m["ticker"]).strip().upper()
    return str(matches[0].get("ticker") or "").strip().upper()


def _post(jobs: list) -> list:
    headers = {"Content-Type": "application/json", "User-Agent": "etf-backend"}
    key = os.environ.get("OPENFIGI_API_KEY")
    if key:
        headers["X-OPENFIGI-APIKEY"] = key
    req = urllib.request.Request(OPENFIGI_URL, data=json.dumps(jobs).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
        return json.load(r)


def enrich_tickers(rows: list) -> list:
    """Fill row['ticker'] for rows that have a CUSIP or ISIN but no ticker.
    Mutates and returns `rows`. Never raises — enrichment is best-effort, so a
    rate limit or outage just leaves some tickers blank."""
    cache = _load_cache()
    key = os.environ.get("OPENFIGI_API_KEY")
    batch_size = 100 if key else 10          # OpenFIGI's per-request job limit

    # Collect the unique ids we still need to resolve.
    pending, pending_keys = [], []
    seen = set()
    for row in rows:
        if row.get("ticker"):
            continue
        cusip, isin = row.get("cusip", ""), row.get("isin", "")
        ck = f"C:{cusip}" if cusip else (f"I:{isin}" if isin else "")
        if not ck:
            continue
        if ck in cache:
            row["ticker"] = cache[ck]
            continue
        if ck in seen:
            continue
        seen.add(ck)
        if cusip:
            pending.append({"idType": "ID_CUSIP", "idValue": cusip})
        else:
            pending.append({"idType": "ID_ISIN", "idValue": isin})
        pending_keys.append(ck)

    # Resolve the misses in batches, updating the cache as we go.
    resolved = {}
    for i in range(0, len(pending), batch_size):
        jobs = pending[i:i + batch_size]
        keys = pending_keys[i:i + batch_size]
        try:
            results = _post(jobs)
        except Exception as e:
            print(f"Note: OpenFIGI lookup stopped early ({type(e).__name__}); "
                  f"{len(pending) - i} ids left unmapped this run.")
            break
        for ck, res in zip(keys, results):
            tk = _pick_ticker(res.get("data") or [])
            resolved[ck] = tk
            cache[ck] = tk
        if not key:
            time.sleep(2.6)                  # stay under the anonymous ~25/min cap

    if resolved:
        _save_cache(cache)

    # Second pass: apply everything (cache hits found mid-loop + freshly resolved).
    for row in rows:
        if row.get("ticker"):
            continue
        cusip, isin = row.get("cusip", ""), row.get("isin", "")
        ck = f"C:{cusip}" if cusip else (f"I:{isin}" if isin else "")
        if ck and cache.get(ck):
            row["ticker"] = cache[ck]
    return rows
