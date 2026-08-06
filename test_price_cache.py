"""
test_price_cache.py — offline round-trip test for the price cache (no network,
no API key). Verifies storage + idempotency without touching a price vendor.

    python test_price_cache.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from price_cache import PriceCache


def main():
    with tempfile.TemporaryDirectory() as d:
        pc = PriceCache(Path(d) / "t.db")
        bars = [
            {"date": "2026-08-05", "open": 1.5, "high": 2.5, "low": 1.4,
             "close": 2.0, "volume": 120, "source": "test"},
            {"date": "2026-08-04", "open": 1.0, "high": 2.0, "low": 0.5,
             "close": 1.5, "volume": 100, "source": "test"},
        ]
        assert pc.put_bars("aapl", bars) == 2
        print("  \u2713 put_bars stores rows (and upper-cases the ticker)")

        pc.put_bars("AAPL", bars)  # same dates again
        hist = pc.get_history("AAPL")
        assert len(hist) == 2, f"expected no dupes, got {len(hist)}"
        print("  \u2713 upsert is idempotent (no dupes)")

        assert hist[0]["date"] == "2026-08-05", "history should be newest-first"
        print("  \u2713 get_history newest-first")

        assert pc.latest("AAPL")["close"] == 2.0
        print("  \u2713 latest() returns the most recent close")

        summary = pc.list_tickers()
        assert summary and summary[0]["ticker"] == "AAPL" and summary[0]["bars"] == 2
        print("  \u2713 list_tickers summarizes freshness")

        assert pc.put_bars("XYZ", []) == 0 and pc.latest("XYZ") is None
        print("  \u2713 empty/no-data runs are harmless")

    print("\nPRICE CACHE CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
