"""
Perceptual-hash cache — Redis-backed (async), ACTIVELY GATES requests.

Lookup is a multi-index Hamming search, not a full scan:

  A pHash of B bits is split into m = PHASH_HAMMING_THRESHOLD + 1 disjoint
  segments. If two hashes differ in at most `threshold` bits, those bits fall
  into at most `threshold` segments, so (pigeonhole) at least one segment is
  identical. Each segment value therefore gets a Redis SET of the full hashes
  that have it; a lookup unions the m sets for the query's segment values and
  computes exact distances on that candidate list only. No false negatives,
  and per-lookup work is ~m * N / 2^(B/m) instead of N (64-bit, threshold 8:
  ~14x fewer comparisons; threshold 4: ~1600x). An exact-hash GET runs first,
  so re-shared identical images never touch the index at all.

Keys (all under CACHE_KEY_PREFIX, so the gateway can share a Redis instance
with other applications):

  {prefix}e:{phash}           STRING  JSON entry, per-entry TTL (CACHE_TTL_S)
  {prefix}s{m}:{i}:{value}    SET     full phashes whose segment i == value

With a TTL, index sets are expired alongside their newest member and stale
members (entry already expired/evicted) are removed lazily during lookups.
Changing PHASH_HAMMING_THRESHOLD changes m and therefore starts a fresh index
namespace; older entries stay reachable by exact match until they expire.

Entries record whether isMeme was actually decided by a model
("isMemeKnown"). daemon2 in manual mode assumes isMeme=True without asking
the model, so such entries can serve slugs to manual requests but never an
isMeme verdict to auto requests.

Every Redis failure is treated as a miss / no-op (fail open): caching is an
optimization, never a correctness dependency.
"""

from __future__ import annotations

import json
import logging
import re
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
    is_meme_known: bool = True


def hash_bits() -> int:
    return config.PHASH_HASH_SIZE * config.PHASH_HASH_SIZE


def _hex_len() -> int:
    return (hash_bits() + 3) // 4


def is_valid_phash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{_hex_len()}}}", value) is not None


def compute_phash(img: Image.Image) -> str:
    """Perceptual hash (imagehash.phash) as a lowercase hex string."""
    return str(imagehash.phash(img, hash_size=config.PHASH_HASH_SIZE))


def _hamming(a_hex: str, b_hex: str) -> int:
    return (int(a_hex, 16) ^ int(b_hex, 16)).bit_count()


def _segments() -> list[tuple[int, int]]:
    """(shift, width) for each disjoint segment, most significant first."""
    bits = hash_bits()
    m = max(1, min(config.PHASH_HAMMING_THRESHOLD + 1, bits))
    base, extra = divmod(bits, m)
    out, pos = [], bits
    for i in range(m):
        width = base + (1 if i < extra else 0)
        pos -= width
        out.append((pos, width))
    return out


def _entry_key(phash: str) -> str:
    return f"{config.CACHE_KEY_PREFIX}e:{phash}"


def _segment_keys(phash: str) -> list[str]:
    value = int(phash, 16)
    segs = _segments()
    return [
        f"{config.CACHE_KEY_PREFIX}s{len(segs)}:{i}:{(value >> shift) & ((1 << width) - 1):x}"
        for i, (shift, width) in enumerate(segs)
    ]


def _parse_entry(raw: str | bytes | None) -> dict | None:
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
        return {
            "isMeme": bool(entry["isMeme"]),
            "filenameSlug": str(entry["filenameSlug"]),
            "isMemeKnown": bool(entry.get("isMemeKnown", True)),
        }
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


