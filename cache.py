"""
cache.py — a tiny SQLite cache for holdings results (stdlib `sqlite3`).

One row per ticker, holding the full HoldingsResult as JSON plus a fetched-at
timestamp. The web layer reads from here for instant responses and only falls
through to a live fetch on a miss; the nightly refresh writes every tracked
ticker in.

    cache = HoldingsCache("holdings.db")
    cache.put(result)                 # result is a holdings.HoldingsResult
    hit = cache.get("IVV")            # -> dict (with _fetched_at) or None
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "holdings.db"


class HoldingsCache:
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
                CREATE TABLE IF NOT EXISTS holdings (
                    ticker     TEXT PRIMARY KEY,
                    as_of      TEXT,
                    source     TEXT,
                    is_stale   INTEGER,
                    count      INTEGER,
                    payload    TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
            """)

    def put(self, result):
        """Store a holdings.HoldingsResult (or any object with .as_dict())."""
        d = result.as_dict()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO holdings "
                "(ticker, as_of, source, is_stale, count, payload, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (d["ticker"], d["as_of"], d["source"], int(d["is_stale"]),
                 d["count"], json.dumps(d), now),
            )

    def get(self, ticker):
        """Return the cached payload dict (with `_fetched_at`) or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM holdings WHERE ticker = ?",
                (ticker.upper(),),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["_fetched_at"] = row["fetched_at"]
        return payload

    def list_tickers(self):
        """Freshness summary for every cached ticker (for GET /tickers)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ticker, as_of, source, is_stale, count, fetched_at "
                "FROM holdings ORDER BY ticker"
            ).fetchall()
        return [
            {"ticker": r["ticker"], "as_of": r["as_of"], "source": r["source"],
             "is_stale": bool(r["is_stale"]), "count": r["count"],
             "fetched_at": r["fetched_at"]}
            for r in rows
        ]
