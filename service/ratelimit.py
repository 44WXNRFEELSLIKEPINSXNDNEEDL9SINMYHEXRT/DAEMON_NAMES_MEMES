"""
In-memory, per-IP sliding-window rate limiter for the classifier API.

No external dependency (no Redis) — appropriate for a single-process
self-hosted pet-project deployment. State is process-local: it resets on
restart and does not synchronize across multiple replicas. If you ever run
more than one instance behind a load balancer, replace this with a shared
store (Redis INCR + EXPIRE is the usual choice) — this module is written so
that swap is a drop-in (same `check()` / `record()` interface).

Why this exists: the service has no auth (see README "Rate limiting & abuse
protection"). A single shared VLM already takes 20-30s per request and fully
serializes behind one lock (pipeline._model_lock) — one client hammering the
endpoint can starve every other caller. This limiter is the cheap mitigation
that fits a pet project: no signup, no API keys, just "don't let one client
monopolize the only worker."
"""

from __future__ import annotations

import threading
import time
from collections import deque

import config


class SlidingWindowLimiter:
    def __init__(self, limit_per_window: int, window_s: int) -> None:
        self.limit = limit_per_window
        self.window_s = window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        dq = self._hits.setdefault(key, deque())
        cutoff = now - self.window_s
        while dq and dq[0] < cutoff:
            dq.popleft()
        return dq

    def check(self, key: str) -> tuple[bool, int, float]:
        """
        Returns (allowed, remaining, retry_after_s). Does NOT record a hit —
        call record() only after the request is accepted, so preflight/HEAD
        checks or early rejections don't consume quota.
        """
        now = time.monotonic()
        with self._lock:
            dq = self._prune(key, now)
            remaining = max(0, self.limit - len(dq))
            if len(dq) >= self.limit:
                retry_after = max(0.0, self.window_s - (now - dq[0]))
                return False, 0, retry_after
            return True, remaining, 0.0

    def record(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            dq = self._prune(key, now)
            dq.append(now)

    def sweep(self, max_idle_s: float = 3600) -> int:
        """Drop empty/stale per-key deques so long-lived processes don't leak
        memory across many distinct client IPs. Call periodically, not per
        request (see app.py)."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            stale = [
                k for k, dq in self._hits.items()
                if not dq or (now - dq[-1]) > max_idle_s
            ]
            for k in stale:
                del self._hits[k]
                removed += 1
        return removed


_limiter = SlidingWindowLimiter(config.RATE_LIMIT_PER_MINUTE, config.RATE_LIMIT_WINDOW_S)
_last_sweep = time.monotonic()
_SWEEP_INTERVAL_S = 300


def client_key(request) -> str:
    """
    Resolve the rate-limit key for a request. Only trusts X-Forwarded-For
    when RATE_LIMIT_TRUST_PROXY=1 (see config.py) — otherwise that header is
    spoofable by any client and would make the limiter a no-op.
    """
    if config.RATE_LIMIT_TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def check_and_record(request) -> tuple[bool, int, float]:
    """Returns (allowed, remaining, retry_after_s); records the hit iff allowed.
    `remaining` reflects quota left AFTER this request is counted."""
    global _last_sweep
    if not config.RATE_LIMIT_ENABLED:
        return True, -1, 0.0

    key = client_key(request)
    allowed, _, retry_after = _limiter.check(key)
    if allowed:
        _limiter.record(key)
        _, remaining, _ = _limiter.check(key)
    else:
        remaining = 0

    now = time.monotonic()
    if now - _last_sweep > _SWEEP_INTERVAL_S:
        _limiter.sweep()
        _last_sweep = now

    return allowed, remaining, retry_after
