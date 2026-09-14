"""
server/ — gateway in front of the OCR+VLM pipeline (service/pipeline.py).

Adds, on top of the already-tested classification pipeline:
  - Perceptual-hash cache (Redis) that ACTIVELY GATES requests: a cache hit
    skips OCR/VLM entirely and returns immediately.
  - Per-provider rate limiting (Redis INCR+EXPIRE). BYO-API-key traffic is
    exempt (it's the user's own cost against their own provider account).
  - An append-only SQLite metrics log of every request.
  - A correction endpoint ("Rename last") that forces a fresh classification
    pass and, depending on whether the flagged result came from the cache or
    a fresh model call, either overwrites the cache entry (scenario A) or
    injects the rejected slug as a negative example into the VLM prompt
    (scenario B). See README "Rename last correction flow".

Only one provider is wired today: "daemon2" (the self-hosted Qwen3-VL+OCR
pipeline in service/). The provider dispatch table and per-provider rate
limits are structured so more providers could be routed through this same
gateway later without reshaping the request/response contract.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image

import cache
import config
import metrics
import providers
import ratelimit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meme-classifier.gateway")

# service/ is imported in-process by default (SERVICE_URL unset). This keeps
# the already-tested pipeline code completely untouched — server/ only calls
# pipeline.classify_detailed(), it doesn't reimplement any classification
# logic. See README for the SERVICE_URL split-container alternative.
#
# IMPORTANT: service/ and server/ each have their OWN modules named `config`
# and `ocr` (and server/ also has its own `ratelimit`). Naively adding
# service/'s directory to sys.path and `import pipeline` would let Python's
# module cache silently resolve pipeline.py's `import config` to server's
# config.py instead of service's — a real, silent-failure bug, not a
# hypothetical one. _import_service_pipeline() below loads service/pipeline
# with sys.modules['config']/['ocr'] transiently isolated so pipeline.py gets
# its OWN config/ocr modules, then restores server's bindings for the rest of
# this process. Do not replace this with a bare `sys.path.insert` + `import`.
def _import_service_pipeline():
    import sys as _sys

    collide_names = ("config", "ocr", "pipeline")
    saved = {name: _sys.modules.get(name) for name in collide_names}
    for name in collide_names:
        _sys.modules.pop(name, None)

    _sys.path.insert(0, config.SERVICE_IMPORT_PATH)
    try:
        import pipeline as _service_pipeline  # noqa: PLC0415
        return _service_pipeline
    finally:
        _sys.path.remove(config.SERVICE_IMPORT_PATH)
        for name, mod in saved.items():
            if mod is not None:
                _sys.modules[name] = mod
            else:
                _sys.modules.pop(name, None)


if config.SERVICE_URL is None:
    service_pipeline = _import_service_pipeline()
else:
    service_pipeline = None  # HTTP proxy path — see _classify_via_http()

app = FastAPI(title="Daemon Names Memes gateway", docs_url="/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

metrics.init_db()

_redis = None
_phash_cache = None
_rate_limiter = None


def _get_redis():
    global _redis
    if _redis is None:
        import redis as redis_lib
        _redis = redis_lib.from_url(
            config.REDIS_URL,
            socket_timeout=config.REDIS_SOCKET_TIMEOUT_S,
            socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT_S,
            decode_responses=True,
        )
    return _redis


def _get_cache() -> cache.PhashCache:
    global _phash_cache
    if _phash_cache is None:
        _phash_cache = cache.PhashCache(_get_redis())
    return _phash_cache


def _get_rate_limiter() -> ratelimit.RedisRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = ratelimit.RedisRateLimiter(_get_redis())
    return _rate_limiter


def _error(err_type: str) -> dict:
    resp = dict(config.FALLBACK_RESPONSE)
    resp["error"] = err_type
    return resp


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content=_error("internal_error"))


@app.get("/health")
async def health():
    redis_ok = True
    cache_size = -1
    try:
        _get_redis().ping()
        cache_size = _get_cache().size()
    except Exception:  # noqa: BLE001
        redis_ok = False

    warn = None
    if cache_size > config.CACHE_SCAN_WARN_SIZE:
        warn = (
            f"cache index has {cache_size} entries, above "
            f"CACHE_SCAN_WARN_SIZE={config.CACHE_SCAN_WARN_SIZE} — the linear "
            f"Hamming scan may be adding real per-request latency, see README"
        )

    return {
        "redis": {"ok": redis_ok, "cache_entries": cache_size, "warning": warn},
        "cache": {
            "enabled": config.CACHE_ENABLED,
            "hamming_threshold": config.PHASH_HAMMING_THRESHOLD,
        },
        "rate_limit": {
            "enabled": config.RATE_LIMIT_ENABLED,
            "window_s": config.RATE_LIMIT_WINDOW_S,
            "provider_limits": ratelimit.PROVIDER_LIMITS,
            "default_limit": config.RATE_LIMIT_DEFAULT_PER_WINDOW,
        },
        "metrics_db": config.METRICS_DB_PATH,
    }


def _decode_image(image_b64: str) -> tuple[Image.Image | None, str | None]:
    try:
        if "," in image_b64[:64] and image_b64.strip().lower().startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]
        raw = base64.b64decode(image_b64, validate=True)
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGB"), None
    except (binascii.Error, ValueError):
        return None, "invalid_base64"
    except Exception:  # noqa: BLE001
        return None, "invalid_image"


def _run_pipeline(provider: str, image_b64: str, mime_type: str, locale: str, mode: str,
                  reject_slug: str | None = None, api_key: str | None = None) -> dict:
    """
    Single dispatch point: routes to service/pipeline.py in-process (daemon2,
    default), a remote service/ instance over HTTP (daemon2 + SERVICE_URL
    set), the Cloudflare Worker (worker), or a BYO-API-key provider
    (google/claude/openai/...). See providers.py — every path returns the
    same detail shape so the cache/metrics/correction logic below stays
    provider-agnostic.
    """
    if provider == "daemon2" and service_pipeline is None:
        return _classify_via_http(image_b64, mime_type, locale, mode, reject_slug)
    return providers.run_provider(
        provider, service_pipeline, image_b64, mime_type, locale, mode, reject_slug, api_key
    )


def _classify_via_http(image_b64: str, mime_type: str, locale: str, mode: str,
                       reject_slug: str | None) -> dict:
    """Split-deployment path: server/ and service/ run as separate containers.
    service/app.py's /classify endpoint doesn't currently accept reject_slug —
    if you run this split, extend that endpoint to accept it, or keep
    SERVICE_URL unset (in-process import, default) to get the correction flow
    working without changes."""
    import httpx
    assert config.SERVICE_URL is not None
    t0 = time.perf_counter()
    resp = httpx.post(
        f"{config.SERVICE_URL.rstrip('/')}/classify",
        json={"image": image_b64, "mimeType": mime_type, "locale": locale, "mode": mode},
        timeout=120,
    )
    result = resp.json()
    total = time.perf_counter() - t0
    return {
        "result": {k: v for k, v in result.items() if k in ("isMeme", "filenameSlug", "error")},
        "ocr_seconds": 0.0, "vlm_seconds": total, "total_seconds": total,
        "ocr_text": "", "ocr_confidence": 0.0, "ocr_used": False,
        "vlm_ran": True, "path": "http-proxy",
    }


@app.post("/classify")
async def classify_endpoint(request: Request):
    t_start = time.perf_counter()
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > config.MAX_REQUEST_BYTES:
        return JSONResponse(status_code=413, content=_error("payload_too_large"))

    body = await request.body()
    if len(body) > config.MAX_REQUEST_BYTES:
        return JSONResponse(status_code=413, content=_error("payload_too_large"))

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content=_error("invalid_json_body"))

    image = payload.get("image") or ""
    mime_type = payload.get("mimeType") or "image/png"
    locale = payload.get("locale") or ""
    mode = payload.get("mode") or "auto"
    provider = payload.get("provider") or "daemon2"
    api_key = payload.get("apiKey")  # BYO-key providers only
    if not all(isinstance(v, str) for v in (image, mime_type, locale, mode, provider)):
        return JSONResponse(status_code=400, content=_error("invalid_field_types"))
    if provider not in providers.KNOWN_PROVIDERS:
        return JSONResponse(status_code=400, content=_error("unknown_provider"))

    client_key_val = ratelimit.client_key(request)

    # --- Rate limiting: exempt for BYO-key providers (the request carries
    # the user's own API key — that traffic is their own cost/quota with
    # their provider, not this gateway's to police). daemon2 and worker are
    # NOT exempt and always go through check_and_record().
    is_byo_key = provider in providers.BYO_KEY_PROVIDERS
    if not is_byo_key:
        allowed, remaining, retry_after = _get_rate_limiter().check_and_record(provider, client_key_val)
        if not allowed:
            log.warning("Rate limit hit for provider=%s client=%s", provider, client_key_val)
            metrics.log_classification(
                provider=provider, mode=mode, client_key=client_key_val,
                error="rate_limited", total_seconds=time.perf_counter() - t_start,
            )
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited", "retry_after_seconds": retry_after},
            )
    else:
        remaining = -1

    if not image:
        return JSONResponse(status_code=200, content=_error("empty_image"))

    img, decode_err = _decode_image(image)
    if decode_err:
        metrics.log_classification(provider=provider, mode=mode, client_key=client_key_val,
                                   error=decode_err, total_seconds=time.perf_counter() - t_start)
        return JSONResponse(content=_error(decode_err))

    phash = cache.compute_phash(img)

    # --- Cache gate: a hit skips OCR/VLM entirely ---
    lookup = _get_cache().lookup(phash)
    if lookup.hit:
        log.info("cache HIT phash=%s matched=%s dist=%s", phash, lookup.matched_phash, lookup.hamming_distance)
        metrics.log_classification(
            provider=provider, mode=mode, client_key=client_key_val, phash=phash,
            cache_hit=True, cache_hamming_distance=lookup.hamming_distance,
            is_meme=lookup.result["isMeme"], filename_slug=lookup.result["filenameSlug"],
            total_seconds=time.perf_counter() - t_start,
        )
        resp = JSONResponse(content=lookup.result)
        if remaining >= 0:
            resp.headers["X-RateLimit-Remaining"] = str(remaining)
        resp.headers["X-Cache"] = "HIT"
        resp.headers["X-Phash"] = phash
        return resp

    # --- Cache miss: run the full pipeline ---
    detail = _run_pipeline(provider, image, mime_type, locale, mode, api_key=api_key)
    result = detail["result"]
    if "error" not in result:
        _get_cache().store(phash, result["isMeme"], result["filenameSlug"])

    metrics.log_classification(
        provider=provider, mode=mode, client_key=client_key_val, phash=phash,
        cache_hit=False, ocr_used=detail.get("ocr_used", False), vlm_ran=detail.get("vlm_ran", False),
        ocr_seconds=detail.get("ocr_seconds"), vlm_seconds=detail.get("vlm_seconds"),
        total_seconds=detail.get("total_seconds"),
        is_meme=result.get("isMeme"), filename_slug=result.get("filenameSlug"),
        error=result.get("error"),
    )

    resp = JSONResponse(content=result)
    if remaining >= 0:
        resp.headers["X-RateLimit-Remaining"] = str(remaining)
    resp.headers["X-Cache"] = "MISS"
    resp.headers["X-Phash"] = phash
    return resp


@app.post("/correct")
async def correct_endpoint(request: Request):
    """
    "Rename last" correction flow. Request:
        {
          "image": "<base64 of the ORIGINAL image>", "mimeType", "locale", "mode",
          "provider": "daemon2",
          "phash": "<phash stored from the flagged result>",
          "cache_hit": bool,           # was the flagged result served from cache?
          "previous_slug": "<the filenameSlug the user is flagging as wrong>"
        }
    See README "Rename last correction flow" for scenario A vs B.
    """
    t_start = time.perf_counter()
    body = await request.body()
    if len(body) > config.MAX_REQUEST_BYTES:
        return JSONResponse(status_code=413, content=_error("payload_too_large"))
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content=_error("invalid_json_body"))

    image = payload.get("image") or ""
    mime_type = payload.get("mimeType") or "image/png"
    locale = payload.get("locale") or ""
    mode = payload.get("mode") or "auto"
    provider = payload.get("provider") or "daemon2"
    stored_phash = payload.get("phash") or ""
    was_cache_hit = bool(payload.get("cache_hit"))
    previous_slug = payload.get("previous_slug") or ""
    api_key = payload.get("apiKey")

    if not all(isinstance(v, str) for v in (image, mime_type, locale, mode, provider, stored_phash, previous_slug)):
        return JSONResponse(status_code=400, content=_error("invalid_field_types"))
    if provider not in providers.KNOWN_PROVIDERS:
        return JSONResponse(status_code=400, content=_error("unknown_provider"))
    if not image:
        return JSONResponse(content=_error("empty_image"))

    client_key_val = ratelimit.client_key(request)
    is_byo_key = provider in providers.BYO_KEY_PROVIDERS
    if not is_byo_key:
        allowed, remaining, retry_after = _get_rate_limiter().check_and_record(provider, client_key_val)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited", "retry_after_seconds": retry_after},
            )

    img, decode_err = _decode_image(image)
    if decode_err:
        return JSONResponse(content=_error(decode_err))

    # Always force a fresh model pass with the rejected slug as a negative
    # example — that's true for both scenario A and B (only the cache
    # handling afterward differs).
    detail = _run_pipeline(provider, image, mime_type, locale, mode,
                           reject_slug=previous_slug, api_key=api_key)
    result = detail["result"]

    if "error" in result:
        metrics.log_classification(
            provider=provider, mode=mode, client_key=client_key_val, phash=stored_phash or None,
            cache_hit=False, was_correction=True, previous_wrong_slug=previous_slug,
            correction_was_cache_hit=was_cache_hit,
            error=result["error"], total_seconds=detail.get("total_seconds"),
        )
        return JSONResponse(content=result)

    fresh_phash = cache.compute_phash(img)

    if was_cache_hit:
        # Scenario A: overwrite the existing cache entry so future images
        # that would've matched it get the corrected answer instead.
        target_phash = stored_phash or fresh_phash
        updated = _get_cache().update(target_phash, result["isMeme"], result["filenameSlug"])
        if not updated:
            # Entry vanished (evicted/expired) — store fresh under the
            # current phash so the correction isn't silently lost.
            _get_cache().store(fresh_phash, result["isMeme"], result["filenameSlug"])
        log.info("correction (cache-hit path): phash=%s updated=%s", target_phash, updated)
    else:
        # Scenario B: the original wrong result was never cached — don't
        # touch the cache. (If this fresh pass happens to also be a cache
        # miss under its own phash, it is NOT auto-stored here either: a
        # correction result reflects one user's judgment on one request,
        # not necessarily meant to become everyone else's cached answer.)
        log.info("correction (fresh-call path): phash=%s (cache untouched)", fresh_phash)

    metrics.log_classification(
        provider=provider, mode=mode, client_key=client_key_val, phash=fresh_phash,
        cache_hit=False, ocr_used=detail.get("ocr_used", False), vlm_ran=detail.get("vlm_ran", False),
        ocr_seconds=detail.get("ocr_seconds"), vlm_seconds=detail.get("vlm_seconds"),
        total_seconds=detail.get("total_seconds"),
        is_meme=result.get("isMeme"), filename_slug=result.get("filenameSlug"),
        was_correction=True, previous_wrong_slug=previous_slug,
        correction_was_cache_hit=was_cache_hit,
    )

    return JSONResponse(content=result)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
