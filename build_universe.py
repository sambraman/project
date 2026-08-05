"""
build_universe.py — write universe.txt: the tickers to pull fundamentals for.

Because the holdings API and the fundamentals builder now live in one repo, this
pulls a broad-market ETF's holdings directly from holdings.get_holdings (no HTTP
call needed), sorts by weight (≈ market-cap ranking), and writes the top N to
universe.txt — the input build_dataset.py reads.

    python build_universe.py                 # top 2000 from VTI (total US market)
    python build_universe.py --source IWV --top 1500
"""

from __future__ import annotations

import sys

from holdings import get_holdings, HoldingsError

DEFAULT_SOURCE = "VTI"   # Vanguard total US market — full holdings, tickered, live
DEFAULT_TOP = 3000


def build_universe(source=DEFAULT_SOURCE, top=DEFAULT_TOP, offline=None,
                   out_path="universe.txt"):
    res = get_holdings(source, offline=offline)
    # holdings already sorted by weight desc; keep real US-style tickers only.
    tickers = []
    for h in res.holdings:
        t = h.ticker.strip().upper()
        if t and t.isalpha() and 1 <= len(t) <= 5:
            tickers.append(t)
        if len(tickers) >= top:
            break
    seen, unique = set(), []
    for t in tickers:
        if t not in seen:
            seen.add(t); unique.append(t)
    with open(out_path, "w") as f:
        f.write("\n".join(unique) + "\n")
    print(f"Wrote {len(unique)} tickers to {out_path} "
          f"(source {source}, as of {res.as_of}).")
    return unique


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    source, top = DEFAULT_SOURCE, DEFAULT_TOP
    if "--source" in argv:
        source = argv[argv.index("--source") + 1].upper()
    if "--top" in argv:
        top = int(argv[argv.index("--top") + 1])
    offline = "--offline" in argv
    try:
        build_universe(source=source, top=top, offline=offline)
    except HoldingsError as e:
        print(f"error: could not pull {source} holdings — {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
