"""
edgar_client.py — SEC EDGAR access with a rate cushion and a raw cache.

Two things every fundamentals call needs from SEC:
  * ticker -> CIK  (from the public company_tickers.json, cached)
  * CIK  -> companyfacts JSON  (all XBRL facts for the company)

SEC allows 10 requests/second per IP and *blocks* offenders for ~10 minutes. We
pace at ~5/s (a deliberate 50% cushion) and cache raw companyfacts to disk, so a
2000-name batch run — or a busy web service — never hammers EDGAR. SEC also
requires a contact email in the User-Agent; that's set below.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.request
from pathlib import Path

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = ssl.create_default_context()

# SEC requires a descriptive UA with contact info. Override via edgar_client
# module attribute or the SEC_USER_AGENT env var if you fork this.
import os
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT", "etf-backend fundamentals (Sam Braman sambraman12@gmail.com)")

SEC_MAX_RPS = 5.0                       # 50% cushion under SEC's 10/s cap — leave it
_MIN_INTERVAL = 1.0 / SEC_MAX_RPS
RAW_CACHE_DIR = Path(__file__).resolve().parent.parent / "raw_cache"
_TICKER_MAP_CACHE = RAW_CACHE_DIR / "company_tickers.json"

_lock = threading.Lock()
_last_request = [0.0]                    # mutable holder for the last-call clock


def _throttle():
    """Block just long enough to stay under SEC_MAX_RPS across threads."""
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


def _get(url: str, timeout: int = 45) -> bytes:
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT,
                                               "Accept-Encoding": "gzip, deflate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            # Likely a rate block — cool down a full cycle rather than hammering.
            print("Warning: SEC returned a rate/block response; cooling down 60s.")
            time.sleep(60)
        raise


def _ticker_map() -> dict:
    """TICKER -> zero-padded 10-digit CIK, cached to disk."""
    if _TICKER_MAP_CACHE.exists():
        try:
            return json.loads(_TICKER_MAP_CACHE.read_text())
        except Exception:
            pass
    raw = _get("https://www.sec.gov/files/company_tickers.json")
    data = json.loads(raw)
    out = {}
    for row in data.values():
        t = str(row.get("ticker", "")).upper()
        if t:
            out[t] = str(row["cik_str"]).zfill(10)
    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _TICKER_MAP_CACHE.write_text(json.dumps(out))
    except Exception:
        pass
    return out


def ticker_to_cik(ticker: str):
    return _ticker_map().get(ticker.upper())


def company_facts(ticker: str, max_age_hours: float = 24.0):
    """Return (cik, facts_dict) for a ticker, from the raw cache if fresh else SEC.
    Raises LookupError if the ticker isn't an SEC operating company (e.g. a
    foreign issuer with no us-gaap facts, or a fund)."""
    cik = ticker_to_cik(ticker)
    if not cik:
        raise LookupError(f"{ticker}: no CIK in SEC company_tickers.json")

    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_CACHE_DIR / f"CIK{cik}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600:
        try:
            return cik, json.loads(path.read_text())
        except Exception:
            pass

    raw = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    facts = json.loads(raw)
    try:
        path.write_text(json.dumps(facts))
    except Exception:
        pass
    return cik, facts


def company_submissions(cik: str, max_age_hours: float = 168.0) -> dict:
    """The filer's submissions metadata (sic/sicDescription, addresses, state of
    incorporation…). Cached a week — this rarely changes. Returns {} on failure
    so classification is best-effort and never blocks the financials."""
    cik = str(cik).zfill(10)
    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_CACHE_DIR / f"submissions_CIK{cik}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600:
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    try:
        data = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    except Exception:
        return {}
    try:
        path.write_text(json.dumps(data))
    except Exception:
        pass
    return data
