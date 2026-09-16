"""
Single configuration layer for server/ — a lite gateway in front of the
classification providers. It does two things only: perceptual-hash caching
(Redis) and a metrics log (SQLite). Model work always happens upstream
(service/ over HTTP, the Cloudflare Worker, or a BYO-key provider API);
rate limiting stays where it already lives (nginx edge, service/, worker).

Every setting is env-overridable; no host-specific values are hardcoded
(same convention as service/config.py).
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


def _env_list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


# --- Server -------------------------------------------------------------------
# Loopback by default: a bare `python app.py` must not grab a public port on a
# shared host. The container overrides this (see Dockerfile) because there the
# port is only reachable through nginx on the compose network.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = _env_int("PORT", 8090)
# Only these peers may set X-Forwarded-For (uvicorn --forwarded-allow-ips).
# Comma-separated IPs/CIDRs, or "*" when the app port is reachable ONLY via a
# proxy that overwrites the header (docker-compose.yml + nginx template do).
FORWARDED_ALLOW_IPS = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
# Interactive OpenAPI docs are off by default on a public gateway.
DOCS_ENABLED = _env_bool("DOCS_ENABLED", False)
CORS_ALLOW_ORIGINS = _env_list("CORS_ALLOW_ORIGINS", "*")

# --- Request guards -------------------------------------------------------------
MAX_B64_CHARS = _env_int("MAX_B64_CHARS", 8_000_000)
MAX_REQUEST_BYTES = _env_int("MAX_REQUEST_BYTES", 12_000_000)

# --- Providers (all upstream, all over HTTP) ------------------------------------
# Used when a request doesn't name a provider. "worker" matches the
# extension's zero-config default; daemon2 is opt-in (needs SERVICE_URL).
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "worker").strip()
# Base URL of a service/ deployment (e.g. http://service:8080). Empty = the
# daemon2 provider is disabled on this gateway (cache hits are still served).
SERVICE_URL = os.environ.get("SERVICE_URL", "").strip().rstrip("/") or None
WORKER_URL = os.environ.get("WORKER_URL", "https://daemon-meme.windown52358.workers.dev").strip()
UPSTREAM_CONNECT_TIMEOUT_S = _env_float("UPSTREAM_CONNECT_TIMEOUT_S", 10.0)
# CPU VLM inference in service/ takes 20-30s per request, more when queued.
UPSTREAM_READ_TIMEOUT_S = _env_float("UPSTREAM_READ_TIMEOUT_S", 150.0)
UPSTREAM_MAX_CONNECTIONS = _env_int("UPSTREAM_MAX_CONNECTIONS", 100)

# BYO-key provider models — overridable so a provider retiring a model is a
# config change, not a code change.
GOOGLE_MODEL = os.environ.get("GOOGLE_MODEL", "gemini-3.1-flash-lite")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-2-vision-1212")

# --- Redis ----------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_SOCKET_TIMEOUT_S = _env_float("REDIS_SOCKET_TIMEOUT_S", 2.0)
REDIS_MAX_CONNECTIONS = _env_int("REDIS_MAX_CONNECTIONS", 50)

# --- Perceptual-hash cache (Redis, ACTIVELY GATES requests) --------------------
CACHE_ENABLED = _env_bool("CACHE_ENABLED", True)
# Two pHashes within this Hamming distance are "the same meme". Also sets the
# index segmentation (threshold + 1 segments, see cache.py) — lower values
# make near-match lookups much cheaper.
PHASH_HAMMING_THRESHOLD = _env_int("PHASH_HAMMING_THRESHOLD", 8)
# imagehash.phash hash_size=8 -> 64-bit hash -> 16 hex chars.
PHASH_HASH_SIZE = _env_int("PHASH_HASH_SIZE", 8)
# Namespace for every key this gateway writes — lets it share a Redis
# instance with other applications without collisions.
CACHE_KEY_PREFIX = os.environ.get("CACHE_KEY_PREFIX", "dnm:cache:")
# 0 = no expiry ("memes don't go stale"). Applied per entry.
CACHE_TTL_S = _env_int("CACHE_TTL_S", 0)
# Warn when one near-match lookup has to compare more candidates than this.
CACHE_SCAN_WARN_SIZE = _env_int("CACHE_SCAN_WARN_SIZE", 50_000)

# --- SQLite metrics log ---------------------------------------------------------
METRICS_ENABLED = _env_bool("METRICS_ENABLED", True)
METRICS_DB_PATH = os.environ.get("METRICS_DB_PATH", "./data/metrics.sqlite3")
# Rows are queued in memory and written in batches by one background thread
# per process, so a slow disk never stalls a request. When the queue is full
# rows are dropped (and logged), never blocking the request path.
METRICS_QUEUE_SIZE = _env_int("METRICS_QUEUE_SIZE", 10_000)
METRICS_BATCH_SIZE = _env_int("METRICS_BATCH_SIZE", 200)

# --- Output contract (same as service/) ----------------------------------------
FALLBACK_RESPONSE = {"isMeme": False, "filenameSlug": "unknown"}
