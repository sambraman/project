"""
test_prices.py — a 10-ticker EODHD price check (free-tier safe). RUN THIS FIRST
before enabling price multiples, so you confirm your key works without burning
your daily quota.

    export EODHD_API_KEY=your_key
    python test_prices.py

~10 API calls (of EODHD's 20/day free tier). Without a key it falls back to
yfinance if installed, else reports that no price source is configured.
"""

from __future__ import annotations

import os

from fundamentals.prices import get_price

TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "V", "WMT"]


def main():
    src = "EODHD" if os.environ.get("EODHD_API_KEY") else "yfinance (no EODHD_API_KEY set)"
    print(f"Price source: {src}\n")
    ok = 0
    for t in TICKERS:
        p = get_price(t)
        if p:
            ok += 1
            print(f"  ✓ {t:6} {p:,.2f}")
        else:
            print(f"  ✗ {t:6} no price")
    print()
    if ok:
        print(f"SUCCESS — {ok}/{len(TICKERS)} priced. Price multiples will fill in "
              f"build_dataset.py --prices and /fundamentals?with_price=true.")
        return 0
    print("No prices returned. Set EODHD_API_KEY (or `pip install yfinance`).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
