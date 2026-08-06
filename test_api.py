"""
test_api.py — call every endpoint for real.

WHY THIS EXISTS: `import app` proves the module parses. It does NOT prove a
route works. A name collision (app.py bound STORE to FundamentalsStore, shadowing
the datastore import) sailed past an import check and 500'd every /holdings
request in production. Nothing short of invoking the route catches that.

Runs with OFFLINE=1 so no network is touched. A 500 fails the test; a 404 on a
ticker with no fixture is acceptable — we're testing wiring, not data.

    python test_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ["OFFLINE"] = "1"
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())   # never touch a real DB

from fastapi.testclient import TestClient        # noqa: E402
import app as app_module                         # noqa: E402

client = TestClient(app_module.app)

FAILURES = []


def call(method: str, path: str, allow=(200,), label=""):
    r = client.request(method, path)
    ok = r.status_code in allow
    mark = "\u2713" if ok else "\u2717"
    name = label or f"{method} {path}"
    print(f"  {mark} {name} -> {r.status_code}")
    if not ok:
        detail = ""
        try:
            detail = str(r.json())[:200]
        except Exception:
            detail = r.text[:200]
        FAILURES.append(f"{name} returned {r.status_code}: {detail}")
    return r


def main():
    print("Store wiring (the bug that shipped)")
    # DATA_STORE must be the unified datastore, NOT FundamentalsStore.
    from datastore import DataStore
    assert isinstance(app_module.DATA_STORE, DataStore), \
        f"DATA_STORE is {type(app_module.DATA_STORE).__name__}, expected DataStore"
    print("  \u2713 DATA_STORE is a DataStore (not shadowed by FundamentalsStore)")
    for m in ("get_holdings", "put_holdings", "get_kpis", "put_kpis", "stats"):
        assert hasattr(app_module.DATA_STORE, m), f"DATA_STORE lacks {m}()"
    print("  \u2713 DATA_STORE exposes every method the endpoints call")
    if app_module.STORE is not None:
        assert not isinstance(app_module.STORE, DataStore), \
            "STORE and DATA_STORE must stay distinct objects"
        print("  \u2713 STORE (FundamentalsStore) remains separate")

    print("\nEndpoints")
    call("GET", "/health")
    call("GET", "/stats")
    call("GET", "/prices")
    call("GET", "/prices?ticker=MSFT&limit=5")
    call("GET", "/tickers")
    # Offline fixtures exist for these; 404 is tolerable, 500 is not.
    call("GET", "/holdings?ticker=IVV", allow=(200, 404))
    call("GET", "/holdings?ticker=SPY", allow=(200, 404))
    call("GET", "/holdings?ticker=SCHD", allow=(200, 404))
    call("GET", "/holdings?ticker=ZZZZ", allow=(200, 404))
    call("GET", "/kpis?ticker=MSFT&sector=hyperscalers", allow=(200, 404, 502))
    call("GET", "/kpis?ticker=JPM&sector=banks", allow=(200, 404, 502))
    call("GET", "/fundamentals?ticker=MSFT", allow=(200, 404, 502))
    call("GET", "/company?ticker=MSFT", allow=(200, 404, 503))
    call("GET", "/search?q=micro", allow=(200, 404, 503))
    # Auth guard must reject, not crash.
    call("POST", "/refresh", allow=(401, 403))
    call("POST", "/refresh-prices", allow=(401, 403))

    print("\n/stats shape")
    s = client.get("/stats").json()
    for key in ("kv", "holdings", "prices", "kpis", "funds"):
        assert key in s, f"/stats missing '{key}' — wrong store object?"
    print(f"  \u2713 /stats returns datastore counts, not fundamentals stats "
          f"({ {k: s[k] for k in ('kv', 'holdings', 'prices', 'kpis')} })")

    if FAILURES:
        print(f"\n{len(FAILURES)} ENDPOINT FAILURE(S):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("\nAPI CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
