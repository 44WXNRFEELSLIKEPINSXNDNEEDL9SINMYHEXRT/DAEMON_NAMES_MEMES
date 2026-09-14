"""
Single configuration layer for server/ — the gateway that sits in front of
the OCR+VLM pipeline (service/) and any future providers, adding: perceptual
-hash caching, per-provider rate limiting (Redis-backed), and a SQLite
metrics log. Every setting here is env-overridable; no host-specific values
are hardcoded (same convention as service/config.py).
"""

import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# --- Server -------------------------------------------------------------------
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8090)

# --- Upstream classifier (service/) --------------------------------------------
# server/ is a gateway: it does caching + rate limiting + metrics, then calls
# into service/pipeline.classify_detailed() in-process (default) OR proxies to
# a remote service/ instance over HTTP if SERVICE_URL is set (e.g. running
# server/ and service/ as separate containers/hosts).
SERVICE_URL = os.environ.get("SERVICE_URL", "").strip() or None
SERVICE_IMPORT_PATH = os.environ.get(
    "SERVICE_IMPORT_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service"),
)

# --- Request guards -------------------------------------------------------------
MAX_B64_CHARS = _env_int("MAX_B64_CHARS", 8_000_000)
MAX_REQUEST_BYTES = _env_int("MAX_REQUEST_BYTES", 12_000_000)

# --- Provider dispatch ---------------------------------------------------------
# All providers share this gateway's Redis cache, Redis rate limiting, and
# SQLite metrics log (see providers.py + app.py). BYO-key providers are
# exempt from rate limiting (see RATE_LIMIT_DEFAULT_PER_WINDOW docs below)
# but their results still populate the shared cache and metrics DB.
WORKER_URL = os.environ.get("WORKER_URL", "https://daemon-meme.windown52358.workers.dev")

# --- Redis ----------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_SOCKET_TIMEOUT_S = _env_float("REDIS_SOCKET_TIMEOUT_S", 2.0)

# --- Perceptual-hash cache (Redis, ACTIVELY GATES requests) --------------------
CACHE_ENABLED = _env_bool("CACHE_ENABLED", True)
# Named, documented constant — see README "Perceptual hash cache" for the
# reasoning. Two pHashes within this Hamming distance are treated as "the
# same meme" and the cached result is served without running OCR/VLM.
PHASH_HAMMING_THRESHOLD = _env_int("PHASH_HAMMING_THRESHOLD", 8)
# imagehash.phash default hash_size=8 -> 64-bit hash -> 16 hex chars.
PHASH_HASH_SIZE = _env_int("PHASH_HASH_SIZE", 8)
# Redis key prefix + index set name for the in-memory-at-query-time scan
# (see cache.py for why a full Redis-side index was not used).
CACHE_KEY_PREFIX = os.environ.get("CACHE_KEY_PREFIX", "dnm:cache:")
CACHE_INDEX_KEY = os.environ.get("CACHE_INDEX_KEY", "dnm:cache:index")
# 0 = no expiry ("memes don't go stale"). Set a TTL in seconds if memory
# becomes a real constraint later.
CACHE_TTL_S = _env_int("CACHE_TTL_S", 0)
# Above this many cached entries, warn in /health that the linear Hamming
# scan (see cache.py) may start costing noticeable per-request latency.
CACHE_SCAN_WARN_SIZE = _env_int("CACHE_SCAN_WARN_SIZE", 50_000)

# --- Rate limiting (Redis, per-provider) ---------------------------------------
RATE_LIMIT_ENABLED = _env_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_WINDOW_S = _env_int("RATE_LIMIT_WINDOW_S", 60)
# Per-provider ceilings. "daemon2" is the self-hosted OCR+VLM pipeline and
# "worker" is the shared Cloudflare Worker — both get a low, hard ceiling the
# client cannot raise (the worker also enforces its OWN 5/min via a Durable
# Object; this is a second, independent limit on top, since the worker is now
# reachable through this shared gateway too). Requests carrying a
# user-supplied API key (BYO-key providers: google/claude/openai/openrouter/
# groq/mistral/xai) are NOT subject to this at all — see app.py: that traffic
# never reaches check_and_record() in the first place, it's the user's own
# cost/quota with their provider, only the cache/metrics are shared.
RATE_LIMIT_DEFAULT_PER_WINDOW = _env_int("RATE_LIMIT_DEFAULT_PER_WINDOW", 5)
RATE_LIMIT_DAEMON2_PER_WINDOW = _env_int("RATE_LIMIT_DAEMON2_PER_WINDOW", 5)
RATE_LIMIT_WORKER_PER_WINDOW = _env_int("RATE_LIMIT_WORKER_PER_WINDOW", 5)
RATE_LIMIT_KEY_PREFIX = os.environ.get("RATE_LIMIT_KEY_PREFIX", "dnm:ratelimit:")
# Trust X-Forwarded-For only behind a reverse proxy you control (nginx in
# docker-compose here). See ratelimit.py for the spoofing risk otherwise.
RATE_LIMIT_TRUST_PROXY = _env_bool("RATE_LIMIT_TRUST_PROXY", True)

# --- SQLite metrics log ---------------------------------------------------------
METRICS_DB_PATH = os.environ.get("METRICS_DB_PATH", "/data/metrics.sqlite3")

# --- Output contract (same as service/) ----------------------------------------
FALLBACK_RESPONSE = {"isMeme": False, "filenameSlug": "unknown"}
