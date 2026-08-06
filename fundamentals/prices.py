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


# --- daily end-of-day bars (for the nightly price-history job) --------------- #
# get_price() above returns one live quote to finish valuation multiples. The
# price job instead wants a daily OHLCV time series, so it uses EODHD's EOD
# endpoint (yfinance history as the dev fallback). Same stdlib-only stack; the
# close is the split/dividend-adjusted close when EODHD provides it.

from datetime import date, timedelta


def _eodhd_eod_bars(ticker: str, days: int):
    key = os.environ.get("EODHD_API_KEY")
    if not key:
        return None
    frm = (date.today() - timedelta(days=days)).isoformat()
    url = (f"https://eodhd.com/api/eod/{ticker.upper()}.US"
           f"?api_token={key}&fmt=json&order=d&from={frm}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "etf-backend"})
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as r:
            data = json.load(r)
    except Exception:
        return None
    bars = []
    for row in data or []:
        close = row.get("adjusted_close")
        if close in (None, "NA", ""):
            close = row.get("close")
        bars.append({
            "date": row.get("date"), "open": row.get("open"),
            "high": row.get("high"), "low": row.get("low"),
            "close": close, "volume": row.get("volume"), "source": "eodhd",
        })
    return bars or None


def _yfinance_eod_bars(ticker: str, days: int):
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        hist = yf.Ticker(ticker).history(period=f"{max(days, 1)}d")
    except Exception:
        return None
    bars = []
    for idx, row in hist.iterrows():
        bars.append({
            "date": idx.date().isoformat(),
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]),
            "volume": int(row["Volume"]), "source": "yfinance",
        })
    # newest-first, to match the EODHD path
    return list(reversed(bars)) or None


def get_eod_bars(ticker: str, days: int = 7):
    """Recent daily OHLCV bars, newest-first, or None. EODHD then yfinance."""
    return _eodhd_eod_bars(ticker, days) or _yfinance_eod_bars(ticker, days)
