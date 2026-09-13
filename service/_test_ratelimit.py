"""Rate limiter unit tests — no models, no network, runs in <1s."""
import time
import types

import config
config.RATE_LIMIT_PER_MINUTE = 3
config.RATE_LIMIT_WINDOW_S = 1  # short window for a fast test
config.RATE_LIMIT_ENABLED = True
config.RATE_LIMIT_TRUST_PROXY = False

import importlib
import ratelimit
importlib.reload(ratelimit)  # pick up the overridden config values


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, host="1.2.3.4", headers=None):
        self.client = FakeClient(host)
        self.headers = headers or {}


# --- basic allow/deny ---
req = FakeRequest("9.9.9.9")
results = [ratelimit.check_and_record(req)[0] for _ in range(5)]
assert results == [True, True, True, False, False], results
print("basic sliding window OK:", results)

# --- different IP has its own bucket ---
req2 = FakeRequest("8.8.8.8")
allowed, remaining, _ = ratelimit.check_and_record(req2)
assert allowed and remaining == 2, (allowed, remaining)
print("per-IP isolation OK")

# --- window expiry ---
time.sleep(1.1)
allowed, remaining, retry = ratelimit.check_and_record(req)
assert allowed, (allowed, remaining, retry)
print("window expiry OK")

# --- X-Forwarded-For NOT trusted by default (anti-spoof) ---
spoofed = FakeRequest("1.1.1.1", headers={"x-forwarded-for": "6.6.6.6"})
key = ratelimit.client_key(spoofed)
assert key == "1.1.1.1", key
print("XFF ignored when RATE_LIMIT_TRUST_PROXY=False:", key)

# --- X-Forwarded-For trusted when explicitly enabled ---
config.RATE_LIMIT_TRUST_PROXY = True
key2 = ratelimit.client_key(spoofed)
assert key2 == "6.6.6.6", key2
print("XFF trusted when RATE_LIMIT_TRUST_PROXY=True:", key2)
config.RATE_LIMIT_TRUST_PROXY = False

# --- disabled bypasses entirely ---
config.RATE_LIMIT_ENABLED = False
req3 = FakeRequest("5.5.5.5")
for _ in range(20):
    allowed, remaining, _ = ratelimit.check_and_record(req3)
    assert allowed and remaining == -1
print("disabled mode bypasses limiter OK")

print("\nALL RATE LIMIT TESTS PASSED")
