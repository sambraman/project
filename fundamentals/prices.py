"""
prices.py — a price for one ticker, to finish the valuation multiples.

EDGAR has no market price, so PE/PB/PS/PEG/earnings-yield need one from
elsewhere. Order: EODHD (licensable, one vendor for LookThrough) when
EODHD_API_KEY is set; otherwise yfinance if installed (free dev fallback);
otherwise None (the multiples just stay blank).
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = ssl.create_default_context()


def _eodhd_price(ticker: str):
    key = os.environ.get("EODHD_API_KEY")
    if not key:
        return None
    url = (f"https://eodhd.com/api/real-time/{ticker.upper()}.US"
           f"?api_token={key}&fmt=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "etf-backend"})
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as r:
            data = json.load(r)
        price = data.get("close") or data.get("previousClose")
        return float(price) if price not in (None, "NA", "") else None
    except Exception:
        return None


def _yfinance_price(ticker: str):
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        fi = yf.Ticker(ticker).fast_info
        price = fi.get("last_price") or fi.get("lastPrice")
        return float(price) if price else None
    except Exception:
        return None


def get_price(ticker: str):
    """Best available live price for a ticker, or None."""
    return _eodhd_price(ticker) or _yfinance_price(ticker)
