"""Unit tests for server/ratelimit.py using fakeredis."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakeredis
import config
config.RATE_LIMIT_ENABLED = True
config.RATE_LIMIT_WINDOW_S = 2
config.RATE_LIMIT_DEFAULT_PER_WINDOW = 3
config.RATE_LIMIT_DAEMON2_PER_WINDOW = 3
config.RATE_LIMIT_TRUST_PROXY = True

import ratelimit
import importlib
importlib.reload(ratelimit)

r = fakeredis.FakeStrictRedis(decode_responses=True)
limiter = ratelimit.RedisRateLimiter(r)

# --- basic allow/deny within window ---
results = [limiter.check_and_record("daemon2", "1.2.3.4")[0] for _ in range(5)]
assert results == [True, True, True, False, False], results
print("basic fixed-window OK:", results)

# --- different client key has its own bucket ---
allowed, remaining, retry = limiter.check_and_record("daemon2", "9.9.9.9")
assert allowed and remaining == 2, (allowed, remaining)
print("per-client isolation OK")

# --- different provider has its own bucket even for the same client ---
allowed2, remaining2, _ = limiter.check_and_record("other-provider", "1.2.3.4")
assert allowed2  # "1.2.3.4" is maxed out on daemon2 but not on other-provider
print("per-provider isolation OK")

# --- unknown provider falls back to RATE_LIMIT_DEFAULT_PER_WINDOW ---
for _ in range(3):
    limiter.check_and_record("mystery-provider", "5.5.5.5")
blocked = limiter.check_and_record("mystery-provider", "5.5.5.5")
assert blocked[0] is False
print("unknown provider uses default limit OK")

# --- client_key(): XFF trust ---
class FakeClient:
    def __init__(self, host): self.host = host
class FakeRequest:
    def __init__(self, host, headers=None):
        self.client = FakeClient(host)
        self.headers = headers or {}

req = FakeRequest("10.0.0.1", {"x-forwarded-for": "203.0.113.5, 10.0.0.1"})
key = ratelimit.client_key(req)
assert key == "203.0.113.5", key
print("client_key() trusts XFF when RATE_LIMIT_TRUST_PROXY=True:", key)

config.RATE_LIMIT_TRUST_PROXY = False
key2 = ratelimit.client_key(req)
assert key2 == "10.0.0.1", key2
print("client_key() ignores XFF when RATE_LIMIT_TRUST_PROXY=False:", key2)
config.RATE_LIMIT_TRUST_PROXY = True

# --- disabled bypasses entirely ---
config.RATE_LIMIT_ENABLED = False
for _ in range(20):
    allowed3, remaining3, _ = limiter.check_and_record("daemon2", "brand-new-client")
    assert allowed3 and remaining3 == -1
print("disabled mode bypasses limiter OK")
config.RATE_LIMIT_ENABLED = True

# --- fail-open on Redis errors ---
class BrokenRedis:
    def incr(self, *a, **k): raise ConnectionError("down")
broken_limiter = ratelimit.RedisRateLimiter(BrokenRedis())
allowed4, remaining4, retry4 = broken_limiter.check_and_record("daemon2", "x")
assert allowed4 is True and remaining4 == -1
print("fail-open on Redis errors OK")

print("\nALL RATE LIMIT TESTS PASSED")
