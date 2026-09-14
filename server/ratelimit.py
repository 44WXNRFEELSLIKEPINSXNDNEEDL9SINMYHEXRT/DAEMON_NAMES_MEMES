"""
Per-provider rate limiting — Redis INCR + EXPIRE (fixed window), the
canonical Redis pattern for this. Provider ceilings are configurable via env
vars (config.py); the default self-hosted provider ("daemon2") gets a low,
hard ceiling the client cannot raise.

Requests carrying a user-supplied API key (BYO-key mode) must never reach
check_and_record() at all — that's enforced by the caller (app.py inspects
the request body for a byo_api_key field before calling in). That traffic is
the user's own cost against their own provider account.

Nginx's limit_req sits in front of this (see docker-compose.yml / nginx.conf)
as a coarse, cheap, IP-based flood guard. Division of labor is unchanged from
service/ratelimit.py's docstring: Nginx stops floods cheaply and generically;
this module enforces the actual per-provider business rule.
"""

from __future__ import annotations

import logging
import time

import config

log = logging.getLogger("meme-classifier.ratelimit")

PROVIDER_LIMITS = {
    "daemon2": config.RATE_LIMIT_DAEMON2_PER_WINDOW,
    "worker": config.RATE_LIMIT_WORKER_PER_WINDOW,
    # BYO-key providers (google/claude/openai/...) are never looked up here —
    # app.py exempts them from rate limiting entirely before calling in.
}


def _limit_for(provider: str) -> int:
    return PROVIDER_LIMITS.get(provider, config.RATE_LIMIT_DEFAULT_PER_WINDOW)


def _window_key(provider: str, client_key: str, window_id: int) -> str:
    return f"{config.RATE_LIMIT_KEY_PREFIX}{provider}:{client_key}:{window_id}"


class RedisRateLimiter:
    """Fixed-window counter: INCR the current window's key, EXPIRE it once on
    first increment. Simpler and cheaper than a sliding-window log for a
    single, low-traffic self-hosted service; the boundary-burst imprecision
    fixed windows are known for doesn't matter at these volumes (5-10/min)."""

    def __init__(self, redis_client) -> None:
        self.r = redis_client

    def check_and_record(self, provider: str, client_key: str) -> tuple[bool, int, int]:
        """
        Returns (allowed, remaining, retry_after_s). Fails OPEN on Redis
        errors — an unreachable rate limiter must not take the whole service
        down; Nginx's limit_req is still in front as the coarse guard in that
        situation (see module docstring).
        """
        if not config.RATE_LIMIT_ENABLED:
            return True, -1, 0

        limit = _limit_for(provider)
        window = config.RATE_LIMIT_WINDOW_S
        now = int(time.time())
        window_id = now // window
        key = _window_key(provider, client_key, window_id)

        try:
            count = self.r.incr(key)
            if count == 1:
                self.r.expire(key, window)
            ttl = self.r.ttl(key)
        except Exception:  # noqa: BLE001
            log.exception("Redis unavailable for rate limiting; failing open")
            return True, -1, 0

        retry_after = max(0, ttl if ttl and ttl > 0 else window)
        if count > limit:
            return False, 0, retry_after
        return True, max(0, limit - count), retry_after


def client_key(request) -> str:
    """Resolve the rate-limit key for a request (peer IP, or X-Forwarded-For
    when RATE_LIMIT_TRUST_PROXY=1 — true by default here because nginx is the
    intended front door in docker-compose; set False if exposing this
    gateway directly)."""
    if config.RATE_LIMIT_TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"
