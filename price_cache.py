"""
price_cache.py — a tiny SQLite cache for daily price bars (stdlib sqlite3).

The price sibling of cache.py (holdings). Stores an OHLCV time series, one row
per (ticker, date), so re-running a day just upserts. The nightly price job
(refresh_prices.py) writes here; the web layer reads it via GET /prices. Lives
at prices.db, which is gitignored (like holdings.db) and rebuilt at runtime.

    pc = PriceCache()
    pc.put_bars("IVV", bars)          # bars = fundamentals.prices.get_eod_bars(...)
    pc.get_history("IVV", limit=30)   # -> newest-first list of dicts
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "prices.db"


class PriceCache:
    def __init__(self, path=DEFAULT_DB):
        self.path = str(path)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prices (
                    ticker     TEXT NOT NULL,
                    date       TEXT NOT NULL,
                    open       REAL,
                    high       REAL,
                    low        REAL,
                    close      REAL,
                    volume     INTEGER,
                    source     TEXT,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (ticker, date)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker)")

    def put_bars(self, ticker: str, bars) -> int:
        """Upsert a list of OHLCV bar dicts for one ticker. Idempotent: a repeat
        run on the same dates overwrites, never duplicates."""
        if not bars:
            return 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t = ticker.upper()
        rows = [(t, b.get("date"), b.get("open"), b.get("high"), b.get("low"),
                 b.get("close"), b.get("volume"), b.get("source"), now)
                for b in bars if b.get("date")]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO prices "
                "(ticker,date,open,high,low,close,volume,source,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def get_history(self, ticker: str, limit: int = 30):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date,open,high,low,close,volume,source FROM prices "
                "WHERE ticker=? ORDER BY date DESC LIMIT ?",
                (ticker.upper(), limit)).fetchall()
        return [dict(r) for r in rows]

    def latest(self, ticker: str):
        h = self.get_history(ticker, limit=1)
        return h[0] if h else None

    def list_tickers(self):
        """Freshness summary per ticker (for GET /prices with no ticker)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ticker, MAX(date) AS latest, COUNT(*) AS bars, "
                "MAX(fetched_at) AS fetched_at FROM prices "
                "GROUP BY ticker ORDER BY ticker").fetchall()
        return [dict(r) for r in rows]
