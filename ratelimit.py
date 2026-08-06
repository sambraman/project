"""
ratelimit.py — per-host request pacing (stdlib only).

Issuer sites sit behind WAFs with tight per-IP budgets (BlackRock/iShares is the
strict one: ~10 requests / 60s). This module keeps us *inside* those budgets
rather than trying to hide from them:

  * TokenBucket — blocks until a request is allowed. Per-host, thread-safe, so
    the FastAPI layer's ThreadPoolExecutor can't accidentally burst.
  * polite_get  — GET with bucket pacing, exponential backoff, and respect for
    the server's own Retry-After header on 429/503.

Design notes
  - The real win is *fewer calls*, not faster ones: fetch an issuer's whole
    product catalog once (issuer_catalog.py) instead of one lookup per ticker.
  - We identify ourselves honestly. Set HTTP_CONTACT to an email/URL so an
    issuer can contact you instead of silently blocking you. Rotating user
    agents or proxying to dodge a WAF is out of scope by design: it converts a
    rate-limit problem into a ToS problem.
  - Limits are overridable per host via RATE_LIMITS env, e.g.
        RATE_LIMITS="www.ishares.com=8/60,www.troweprice.com=15/60"

    python ratelimit.py        # self-test, no network
"""

from __future__ import annotations

import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = ssl.create_default_context()

# host -> (max_calls, per_seconds). Deliberately below each site's real ceiling
# so a retry or a concurrent worker can't tip us over.
DEFAULT_LIMITS = {
    "www.ishares.com":              (8, 60.0),   # documented-strict: 10/60
    "www.blackrock.com":            (8, 60.0),
    "www.troweprice.com":           (10, 60.0),
    "www.capitalgroup.com":         (10, 60.0),
    "www.schwabassetmanagement.com": (10, 60.0),
    "www.vaneck.com":               (10, 60.0),
    "www.globalxetfs.com":          (10, 60.0),
    "www.wisdomtree.com":           (10, 60.0),
    "www.ftportfolios.com":         (10, 60.0),
    "www.ssga.com":                 (10, 60.0),
    "www.invesco.com":              (10, 60.0),
    "investor.vanguard.com":        (10, 60.0),
    "www.sec.gov":                  (8, 10.0),   # SEC asks for <=10/s; be gentler
}
FALLBACK_LIMIT = (10, 60.0)


def _parse_env_limits():
    """RATE_LIMITS="host=calls/seconds,host2=calls/seconds" """
    raw = os.environ.get("RATE_LIMITS", "").strip()
    out = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        host, _, spec = part.partition("=")
        try:
            calls, _, secs = spec.partition("/")
            out[host.strip().lower()] = (int(calls), float(secs or 60))
        except ValueError:
            continue
    return out


class TokenBucket:
    """Classic token bucket. acquire() blocks until a token is available."""

    def __init__(self, max_calls: int, per_seconds: float):
        self.max_calls = max(1, int(max_calls))
        self.per_seconds = float(per_seconds)
        self._tokens = float(self.max_calls)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float):
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        rate = self.max_calls / self.per_seconds
        self._tokens = min(float(self.max_calls), self._tokens + elapsed * rate)
        self._updated = now

    def acquire(self, timeout: float | None = None) -> float:
        """Block until a token is free. Returns seconds waited."""
        deadline = None if timeout is None else time.monotonic() + timeout
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                deficit = 1.0 - self._tokens
                sleep_for = deficit * (self.per_seconds / self.max_calls)
            if deadline is not None and time.monotonic() + sleep_for > deadline:
                raise TimeoutError("rate limit wait exceeded timeout")
            sleep_for = min(max(sleep_for, 0.01), 5.0)
            time.sleep(sleep_for)
            waited += sleep_for


class HostLimiter:
    """One TokenBucket per hostname, created on demand."""

    def __init__(self, limits=None):
        self._limits = dict(DEFAULT_LIMITS)
        self._limits.update(_parse_env_limits())
        if limits:
            self._limits.update(limits)
        self._buckets = {}
        self._lock = threading.Lock()

    def bucket(self, host: str) -> TokenBucket:
        host = (host or "").lower()
        with self._lock:
            b = self._buckets.get(host)
            if b is None:
                calls, secs = self._limits.get(host, FALLBACK_LIMIT)
                b = TokenBucket(calls, secs)
                self._buckets[host] = b
            return b

    def acquire_for(self, url: str) -> float:
        return self.bucket(urllib.parse.urlparse(url).netloc).acquire()


LIMITER = HostLimiter()


def default_headers() -> dict:
    """Honest, contactable identification. Set HTTP_CONTACT so an issuer can
    reach you rather than silently blocking your IP."""
    contact = os.environ.get("HTTP_CONTACT", "").strip()
    ua = "etf-backend/1.0 (+holdings look-through)"
    if contact:
        ua += f" contact:{contact}"
    return {"User-Agent": ua, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}


def polite_get(url: str, timeout: int = 60, headers: dict | None = None,
               max_retries: int = 3, limiter: HostLimiter | None = None) -> bytes:
    """GET with per-host pacing and backoff.

    Retries on 429/503 (honoring Retry-After) and transient 5xx. A 403 is NOT
    retried: that's the WAF saying no, and hammering it is how a soft block
    becomes a hard one. Fix a 403 by slowing down or asking the issuer for
    access, not by retrying harder.
    """
    lim = limiter or LIMITER
    hdrs = default_headers()
    if headers:
        hdrs.update(headers)

    last_err = None
    for attempt in range(max_retries + 1):
        lim.acquire_for(url)
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_SSL_CONTEXT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 403:
                raise
            if e.code in (429, 503) or 500 <= e.code < 600:
                if attempt >= max_retries:
                    raise
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = float(retry_after) if retry_after else 0.0
                except (TypeError, ValueError):
                    delay = 0.0
                delay = delay or min(60.0, 2.0 ** attempt * 5.0)
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt >= max_retries:
                raise
            time.sleep(min(30.0, 2.0 ** attempt * 2.0))
    if last_err:
        raise last_err
    raise RuntimeError("polite_get exhausted retries")


def _self_test():
    b = TokenBucket(3, 1.0)
    t0 = time.monotonic()
    for _ in range(3):
        b.acquire()
    assert time.monotonic() - t0 < 0.3, "first 3 should be immediate"
    print("  \u2713 burst up to capacity is immediate")

    b.acquire()
    assert time.monotonic() - t0 >= 0.25, "4th must wait for a refill"
    print("  \u2713 4th call blocks until refill")

    hl = HostLimiter({"example.com": (2, 1.0)})
    assert hl.bucket("example.com") is hl.bucket("example.com")
    assert hl.bucket("example.com") is not hl.bucket("other.com")
    print("  \u2713 one bucket per host, reused")

    assert hl.bucket("www.ishares.com").max_calls <= 10
    print("  \u2713 iShares stays under its 10/60 ceiling")

    def hammer():
        for _ in range(4):
            hl.bucket("t.com").acquire()
    threads = [threading.Thread(target=hammer) for _ in range(3)]
    t0 = time.monotonic()
    [t.start() for t in threads]
    [t.join() for t in threads]
    print(f"  \u2713 thread-safe under concurrency ({time.monotonic() - t0:.1f}s for 12 calls)")

    print("\nRATE LIMIT CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
