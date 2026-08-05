"""
store.py — the persistent fundamentals dataset (SQLite).

One file, two tables:
  * companies — one row per ticker: identity, classification, year span, and the
    coverage ("% of data mapped") figure.
  * history   — one row per (ticker, fiscal_year): every metric for that year.

The overnight cataloger (build_dataset.py) writes it; the web service reads it so
it never has to hit SEC live for a catalogued company. The file lives at
data/fundamentals.db and is committed to the repo, so a deploy ships with the
whole dataset baked in.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fundamentals.metrics import METRIC_KEYS

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "fundamentals.db"

_COMPANY_COLS = [
    "ticker", "cik", "name", "sector", "industry", "country", "hq", "currency",
    "first_year", "last_year", "years_count", "coverage_pct", "mapped_cells",
    "total_cells", "updated_at",
]


class FundamentalsStore:
    def __init__(self, path=DEFAULT_PATH, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init()

    # -- connection ---------------------------------------------------------- #
    def exists(self) -> bool:
        return self.path.exists()

    def _connect(self):
        if self.read_only:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS companies (
                ticker TEXT PRIMARY KEY, cik INTEGER, name TEXT, sector TEXT,
                industry TEXT, country TEXT, hq TEXT, currency TEXT,
                first_year INTEGER, last_year INTEGER, years_count INTEGER,
                coverage_pct REAL, mapped_cells INTEGER, total_cells INTEGER,
                updated_at TEXT)""")
            hist_cols = ", ".join(
                f'"{k}" {"INTEGER" if k == "fiscal_year" else "REAL"}'
                for k in (["fiscal_year"] + METRIC_KEYS))
            c.execute(f"""CREATE TABLE IF NOT EXISTS history (
                ticker TEXT, {hist_cols},
                PRIMARY KEY (ticker, fiscal_year))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_hist_ticker ON history(ticker)")

    # -- write --------------------------------------------------------------- #
    def write_company(self, record: dict):
        """Persist one get_history() record (company row + its history rows)."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        comp = {k: record.get(k) for k in _COMPANY_COLS}
        comp["updated_at"] = now
        with self._connect() as c:
            c.execute(
                f"INSERT OR REPLACE INTO companies ({','.join(_COMPANY_COLS)}) "
                f"VALUES ({','.join('?' * len(_COMPANY_COLS))})",
                [comp[k] for k in _COMPANY_COLS])
            c.execute("DELETE FROM history WHERE ticker = ?", (record["ticker"],))
            cols = ["ticker", "fiscal_year"] + METRIC_KEYS
            for row in record.get("history", []):
                vals = [record["ticker"], row.get("fiscal_year")] + \
                       [row.get(k) for k in METRIC_KEYS]
                c.execute(
                    f"INSERT OR REPLACE INTO history ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})", vals)

    def done_tickers(self) -> set:
        if not self.exists():
            return set()
        with self._connect() as c:
            return {r[0] for r in c.execute("SELECT ticker FROM companies")}

    # -- read (used by the web service) -------------------------------------- #
    def get_company(self, ticker: str):
        ticker = ticker.upper()
        with self._connect() as c:
            comp = c.execute("SELECT * FROM companies WHERE ticker=?",
                             (ticker,)).fetchone()
            if not comp:
                return None
            rows = c.execute(
                "SELECT * FROM history WHERE ticker=? ORDER BY fiscal_year DESC",
                (ticker,)).fetchall()
        out = dict(comp)
        out["history"] = [{k: r[k] for k in r.keys() if k != "ticker"} for r in rows]
        return out

    def search(self, q: str, limit: int = 20):
        q = (q or "").strip().upper()
        if not q:
            return []
        like = f"%{q}%"
        with self._connect() as c:
            rows = c.execute(
                "SELECT ticker, name, sector, country, last_year, coverage_pct "
                "FROM companies WHERE ticker LIKE ? OR UPPER(name) LIKE ? "
                "ORDER BY (ticker = ?) DESC, ticker LIMIT ?",
                (like, like, q, limit)).fetchall()
        return [dict(r) for r in rows]

    def stats(self):
        if not self.exists():
            return {"companies": 0, "history_rows": 0}
        with self._connect() as c:
            n = c.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            h = c.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            cov = c.execute("SELECT AVG(coverage_pct) FROM companies").fetchone()[0]
        return {"companies": n, "history_rows": h,
                "avg_coverage_pct": round(cov, 1) if cov else 0.0}
