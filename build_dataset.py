"""
build_dataset.py — the batch fundamentals builder.

Reads universe.txt (from build_universe.py), pulls each company's fundamentals
from SEC EDGAR, and writes fundamentals.db (SQLite, one row per ticker) and
fundamentals.csv. It's resumable: companies already in the DB are skipped, so if
it stops for any reason, just run it again.

    python build_dataset.py                 # all tickers in universe.txt
    python build_dataset.py --prices        # also fill PE/PB/PS (needs a price source)
    python build_dataset.py AAPL MSFT NVDA  # just these (ad-hoc)

Progress prints every 25 companies. SEC is paced at ~5 req/s with caching, so a
2000-name run takes roughly 7 minutes the first time and is fast on re-runs.
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

from fundamentals import get_fundamentals

DB_PATH = Path("fundamentals.db")
CSV_PATH = Path("fundamentals.csv")
UNIVERSE = Path("universe.txt")

COLUMNS = [
    "ticker", "cik", "name", "sector", "industry", "country", "hq",
    "fiscal_year", "price",
    "revenue", "net_income", "assets", "equity", "debt", "shares_outstanding",
    "net_margin", "gross_margin", "operating_margin", "roe", "roa",
    "revenue_growth_yoy", "earnings_growth_yoy", "eps_growth_yoy", "revenue_cagr_3y",
    "debt_to_equity", "debt_to_assets", "liabilities_to_assets", "current_ratio",
    "interest_coverage", "eps_diluted", "book_value_per_share", "revenue_per_share",
    "pe_ratio", "pb_ratio", "ps_ratio", "peg_ratio", "earnings_yield",
]


def _col_type(c):
    if c in ("ticker", "name", "sector", "industry", "country", "hq"):
        return "TEXT"
    if c in ("cik", "fiscal_year"):
        return "INTEGER"
    return "REAL"


def _init_db(conn):
    cols = ", ".join(f'"{c}" {_col_type(c)}' for c in COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS fundamentals ({cols}, "
                 f"PRIMARY KEY (ticker))")


def _done_tickers(conn):
    try:
        return {r[0] for r in conn.execute("SELECT ticker FROM fundamentals")}
    except sqlite3.OperationalError:
        return set()


def _load_universe():
    if not UNIVERSE.exists():
        print(f"No {UNIVERSE}. Run build_universe.py first, or pass tickers on the "
              f"command line.", file=sys.stderr)
        return []
    return [ln.strip().upper() for ln in UNIVERSE.read_text().splitlines() if ln.strip()]


def build(tickers, with_prices=False, resume=True):
    conn = sqlite3.connect(DB_PATH)
    _init_db(conn)
    done = _done_tickers(conn) if resume else set()
    todo = [t for t in tickers if t not in done]
    print(f"{len(tickers)} in universe, {len(done)} already done, {len(todo)} to fetch.")

    ok = fail = 0
    for i, t in enumerate(todo, 1):
        try:
            row = get_fundamentals(t, with_price=with_prices)
            conn.execute(
                f"INSERT OR REPLACE INTO fundamentals ({','.join(COLUMNS)}) "
                f"VALUES ({','.join('?' * len(COLUMNS))})",
                [row.get(c) for c in COLUMNS],
            )
            conn.commit()
            ok += 1
        except LookupError:
            fail += 1                       # foreign issuer / fund / no XBRL — skip
        except Exception as e:
            fail += 1
            print(f"  ! {t}: {type(e).__name__}: {e}")
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}  (ok {ok}, skipped/failed {fail})")

    conn.close()
    _export_csv()
    print(f"Done. {ok} written, {fail} skipped. -> {DB_PATH}, {CSV_PATH}")


def _export_csv():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(f"SELECT {','.join(COLUMNS)} FROM fundamentals "
                        f"ORDER BY ticker").fetchall()
    conn.close()
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    with_prices = "--prices" in argv
    cli_tickers = [a.upper() for a in argv if not a.startswith("-")]
    tickers = cli_tickers or _load_universe()
    if not tickers:
        return 1
    build(tickers, with_prices=with_prices)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
