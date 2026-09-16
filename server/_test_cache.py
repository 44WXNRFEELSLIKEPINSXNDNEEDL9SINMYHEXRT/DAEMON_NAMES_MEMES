"""Unit tests for server/cache.py using fakeredis — no real Redis needed."""
import sys, os, asyncio, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakeredis
from PIL import Image, ImageDraw

import config
config.CACHE_ENABLED = True
config.PHASH_HAMMING_THRESHOLD = 8
config.PHASH_HASH_SIZE = 8
config.CACHE_TTL_S = 0
config.CACHE_SCAN_WARN_SIZE = 50000

import cache
import importlib
importlib.reload(cache)


def make_image(seed, size=(200, 200)):
    img = Image.new("RGB", size, (seed % 256, (seed*3) % 256, (seed*7) % 256))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 100, 100], fill=((seed*11) % 256, 10, 10))
    return img


def flip_bits(phash, n, rng):
    value = int(phash, 16)
    for bit in rng.sample(range(64), n):
        value ^= 1 << bit
    return f"{value:016x}"


async def main():
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    c = cache.PhashCache(r)
    rng = random.Random(7)

    # --- segmentation covers every bit exactly once, threshold+1 segments ---
    segs = cache._segments()
    assert len(segs) == 9 and sum(w for _, w in segs) == 64, segs
    covered = 0
    for shift, width in segs:
        mask = ((1 << width) - 1) << shift
        assert covered & mask == 0
        covered |= mask
    assert covered == (1 << 64) - 1
    print("segments disjoint + complete OK:", segs)

    # --- compute_phash is deterministic and valid ---
    h1 = cache.compute_phash(make_image(1))
    assert h1 == cache.compute_phash(make_image(1)) and cache.is_valid_phash(h1), h1
    assert not cache.is_valid_phash("xyz") and not cache.is_valid_phash(None)
    print("phash deterministic OK:", h1)

    # --- miss on empty cache ---
    assert not (await c.lookup(h1, require_known_is_meme=True)).hit
    print("empty cache miss OK")

    # --- store then exact hit ---
    await c.store(h1, {"isMeme": True, "filenameSlug": "test-slug"}, is_meme_known=True)
    hit = await c.lookup(h1, require_known_is_meme=True)
    assert hit.hit and hit.hamming_distance == 0 and hit.result == {"isMeme": True, "filenameSlug": "test-slug"}
    print("exact-hash hit OK")

    # --- near match found at every distance <= threshold, never above ---
    for dist in range(0, 13):
        q = flip_bits(h1, dist, rng)
        res = await c.lookup(q, require_known_is_meme=True)
        assert res.hit == (dist <= 8), (dist, res)
        if res.hit:
            assert res.matched_phash == h1 and res.hamming_distance == dist
    print("near-match boundary (<=8 hit, >8 miss) OK")

    # --- index lookup agrees with brute force over many random entries ---
    stored = {}
    for i in range(400):
        ph = f"{rng.getrandbits(64):016x}"
        stored[ph] = f"slug-{i}"
        await c.store(ph, {"isMeme": True, "filenameSlug": stored[ph]}, is_meme_known=True)
    stored[h1] = "test-slug"
    anchors = list(stored)
    for _ in range(300):
        base = rng.choice(anchors)
        q = flip_bits(base, rng.randint(0, 14), rng) if rng.random() < 0.7 else f"{rng.getrandbits(64):016x}"
        best = min(((cache._hamming(q, ph), ph) for ph in stored), default=None)
        res = await c.lookup(q, require_known_is_meme=True)
        if best[0] <= 8:
            assert res.hit and res.hamming_distance == best[0], (q, best, res)
        else:
            assert not res.hit, (q, best, res)
    print("multi-index lookup == brute force (300 queries over 401 entries) OK")

    # --- the closest entry wins ---
    near = flip_bits(h1, 2, rng)
    await c.store(near, {"isMeme": False, "filenameSlug": "closer"}, is_meme_known=True)
    res = await c.lookup(flip_bits(near, 1, random.Random(99)), require_known_is_meme=True)
    assert res.hit and res.hamming_distance <= 3
    print("closest match preferred OK:", res.hamming_distance, res.result)

    # --- manual-mode (assumed isMeme) entries ---
    assumed = f"{rng.getrandbits(64):016x}"
    await c.store(assumed, {"isMeme": True, "filenameSlug": "assumed"}, is_meme_known=False, overwrite=False)
    assert not (await c.lookup(assumed, require_known_is_meme=True)).hit
    res = await c.lookup(assumed, require_known_is_meme=False)
    assert res.hit and not res.is_meme_known and res.result["filenameSlug"] == "assumed"
    # a known verdict overwrites an assumed one...
    await c.store(assumed, {"isMeme": False, "filenameSlug": "known"}, is_meme_known=True)
    assert (await c.lookup(assumed, require_known_is_meme=True)).result["filenameSlug"] == "known"
    # ...but an assumed store never clobbers a known one
    await c.store(assumed, {"isMeme": True, "filenameSlug": "assumed-again"}, is_meme_known=False, overwrite=False)
    assert (await c.lookup(assumed, require_known_is_meme=True)).result["filenameSlug"] == "known"
    print("assumed-isMeme entries never served as auto verdicts OK")

    # --- update() ---
    assert await c.update(h1, {"isMeme": False, "filenameSlug": "corrected-slug"}, is_meme_known=True) is True
    assert (await c.lookup(h1, require_known_is_meme=True)).result == {"isMeme": False, "filenameSlug": "corrected-slug"}
    assert await c.update(h1, {"isMeme": True, "filenameSlug": "slug-only"}, is_meme_known=False) is True
    assert (await c.lookup(h1, require_known_is_meme=True)).result == {"isMeme": False, "filenameSlug": "slug-only"}
    assert await c.update("0" * 16, {"isMeme": True, "filenameSlug": "x"}, is_meme_known=True) is False
    assert await c.update("not-a-hash", {"isMeme": True, "filenameSlug": "x"}, is_meme_known=True) is False
    print("update() overwrites existing / keeps known isMeme / False on missing OK")

    # --- TTL applies per entry, stale index members are pruned lazily ---
    config.CACHE_TTL_S = 100
    ttl_hash = f"{rng.getrandbits(64):016x}"
    await c.store(ttl_hash, {"isMeme": True, "filenameSlug": "ttl"}, is_meme_known=True)
    assert 0 < await r.ttl(cache._entry_key(ttl_hash)) <= 100
    assert all([0 < await r.ttl(k) <= 100 for k in cache._segment_keys(ttl_hash)])
    await r.delete(cache._entry_key(ttl_hash))  # simulate expiry/eviction
    probe = flip_bits(ttl_hash, 1, rng)
    assert not (await c.lookup(probe, require_known_is_meme=True)).hit
    assert not any([await r.sismember(k, ttl_hash) for k in cache._segment_keys(ttl_hash)])
    config.CACHE_TTL_S = 0
    print("per-entry TTL + lazy index pruning OK")

    # --- threshold 0 = exact match only, no index keys written ---
    config.PHASH_HAMMING_THRESHOLD = 0
    exact = f"{rng.getrandbits(64):016x}"
    before = len(await r.keys("dnm:cache:s*"))
    await c.store(exact, {"isMeme": True, "filenameSlug": "exact"}, is_meme_known=True)
    assert len(await r.keys("dnm:cache:s*")) == before
    assert (await c.lookup(exact, require_known_is_meme=True)).hit
    assert not (await c.lookup(flip_bits(exact, 1, rng), require_known_is_meme=True)).hit
    config.PHASH_HAMMING_THRESHOLD = 8
    print("threshold=0 exact-only mode OK")

    # --- disabled cache is a no-op ---
    config.CACHE_ENABLED = False
    assert not (await c.lookup(h1, require_known_is_meme=True)).hit
    config.CACHE_ENABLED = True
    print("disabled cache OK")

    # --- fail-open: broken redis never raises ---
    class BrokenRedis:
        def pipeline(self, *a, **k): raise ConnectionError("down")
        async def get(self, *a, **k): raise ConnectionError("down")
        async def mget(self, *a, **k): raise ConnectionError("down")
        async def ping(self): raise ConnectionError("down")

    broken = cache.PhashCache(BrokenRedis())
    assert not (await broken.lookup(h1, require_known_is_meme=True)).hit
    await broken.store(h1, {"isMeme": True, "filenameSlug": "x"}, is_meme_known=True)
    assert await broken.update(h1, {"isMeme": True, "filenameSlug": "x"}, is_meme_known=True) is False
    assert await broken.ping() is False
    print("fail-open on Redis errors OK")

    await r.aclose()


asyncio.run(main())
print("\nALL CACHE TESTS PASSED")
