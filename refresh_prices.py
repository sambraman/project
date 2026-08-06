"""
refresh_prices.py — pull daily EOD price bars for every tracked ticker.

The price sibling of refresh.py (holdings). Run nightly after market close, via
the web service's POST /refresh-prices, an external cron hitting that endpoint,
or on demand:

    python refresh_prices.py                 # TRACKED_TICKERS (live)
    python refresh_prices.py IVV QQQ VOO      # just these
    python refresh_prices.py --days 30       # deeper backfill window

Prices come from fundamentals.prices.get_eod_bars (EODHD, yfinance fallback) and
land in the SQLite PriceCache (prices.db). One failing ticker never aborts the
run. Note: EODHD's free tier is ~20 calls/day, so the default universe is your
TRACKED_TICKERS (the funds), not their constituents — widen it on a paid tier.
"""

from __future__ import annotations

import sys

from price_cache import PriceCache
from refresh import tracked_tickers
from fundamentals.prices import get_eod_bars


def refresh_prices(tickers=None, cache=None, days: int = 7):
    """Fetch EOD bars for each ticker and store them. Returns a per-ticker
    status list, mirroring refresh.refresh()."""
    tickers = tickers or tracked_tickers()
    cache = cache or PriceCache()
    results = []
    for t in tickers:
        try:
            bars = get_eod_bars(t, days=days)
            n = cache.put_bars(t, bars or [])
            latest = bars[0].get("date") if bars else None
            ok = n > 0
            results.append({"ticker": t, "ok": ok, "bars": n, "latest": latest})
            mark = "\u2713" if ok else "\u2717"
            tail = f"  latest {latest}" if latest else "  (no price source / no data)"
            print(f"  {mark} {t:<6} {n:>3} bars{tail}")
        except Exception as e:  # never let one ticker abort the run
            results.append({"ticker": t, "ok": False, "error": str(e)})
            print(f"  \u2717 {t:<6} {e}")
    ok = sum(1 for r in results if r["ok"])
    print(f"priced {ok}/{len(results)} tickers")
    return results


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    days = 7
    if "--days" in argv:
        i = argv.index("--days")
        try:
            days = int(argv[i + 1])
            del argv[i:i + 2]
        except (IndexError, ValueError):
            print("--days needs an integer")
            return 2
    tickers = [a.upper() for a in argv if not a.startswith("-")] or None
    refresh_prices(tickers=tickers, days=days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
