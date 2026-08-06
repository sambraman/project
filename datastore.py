"""
datastore.py — one place all data lives, so the web app never waits on a vendor.

THE PROBLEM THIS SOLVES
Before: /holdings could trigger a live iShares fetch, an SEC N-PORT walk, and an
OpenFIGI enrichment pass — inside the request. A slow or blocked issuer became a
slow or failed page load, and the data was scattered across holdings.db,
prices.db, fundamentals.db, .catalog_cache/ and a raw companyfacts cache with no
common lookup.

After: one SQLite file, one lookup pattern, and a hard rule —

    THE REQUEST PATH NEVER MAKES A NETWORK CALL.

Reads are served from the store. Anything missing or stale is handed to the
nightly refresh (or a background thread), and the caller gets whatever is on
hand plus an honest freshness flag. Stale data with a date beats a spinner, and
beats a 502.

LAYOUT — one DB, one key convention, so anything is findable:

    kv(namespace, key)   -> json value + fetched_at + ttl        generic cache
    holdings(fund, ...)  -> one row per (fund, holding, as_of)   look-through
    prices(ticker, date) -> OHLCV                                time series
    kpis(ticker, sector) -> json KPI payload                     sector metrics

    DATA_DIR (env, default ./data) holds lookthrough.db. On Render, point this
    at a mounted persistent disk or the cache dies on every deploy.

USAGE
    from datastore import STORE
    STORE.put_json("catalog", "ishares", {...}, ttl_hours=24)
    hit = STORE.get_json("catalog", "ishares")      # -> Entry | None
    if hit and hit.fresh: ...                       # fresh
    elif hit: ...                                   # stale but usable

    python datastore.py        # self-test, no network
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent / "data"))
DB_PATH = DATA_DIR / "lookthrough.db"

DEFAULT_TTL_HOURS = {
    "catalog": 24.0,        # issuer product lineups change slowly
    "holdings": 20.0,       # daily files; refresh nightly
    "prices": 20.0,
    "kpis": 168.0,          # filings are quarterly — a week is generous
    "companyfacts": 24.0,
    "figi": 720.0,          # ticker<->CUSIP mappings are near-static
}


@dataclass
class Entry:
    namespace: str
    key: str
    value: object
    fetched_at: float
    ttl_hours: float

    @property
    def age_hours(self) -> float:
        return (time.time() - self.fetched_at) / 3600.0

    @property
    def fresh(self) -> bool:
        return self.age_hours <= self.ttl_hours

    def as_meta(self) -> dict:
        """Freshness metadata to attach to an API payload."""
        return {"cached": True, "fresh": self.fresh,
                "age_hours": round(self.age_hours, 2),
                "fetched_at": int(self.fetched_at)}


class DataStore:
    def __init__(self, path=None):
        self.path = str(path or DB_PATH)
        self.degraded = False
        self._lock = threading.Lock()
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._init()
        except Exception as e:
            # A read-only or missing DATA_DIR must NOT take the whole service
            # down at import time. Fall back to a scratch DB: the app still
            # serves (just without persistence across restarts) and /stats
            # reports degraded=True so the cause is visible.
            import tempfile
            fallback = Path(tempfile.gettempdir()) / "lookthrough-fallback.db"
            print(f"WARNING: datastore at {self.path} unusable ({e}). "
                  f"Falling back to {fallback} — data will NOT persist across "
                  f"restarts. Set DATA_DIR to a writable persistent disk.")
            self.path = str(fallback)
            self.degraded = True
            self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL lets readers proceed during a write — the refresh job no longer
        # blocks the web app. This is the single biggest reliability win here.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init(self):
        with self._connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS kv (
                    namespace  TEXT NOT NULL,
                    key        TEXT NOT NULL,
                    value      TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    ttl_hours  REAL NOT NULL,
                    PRIMARY KEY (namespace, key)
                );
                CREATE INDEX IF NOT EXISTS idx_kv_ns ON kv(namespace);

                CREATE TABLE IF NOT EXISTS holdings (
                    fund       TEXT NOT NULL,
                    holding    TEXT NOT NULL,
                    name       TEXT,
                    weight     REAL,
                    as_of      TEXT NOT NULL,
                    source     TEXT,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (fund, holding, as_of)
                );
                CREATE INDEX IF NOT EXISTS idx_hold_fund ON holdings(fund, as_of);
                -- reverse look-through: "which funds hold NVDA?"
                CREATE INDEX IF NOT EXISTS idx_hold_holding ON holdings(holding);

                CREATE TABLE IF NOT EXISTS holdings_meta (
                    fund       TEXT PRIMARY KEY,
                    as_of      TEXT,
                    source     TEXT,
                    is_stale   INTEGER,
                    degraded   INTEGER,
                    count      INTEGER,
                    warnings   TEXT,
                    fetched_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prices (
                    ticker     TEXT NOT NULL,
                    date       TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    volume     INTEGER,
                    source     TEXT,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (ticker, date)
                );
                CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker, date DESC);

                CREATE TABLE IF NOT EXISTS kpis (
                    ticker     TEXT NOT NULL,
                    sector     TEXT NOT NULL,
                    period     TEXT,
                    payload    TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (ticker, sector)
                );
                CREATE INDEX IF NOT EXISTS idx_kpis_sector ON kpis(sector);
            """)

    # --- generic kv ------------------------------------------------------- #
    def put_json(self, namespace: str, key: str, value, ttl_hours=None):
        ttl = ttl_hours if ttl_hours is not None else \
            DEFAULT_TTL_HOURS.get(namespace, 24.0)
        with self._lock, self._connect() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?,?)",
                      (namespace, key.upper() if namespace != "catalog" else key,
                       json.dumps(value), time.time(), float(ttl)))
        return True

    def get_json(self, namespace: str, key: str, allow_stale: bool = True):
        k = key.upper() if namespace != "catalog" else key
        with self._connect() as c:
            row = c.execute(
                "SELECT * FROM kv WHERE namespace=? AND key=?",
                (namespace, k)).fetchone()
        if not row:
            return None
        try:
            val = json.loads(row["value"])
        except Exception:
            return None
        e = Entry(namespace, k, val, row["fetched_at"], row["ttl_hours"])
        if not e.fresh and not allow_stale:
            return None
        return e

    def delete(self, namespace: str, key: str):
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM kv WHERE namespace=? AND key=?", (namespace, key))

    # --- holdings --------------------------------------------------------- #
    def put_holdings(self, result) -> int:
        """Persist a HoldingsResult (duck-typed: ticker/as_of/source/holdings)."""
        now = time.time()
        fund = result.ticker.upper()
        rows = [(fund, h.ticker or h.name[:32], h.name, h.weight,
                 result.as_of, result.source, now) for h in result.holdings]
        with self._lock, self._connect() as c:
            c.executemany(
                "INSERT OR REPLACE INTO holdings "
                "(fund,holding,name,weight,as_of,source,fetched_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            c.execute(
                "INSERT OR REPLACE INTO holdings_meta "
                "(fund,as_of,source,is_stale,degraded,count,warnings,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fund, result.as_of, result.source,
                 int(bool(getattr(result, "is_stale", False))),
                 int(bool(getattr(result, "degraded", False))),
                 len(result.holdings),
                 json.dumps(list(getattr(result, "warnings", []))), now))
        return len(rows)

    def get_holdings(self, fund: str):
        """Latest stored holdings for a fund, with freshness metadata."""
        f = fund.upper()
        with self._connect() as c:
            meta = c.execute("SELECT * FROM holdings_meta WHERE fund=?",
                             (f,)).fetchone()
            if not meta:
                return None
            rows = c.execute(
                "SELECT holding,name,weight,source FROM holdings "
                "WHERE fund=? AND as_of=? ORDER BY weight DESC",
                (f, meta["as_of"])).fetchall()
        age = (time.time() - meta["fetched_at"]) / 3600.0
        return {
            "ticker": f, "as_of": meta["as_of"], "source": meta["source"],
            "is_stale": bool(meta["is_stale"]), "degraded": bool(meta["degraded"]),
            "count": meta["count"],
            "warnings": json.loads(meta["warnings"] or "[]"),
            "holdings": [dict(r) for r in rows],
            "cached": True, "age_hours": round(age, 2),
            "fresh": age <= DEFAULT_TTL_HOURS["holdings"],
        }

    def funds_holding(self, ticker: str, limit: int = 50):
        """Reverse look-through — the query an RIA actually asks: which of my
        funds give me exposure to X? The idx_hold_holding index makes this fast
        instead of a full scan."""
        t = ticker.upper()
        with self._connect() as c:
            rows = c.execute(
                "SELECT h.fund, h.weight, h.as_of FROM holdings h "
                "JOIN holdings_meta m ON m.fund=h.fund AND m.as_of=h.as_of "
                "WHERE h.holding=? ORDER BY h.weight DESC LIMIT ?",
                (t, limit)).fetchall()
        return [dict(r) for r in rows]

    # --- prices ----------------------------------------------------------- #
    def put_prices(self, ticker: str, bars) -> int:
        if not bars:
            return 0
        now, t = time.time(), ticker.upper()
        rows = [(t, b.get("date"), b.get("open"), b.get("high"), b.get("low"),
                 b.get("close"), b.get("volume"), b.get("source"), now)
                for b in bars if b.get("date")]
        if not rows:
            return 0
        with self._lock, self._connect() as c:
            c.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?,?)",
                          rows)
        return len(rows)

    def get_prices(self, ticker: str, limit: int = 30):
        with self._connect() as c:
            rows = c.execute(
                "SELECT date,open,high,low,close,volume,source FROM prices "
                "WHERE ticker=? ORDER BY date DESC LIMIT ?",
                (ticker.upper(), limit)).fetchall()
        return [dict(r) for r in rows]

    # --- kpis ------------------------------------------------------------- #
    def put_kpis(self, ticker: str, sector: str, period: str, payload):
        with self._lock, self._connect() as c:
            c.execute("INSERT OR REPLACE INTO kpis VALUES (?,?,?,?,?)",
                      (ticker.upper(), sector, period, json.dumps(payload),
                       time.time()))

    def get_kpis(self, ticker: str, sector: str):
        with self._connect() as c:
            row = c.execute("SELECT * FROM kpis WHERE ticker=? AND sector=?",
                            (ticker.upper(), sector)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        age = (time.time() - row["fetched_at"]) / 3600.0
        payload.update({"cached": True, "age_hours": round(age, 2),
                        "fresh": age <= DEFAULT_TTL_HOURS["kpis"]})
        return payload

    # --- ops -------------------------------------------------------------- #
    def stats(self) -> dict:
        out = {"db_path": self.path, "degraded": self.degraded}
        if self.degraded:
            out["warning"] = ("Storage is a temp fallback — data will be lost on "
                              "restart. Set DATA_DIR to a writable persistent disk.")
        with self._connect() as c:
            for t in ("kv", "holdings", "holdings_meta", "prices", "kpis"):
                out[t] = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            out["funds"] = [dict(r) for r in c.execute(
                "SELECT fund, as_of, source, count, degraded FROM holdings_meta "
                "ORDER BY fund").fetchall()]
            try:
                out["db_mb"] = round(Path(self.path).stat().st_size / 1e6, 2)
            except OSError:
                out["db_mb"] = None
        return out

    def vacuum(self):
        with self._connect() as c:
            c.execute("VACUUM")


STORE = DataStore()


# --------------------------------------------------------------------------- #
# Stale-while-revalidate
# --------------------------------------------------------------------------- #
def get_or_refresh(namespace: str, key: str, fetch_fn, ttl_hours=None,
                   store: DataStore | None = None, background: bool = True):
    """Serve from the store; refresh only when needed.

    fresh  -> return it, no network.
    stale  -> return it NOW, refresh in a background thread. The user sees data
              immediately and the next request gets the update.
    absent -> fetch synchronously (nothing else to serve).

    This is the pattern that makes the app feel fast: the slow path runs at most
    once per TTL, and never while someone is waiting on a stale-but-usable
    answer.
    """
    st = store or STORE
    hit = st.get_json(namespace, key)
    if hit and hit.fresh:
        return hit.value, "fresh"
    if hit:
        if background:
            def _bg():
                try:
                    st.put_json(namespace, key, fetch_fn(), ttl_hours)
                except Exception:
                    pass    # keep serving the stale value
            threading.Thread(target=_bg, daemon=True).start()
            return hit.value, "stale-refreshing"
        try:
            val = fetch_fn()
            st.put_json(namespace, key, val, ttl_hours)
            return val, "refreshed"
        except Exception:
            return hit.value, "stale"
    val = fetch_fn()
    st.put_json(namespace, key, val, ttl_hours)
    return val, "cold"


# --------------------------------------------------------------------------- #
def _self_test():
    import tempfile

    class _H:
        def __init__(self, t, n, w): self.ticker, self.name, self.weight = t, n, w

    class _R:
        def __init__(self):
            self.ticker, self.as_of, self.source = "IVV", "2026-08-04", "ishares"
            self.is_stale, self.degraded, self.warnings = False, False, []
            self.holdings = [_H("AAPL", "Apple", 0.07), _H("NVDA", "Nvidia", 0.06)]

    with tempfile.TemporaryDirectory() as d:
        st = DataStore(Path(d) / "t.db")

        st.put_json("catalog", "ishares", {"IVV": "239726"}, ttl_hours=24)
        e = st.get_json("catalog", "ishares")
        assert e.value["IVV"] == "239726" and e.fresh
        print("  \u2713 kv round-trip, marked fresh")

        st.put_json("catalog", "old", {"a": 1}, ttl_hours=0)
        old = st.get_json("catalog", "old")
        assert old is not None and not old.fresh
        assert st.get_json("catalog", "old", allow_stale=False) is None
        print("  \u2713 stale entries survive but are flagged (stale beats empty)")

        assert st.put_holdings(_R()) == 2
        h = st.get_holdings("ivv")
        assert h["count"] == 2 and h["holdings"][0]["holding"] == "AAPL"
        assert h["cached"] and h["fresh"]
        print("  \u2713 holdings persist with freshness metadata")

        rev = st.funds_holding("NVDA")
        assert rev and rev[0]["fund"] == "IVV"
        print("  \u2713 reverse look-through: which funds hold NVDA")

        st.put_prices("IVV", [{"date": "2026-08-04", "close": 5.0, "source": "t"},
                              {"date": "2026-08-03", "close": 4.0, "source": "t"}])
        p = st.get_prices("IVV", limit=1)
        assert p[0]["date"] == "2026-08-04"
        print("  \u2713 prices stored newest-first")

        st.put_kpis("MSFT", "hyperscalers", "2026Q1", {"kpis": [], "coverage": 90})
        k = st.get_kpis("MSFT", "hyperscalers")
        assert k["coverage"] == 90 and k["cached"]
        print("  \u2713 KPI payloads cached by ticker+sector")

        # stale-while-revalidate
        calls = []

        def fetch():
            calls.append(1)
            return {"v": len(calls)}

        v, state = get_or_refresh("kpis", "X", fetch, store=st)
        assert state == "cold" and v == {"v": 1}
        v, state = get_or_refresh("kpis", "X", fetch, store=st)
        assert state == "fresh" and len(calls) == 1, "fresh must not refetch"
        print("  \u2713 fresh reads make ZERO network calls")

        st.put_json("kpis", "Y", {"old": True}, ttl_hours=0)
        v, state = get_or_refresh("kpis", "Y", fetch, ttl_hours=1, store=st,
                                  background=False)
        assert state == "refreshed"
        print("  \u2713 stale entry refreshes and updates")

        def boom():
            raise RuntimeError("vendor down")

        st.put_json("kpis", "Z", {"kept": True}, ttl_hours=0)
        v, state = get_or_refresh("kpis", "Z", boom, store=st, background=False)
        assert v == {"kept": True} and state == "stale"
        print("  \u2713 vendor failure serves stale data instead of erroring")

        s = st.stats()
        assert s["holdings"] == 2 and s["funds"][0]["fund"] == "IVV"
        print(f"  \u2713 stats for ops ({s['holdings']} holdings, {s['db_mb']} MB)")

    print("\nDATASTORE CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
