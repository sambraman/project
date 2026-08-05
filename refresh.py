"""
refresh.py — pull every tracked ticker into the cache.

Run nightly (from the web service scheduler or an external cron hitting
POST /refresh) or on demand from the command line:

    python refresh.py                 # refresh TRACKED_TICKERS (live)
    python refresh.py --offline       # refresh from fixtures (no network)
    python refresh.py IVV QQQ VOO      # refresh just these

TRACKED_TICKERS comes from the environment (comma-separated); it defaults to one
ticker per issuer type so a fresh checkout has something to refresh.
"""

from __future__ import annotations

import os
import sys

from cache import HoldingsCache
from holdings import get_holdings, HoldingsError

DEFAULT_TRACKED = "IVV,QQQ,SPY,VTI,VOO,SCHD"


def tracked_tickers():
    raw = os.environ.get("TRACKED_TICKERS", DEFAULT_TRACKED)
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def refresh(tickers=None, offline=None, cache=None):
    """Fetch each ticker and store it. Returns a per-ticker status list; one
    failing ticker never aborts the run."""
    tickers = tickers or tracked_tickers()
    cache = cache or HoldingsCache()
    results = []
    for t in tickers:
        try:
            res = get_holdings(t, offline=offline)
            cache.put(res)
            results.append({"ticker": t, "ok": True, "count": res.count,
                            "as_of": res.as_of, "source": res.source,
                            "is_stale": res.is_stale})
            print(f"  ✓ {t:<6} {res.count:>5} holdings  as of {res.as_of}  "
                  f"({res.source}{', stale' if res.is_stale else ''})")
        except HoldingsError as e:
            results.append({"ticker": t, "ok": False, "error": str(e)})
            print(f"  ✗ {t:<6} {e}")
    ok = sum(1 for r in results if r["ok"])
    print(f"refreshed {ok}/{len(results)} tickers")
    return results


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    offline = "--offline" in argv
    tickers = [a.upper() for a in argv if not a.startswith("-")] or None
    refresh(tickers=tickers, offline=offline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
