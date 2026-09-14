"""
Perceptual-hash cache — Redis-backed, ACTIVELY GATES requests (not log-only).

Design choice (documented per the task): Redis has no native Hamming-distance
query. Two approaches were considered:

  (a) Maintain a Redis-side index and use BITCOUNT on XORed bitmaps (SETBIT
      per hash bit, BITOP XOR, BITCOUNT) to compute Hamming distance inside
      Redis, avoiding any data transfer.
  (b) Keep all known (phash -> result) pairs in ONE Redis hash, fetch the
      whole thing with HGETALL, and compute Hamming distance in Python
      (int XOR + bit_count()) on the fetched keys.

We picked (b). Reasoning: this is a pet-project / low-traffic cache — the
realistic ceiling is thousands to low tens-of-thousands of distinct memes,
not millions. A 64-bit hash is 8 bytes; even 50,000 entries is a few hundred
KB transferred and comparing them is sub-millisecond in Python (int.bit_count
on a 64-bit XOR). Approach (a) would need SETBIT/BITOP scaffolding and a
second index structure purely to save an O(n) fetch that isn't a bottleneck
at this scale. If the cache ever grows to genuinely large N (see
CACHE_SCAN_WARN_SIZE in config.py — /health flags this), approach (a) or a
proper vector/BK-tree index is the correct next step — this module is
isolated so that swap doesn't touch the rest of the pipeline.

Storage shape: ONE Redis hash at config.CACHE_INDEX_KEY, field = phash hex
string, value = JSON {"isMeme": bool, "filenameSlug": str, "created_at": ts}.
No expiry by default (CACHE_TTL_S=0, "memes don't go stale"); if a TTL is
configured it's applied as a plain key TTL on a per-entry mirror key
(HASH fields can't carry independent TTLs in Redis) — see _entry_key().
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import imagehash
from PIL import Image

import config

log = logging.getLogger("meme-classifier.cache")


@dataclass
class CacheLookup:
    hit: bool
    phash: str
    matched_phash: str | None = None
    hamming_distance: int | None = None
    result: dict | None = None


def compute_phash(img: Image.Image) -> str:
    """64-bit perceptual hash (imagehash.phash, hash_size=8) as a hex string."""
    h = imagehash.phash(img, hash_size=config.PHASH_HASH_SIZE)
    return str(h)


def _hamming(a_hex: str, b_hex: str) -> int:
    return (int(a_hex, 16) ^ int(b_hex, 16)).bit_count()


def _entry_key(phash: str) -> str:
    return f"{config.CACHE_KEY_PREFIX}{phash}"


class PhashCache:
    """Thin wrapper so app.py / pipeline callers don't touch redis directly."""

    def __init__(self, redis_client) -> None:
        self.r = redis_client

    def lookup(self, phash: str) -> CacheLookup:
        """
        Scan all known hashes for one within PHASH_HAMMING_THRESHOLD. Returns
        the closest match on a hit. Never raises — a Redis error is treated
        as a cache miss (fail open: the pipeline still runs; caching is an
        optimization, not a correctness dependency).
        """
        if not config.CACHE_ENABLED:
            return CacheLookup(hit=False, phash=phash)
        try:
            index = self.r.hgetall(config.CACHE_INDEX_KEY)
        except Exception:  # noqa: BLE001
            log.exception("Redis unavailable for cache lookup; treating as miss")
            return CacheLookup(hit=False, phash=phash)

        if len(index) > config.CACHE_SCAN_WARN_SIZE:
            log.warning(
                "Cache index has %d entries (> CACHE_SCAN_WARN_SIZE=%d); "
                "linear Hamming scan may start costing real per-request time — "
                "see README 'Perceptual hash cache' for the scaling note",
                len(index), config.CACHE_SCAN_WARN_SIZE,
            )

        best_key, best_dist, best_raw = None, None, None
        for raw_key, raw_val in index.items():
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            try:
                dist = _hamming(phash, key)
            except ValueError:
                continue  # corrupt/foreign key in the index; skip defensively
            if best_dist is None or dist < best_dist:
                best_key, best_dist, best_raw = key, dist, raw_val

        if best_key is not None and best_dist is not None and best_dist <= config.PHASH_HAMMING_THRESHOLD:
            try:
                result = json.loads(best_raw)
            except (json.JSONDecodeError, TypeError):
                log.warning("Corrupt cache entry for phash=%s; treating as miss", best_key)
                return CacheLookup(hit=False, phash=phash)
            return CacheLookup(
                hit=True, phash=phash, matched_phash=best_key,
                hamming_distance=best_dist,
                result={"isMeme": result["isMeme"], "filenameSlug": result["filenameSlug"]},
            )
        return CacheLookup(hit=False, phash=phash)

    def store(self, phash: str, is_meme: bool, filename_slug: str) -> None:
        """Insert or overwrite the cache entry for this exact phash."""
        payload = json.dumps({
            "isMeme": is_meme, "filenameSlug": filename_slug,
            "created_at": time.time(),
        })
        try:
            self.r.hset(config.CACHE_INDEX_KEY, phash, payload)
            if config.CACHE_TTL_S > 0:
                self.r.expire(_entry_key(phash), config.CACHE_TTL_S)
        except Exception:  # noqa: BLE001
            log.exception("Failed to store cache entry for phash=%s (fail-open)", phash)

    def update(self, phash: str, is_meme: bool, filename_slug: str) -> bool:
        """
        Overwrite an EXISTING cache entry in place (correction flow, scenario
        A). Returns True if an entry existed and was updated, False if there
        was nothing to update (caller should fall back to a plain store()).
        """
        try:
            existed = self.r.hexists(config.CACHE_INDEX_KEY, phash)
        except Exception:  # noqa: BLE001
            log.exception("Redis unavailable for cache update")
            return False
        self.store(phash, is_meme, filename_slug)
        return bool(existed)

    def size(self) -> int:
        try:
            return int(self.r.hlen(config.CACHE_INDEX_KEY))
        except Exception:  # noqa: BLE001
            return -1