class PhashCache:
    """Thin async wrapper so app.py never touches Redis keys directly."""

    def __init__(self, redis_client) -> None:
        self.r = redis_client

    async def lookup(self, phash: str, *, require_known_is_meme: bool) -> CacheLookup:
        """Closest usable entry within PHASH_HAMMING_THRESHOLD, or a miss."""
        miss = CacheLookup(hit=False, phash=phash)
        if not config.CACHE_ENABLED or not is_valid_phash(phash):
            return miss
        try:
            use_index = config.PHASH_HAMMING_THRESHOLD > 0
            seg_keys = _segment_keys(phash) if use_index else []
            async with self.r.pipeline(transaction=False) as pipe:
                pipe.get(_entry_key(phash))
                for key in seg_keys:
                    pipe.smembers(key)
                replies = await pipe.execute()

            ordered: list[tuple[int, str]] = [(0, phash)]
            candidates: set[str] = set()
            for members in replies[1:]:
                candidates.update(m.decode() if isinstance(m, bytes) else m for m in members)
            candidates.discard(phash)
            if len(candidates) > config.CACHE_SCAN_WARN_SIZE:
                log.warning(
                    "pHash lookup compared %d candidates (> CACHE_SCAN_WARN_SIZE=%d); "
                    "consider a lower PHASH_HAMMING_THRESHOLD or a CACHE_TTL_S",
                    len(candidates), config.CACHE_SCAN_WARN_SIZE,
                )
            for cand in candidates:
                try:
                    dist = _hamming(phash, cand)
                except ValueError:
                    continue
                if dist <= config.PHASH_HAMMING_THRESHOLD:
                    ordered.append((dist, cand))
            ordered[1:] = sorted(ordered[1:])

            raws = [replies[0]]
            if len(ordered) > 1:
                raws += await self.r.mget([_entry_key(c) for _, c in ordered[1:]])

            stale: list[str] = []
            for (dist, cand), raw in zip(ordered, raws):
                entry = _parse_entry(raw)
                if entry is None:
                    if raw is None and cand != phash:
                        stale.append(cand)
                    continue
                if require_known_is_meme and not entry["isMemeKnown"]:
                    continue
                await self._prune(stale)
                return CacheLookup(
                    hit=True, phash=phash, matched_phash=cand, hamming_distance=dist,
                    result={"isMeme": entry["isMeme"], "filenameSlug": entry["filenameSlug"]},
                    is_meme_known=entry["isMemeKnown"],
                )
            await self._prune(stale)
            return miss
        except Exception:  # noqa: BLE001
            log.exception("Redis unavailable for cache lookup; treating as miss")
            return miss

    async def _prune(self, stale: list[str]) -> None:
        """Drop index members whose entry expired or was evicted."""
        if not stale:
            return
        try:
            async with self.r.pipeline(transaction=False) as pipe:
                for cand in stale:
                    for key in _segment_keys(cand):
                        pipe.srem(key, cand)
                await pipe.execute()
        except Exception:  # noqa: BLE001
            log.debug("Failed to prune stale cache index members", exc_info=True)

    async def store(self, phash: str, result: dict, *, is_meme_known: bool,
                    overwrite: bool = True) -> None:
        """Insert (or overwrite) the entry for this exact phash."""
        if not config.CACHE_ENABLED or not is_valid_phash(phash):
            return
        payload = json.dumps({
            "isMeme": bool(result["isMeme"]), "filenameSlug": result["filenameSlug"],
            "isMemeKnown": is_meme_known, "created_at": time.time(),
        }, ensure_ascii=False)
        ttl = config.CACHE_TTL_S if config.CACHE_TTL_S > 0 else None
        try:
            async with self.r.pipeline(transaction=True) as pipe:
                pipe.set(_entry_key(phash), payload, ex=ttl, nx=not overwrite)
                if config.PHASH_HAMMING_THRESHOLD > 0:
                    for key in _segment_keys(phash):
                        pipe.sadd(key, phash)
                        if ttl:
                            pipe.expire(key, ttl)
                await pipe.execute()
        except Exception:  # noqa: BLE001
            log.exception("Failed to store cache entry for phash=%s (fail-open)", phash)

    async def update(self, phash: str, result: dict, *, is_meme_known: bool) -> bool:
        """
        Overwrite an EXISTING entry (correction flow, scenario A). Returns
        False when there was nothing to update. An assumed isMeme never
        replaces a model-decided one — only the slug is corrected then.
        """
        if not is_valid_phash(phash):
            return False
        try:
            existing = _parse_entry(await self.r.get(_entry_key(phash)))
        except Exception:  # noqa: BLE001
            log.exception("Redis unavailable for cache update")
            return False
        if existing is None:
            return False
        if existing["isMemeKnown"] and not is_meme_known:
            result = {"isMeme": existing["isMeme"], "filenameSlug": result["filenameSlug"]}
            is_meme_known = True
        await self.store(phash, result, is_meme_known=is_meme_known)
        return True

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:  # noqa: BLE001
            return False
