"""Unit tests for server/cache.py using fakeredis — no real Redis needed."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakeredis
from PIL import Image, ImageDraw

import config
config.CACHE_ENABLED = True
config.PHASH_HAMMING_THRESHOLD = 8
config.CACHE_SCAN_WARN_SIZE = 50000

import cache
import importlib
importlib.reload(cache)

r = fakeredis.FakeStrictRedis(decode_responses=True)
c = cache.PhashCache(r)

def make_image(seed, size=(200, 200)):
    img = Image.new("RGB", size, (seed % 256, (seed*3) % 256, (seed*7) % 256))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 100, 100], fill=((seed*11) % 256, 10, 10))
    return img

# --- compute_phash is deterministic ---
img_a = make_image(1)
h1 = cache.compute_phash(img_a)
h2 = cache.compute_phash(img_a)
assert h1 == h2, (h1, h2)
print("phash deterministic OK:", h1)

# --- miss on empty cache ---
lookup = c.lookup(h1)
assert not lookup.hit
print("empty cache miss OK")

# --- store then hit on EXACT same hash ---
c.store(h1, True, "test-slug")
lookup2 = c.lookup(h1)
assert lookup2.hit and lookup2.hamming_distance == 0 and lookup2.result["filenameSlug"] == "test-slug"
print("exact-hash hit OK:", lookup2)

# --- a very different image misses ---
img_b = make_image(999, size=(50, 300))
hb = cache.compute_phash(img_b)
dist = cache._hamming(h1, hb)
lookup3 = c.lookup(hb)
print(f"different image hamming dist={dist} hit={lookup3.hit}")
if dist > config.PHASH_HAMMING_THRESHOLD:
    assert not lookup3.hit
else:
    assert lookup3.hit  # small image happened to hash close; still valid behavior

# --- update() on existing entry ---
updated = c.update(h1, False, "corrected-slug")
assert updated is True
lookup4 = c.lookup(h1)
assert lookup4.result["filenameSlug"] == "corrected-slug" and lookup4.result["isMeme"] is False
print("update() overwrites existing entry OK:", lookup4.result)

# --- update() on non-existent entry returns False ---
fake_hash = "0" * 16
updated2 = c.update(fake_hash, True, "whatever")
assert updated2 is False
print("update() on missing entry returns False OK")

# --- size() ---
sz = c.size()
assert sz >= 1
print("size() OK:", sz)

# --- fail-open: broken redis client doesn't raise ---
class BrokenRedis:
    def hgetall(self, *a, **k): raise ConnectionError("down")
    def hset(self, *a, **k): raise ConnectionError("down")
    def hexists(self, *a, **k): raise ConnectionError("down")
    def hlen(self, *a, **k): raise ConnectionError("down")

broken_cache = cache.PhashCache(BrokenRedis())
lookup5 = broken_cache.lookup(h1)
assert not lookup5.hit  # fails open to a miss, doesn't raise
broken_cache.store(h1, True, "x")  # must not raise
assert broken_cache.size() == -1
print("fail-open on Redis errors OK")

print("\nALL CACHE TESTS PASSED")
