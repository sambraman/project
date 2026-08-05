"""
build_dataset.py — the overnight cataloger.

Walks universe.txt (top ~3000 companies from build_universe.py), pulls up to 10
fiscal years of fundamentals per company from SEC EDGAR, and stores it all in
data/fundamentals.db (via store.FundamentalsStore). Commit that file to the repo
and the web service serves the whole catalog with no live SEC calls — the point
of running this once, overnight, instead of continuously.

    python build_universe.py --top 3000     # writes universe.txt (from VTI)
    python build_dataset.py                  # catalog everything in universe.txt
    python build_dataset.py AAPL MSFT NVDA   # ad-hoc a few
    python build_dataset.py --workers 8      # tune concurrency (default 8)

Design for an unattended overnight run:
  * Resumable — companies already in the DB are skipped, so re-running continues.
  * Rate-safe — fetches run concurrently but SEC's own ~5 req/s throttle
    (edgar_client) still paces every request; DB writes happen on one thread.
  * Fault-tolerant — a company that errors is logged and skipped, never aborts.
Progress + an ETA print every 25 companies. A ~3000-name run is roughly 20-30
min; re-runs that only fill gaps are fast.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fundamentals import get_history
from store import FundamentalsStore

UNIVERSE = Path("universe.txt")
DEFAULT_WORKERS = 8
YEARS = 10


def _load_universe():
    if not UNIVERSE.exists():
        print(f"No {UNIVERSE}. Run: python build_universe.py --top 3000", file=sys.stderr)
        return []
    return [ln.strip().upper() for ln in UNIVERSE.read_text().splitlines() if ln.strip()]


def build(tickers, workers=DEFAULT_WORKERS, resume=True, store=None):
    store = store or FundamentalsStore()
    done = store.done_tickers() if resume else set()
    todo = [t for t in tickers if t not in done]
    print(f"{len(tickers)} in universe, {len(done)} already catalogued, "
          f"{len(todo)} to fetch (up to {YEARS}y each).")

    ok = fail = 0
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(get_history, t, YEARS): t for t in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            t = futures[fut]
            try:
                record = fut.result()
                store.write_company(record)     # writes happen here, on one thread
                ok += 1
            except LookupError:
                fail += 1                        # foreign issuer / fund / no XBRL
            except Exception as e:
                fail += 1
                print(f"  ! {t}: {type(e).__name__}: {e}")
            if i % 25 == 0 or i == len(todo):
                rate = i / max(time.monotonic() - t0, 1e-6)
                eta = (len(todo) - i) / max(rate, 1e-6)
                print(f"  {i}/{len(todo)}  ok={ok} skipped={fail}  "
                      f"{rate:.1f}/s  ETA {eta/60:.1f} min")

    s = store.stats()
    print(f"Done. catalog now holds {s['companies']} companies / "
          f"{s['history_rows']} year-rows, avg coverage {s.get('avg_coverage_pct')}%.")
    print(f"Commit it:  git add -f {store.path}  &&  git commit -m 'update fundamentals catalog'  &&  git push")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    workers = DEFAULT_WORKERS
    skip = set()
    if "--workers" in argv:
        i = argv.index("--workers")
        workers = int(argv[i + 1])
        skip = {i, i + 1}                       # consume the flag AND its value
    cli = [a.upper() for j, a in enumerate(argv)
           if not a.startswith("-") and j not in skip]
    tickers = cli or _load_universe()
    if not tickers:
        return 1
    build(tickers, workers=workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
