"""
server/ — lite gateway in front of the classification providers.

It does exactly two things on top of the providers:
  - Perceptual-hash cache (Redis) that ACTIVELY GATES requests: a hit skips
    the provider call entirely and returns immediately.
  - An append-only SQLite metrics log of every request.

Plus the "Rename last" correction endpoint (/correct), which forces a fresh
provider pass with the rejected slug as a negative example and, when the
flagged result came from the cache, overwrites that cache entry.

Model work always happens upstream over HTTP (see providers.py): service/ at
SERVICE_URL (daemon2), the Cloudflare Worker, or BYO-key provider APIs.
Rate limiting is deliberately NOT done here — nginx limits at the edge,
service/ and the worker enforce their own per-client limits, and BYO-key
traffic is the user's own quota.

Everything on the request path is non-blocking: async Redis, one pooled
async HTTP client, image decode + pHash in a worker thread, metrics enqueued
to a background writer. All shared state lives in Redis/SQLite, so the app
can run with several uvicorn workers.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import io
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis_asyncio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image

import cache
import config
import metrics
import providers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("meme-classifier.gateway")

MAX_REJECT_SLUG_CHARS = 120
EXPOSED_HEADERS = ["X-Cache", "X-Phash", "Retry-After"]


def _error(err_type: str) -> dict:
    resp = dict(config.FALLBACK_RESPONSE)
    resp["error"] = err_type
    return resp


def _json_error(status: int, err_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=_error(err_type))


def _decode_and_hash(image_b64: str) -> tuple[str | None, str | None, str | None, str | None]:
    """(clean_b64, phash, detected_mime, error). CPU-bound: run in a thread."""
    try:
        if image_b64[:5].lower() == "data:" and "," in image_b64[:128]:
            image_b64 = image_b64.split(",", 1)[1]
        raw = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        return None, None, None, "invalid_base64"
    try:
        img = Image.open(io.BytesIO(raw))
        mime = Image.MIME.get(img.format or "")
        # JPEG draft decode at reduced scale: pHash downsamples to 32x32 anyway.
        with contextlib.suppress(Exception):
            img.draft("RGB", (512, 512))
        img.load()
        return image_b64, cache.compute_phash(img.convert("RGB")), mime, None
    except Exception:  # noqa: BLE001
        return None, None, None, "invalid_image"


async def _read_payload(request: Request) -> dict | JSONResponse:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > config.MAX_REQUEST_BYTES:
        return _json_error(413, "payload_too_large")
    body = await request.body()
    if len(body) > config.MAX_REQUEST_BYTES:
        return _json_error(413, "payload_too_large")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_error(400, "invalid_json_body")
    if not isinstance(payload, dict):
        return _json_error(400, "invalid_json_body")
    return payload


def _common_fields(payload: dict) -> tuple[dict, JSONResponse | None]:
    fields = {
        "image": payload.get("image") or "",
        "mime_type": payload.get("mimeType") or "image/png",
        "locale": payload.get("locale") or "",
        "mode": payload.get("mode") or "auto",
        "provider": payload.get("provider") or config.DEFAULT_PROVIDER,
        "api_key": payload.get("apiKey") or None,
        # Optional per-request override of the provider's configured default
        # model (extension Settings → a provider's "Model" field).
        "model": payload.get("model") or None,
    }
    if not all(isinstance(fields[k], str) for k in ("image", "mime_type", "locale", "mode", "provider")) \
            or not isinstance(fields["api_key"], (str, type(None))) \
            or not isinstance(fields["model"], (str, type(None))):
        return fields, _json_error(400, "invalid_field_types")
    fields["mode"] = fields["mode"].strip().lower()
    if fields["mode"] not in ("auto", "manual"):
        return fields, _json_error(400, "invalid_mode")
    if fields["provider"] not in providers.KNOWN_PROVIDERS:
        return fields, _json_error(400, "unknown_provider")
    if len(fields["image"]) > config.MAX_B64_CHARS:
        return fields, _json_error(413, "payload_too_large")
    fields["locale"] = fields["locale"][:35]
    return fields, None


def _client_ip(request: Request) -> str | None:
    # uvicorn's proxy-headers middleware already resolved X-Forwarded-For for
    # trusted peers only (FORWARDED_ALLOW_IPS) — never parse it here.
    return request.client.host if request.client else None


def _provider_response(pr: providers.ProviderResult, extra_headers: dict) -> JSONResponse:
    if pr.status == 429:
        retry = pr.retry_after or 60
        resp = JSONResponse(status_code=429,
                            content={"error": "rate_limited", "retry_after_seconds": retry})
        resp.headers["Retry-After"] = str(retry)
    else:
        resp = JSONResponse(status_code=pr.status, content=pr.result)
    resp.headers.update(extra_headers)
    return resp


def create_app(*, redis_client=None, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    """Build the app. Tests inject fakeredis / a mocked httpx transport."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_redis = redis_client is None
        own_http = http_client is None
        r = redis_client if not own_redis else redis_asyncio.from_url(
            config.REDIS_URL,
            socket_timeout=config.REDIS_SOCKET_TIMEOUT_S,
            socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT_S,
            max_connections=config.REDIS_MAX_CONNECTIONS,
            decode_responses=True,
            health_check_interval=30,
        )
        app.state.cache = cache.PhashCache(r)
        app.state.http = http_client if not own_http else providers.new_http_client()
        app.state.metrics = metrics.MetricsWriter()
        await asyncio.to_thread(app.state.metrics.start)
        if not await app.state.cache.ping():
            log.warning("Redis at %s is unreachable; serving without cache until it recovers",
                        config.REDIS_URL.split("@")[-1])
        log.info("gateway ready | daemon2=%s worker=%s cache=%s metrics=%s",
                 bool(config.SERVICE_URL), bool(config.WORKER_URL),
                 config.CACHE_ENABLED, app.state.metrics.running)
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.metrics.stop)
            if own_http:
                await app.state.http.aclose()
            if own_redis:
                with contextlib.suppress(Exception):
                    await r.aclose()

    app = FastAPI(
        title="Daemon Names Memes gateway",
        lifespan=lifespan,
        docs_url="/docs" if config.DOCS_ENABLED else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.DOCS_ENABLED else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ALLOW_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        expose_headers=EXPOSED_HEADERS,
    )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        log.exception("Unhandled error on %s", request.url.path)
        return _json_error(500, "internal_error")

    @app.get("/health")
    async def health(request: Request):
        redis_ok = await request.app.state.cache.ping()
        return {
            "status": "ok" if redis_ok or not config.CACHE_ENABLED else "degraded",
            "redis": {"ok": redis_ok},
            "cache": {
                "enabled": config.CACHE_ENABLED,
                "hamming_threshold": config.PHASH_HAMMING_THRESHOLD,
                "ttl_s": config.CACHE_TTL_S,
            },
            "metrics": {"enabled": request.app.state.metrics.running},
            "providers": {
                "daemon2": bool(config.SERVICE_URL),
                "worker": bool(config.WORKER_URL),
                "byo_key": sorted(providers.BYO_KEY_PROVIDERS),
                "default": config.DEFAULT_PROVIDER,
            },
        }

    @app.post("/classify")
    async def classify_endpoint(request: Request):
        t_start = time.perf_counter()
        payload = await _read_payload(request)
        if isinstance(payload, JSONResponse):
            return payload
        f, err = _common_fields(payload)
        if err:
            return err
        if not f["image"]:
            return JSONResponse(content=_error("empty_image"))

        state = request.app.state
        client_ip = _client_ip(request)
        provider, mode = f["provider"], f["mode"]

        image_b64, phash, detected_mime, decode_err = await asyncio.to_thread(_decode_and_hash, f["image"])
        if decode_err:
            state.metrics.record(provider=provider, mode=mode, client_key=client_ip,
                                 error=decode_err, total_seconds=time.perf_counter() - t_start)
            return JSONResponse(content=_error(decode_err))

        # daemon2 manual mode assumes isMeme=True: such results may reuse any
        # cached slug, but must never be served as an isMeme verdict later.
        assumed = mode == "manual" and providers.assumes_meme_in_manual_mode(provider)

        lookup = await state.cache.lookup(phash, require_known_is_meme=not assumed)
        if lookup.hit:
            result = dict(lookup.result)
            if assumed:
                result["isMeme"] = True
            state.metrics.record(
                provider=provider, mode=mode, client_key=client_ip, phash=phash,
                cache_hit=True, cache_hamming_distance=lookup.hamming_distance,
                is_meme=result["isMeme"], filename_slug=result["filenameSlug"],
                total_seconds=time.perf_counter() - t_start,
            )
            return JSONResponse(content=result, headers={"X-Cache": "HIT", "X-Phash": phash})

        pr = await providers.run_provider(
            state.http, provider, image_b64, detected_mime or f["mime_type"], f["locale"], mode,
            api_key=f["api_key"], client_ip=client_ip, model=f["model"],
        )
        if pr.ok:
            await state.cache.store(phash, pr.result, is_meme_known=not assumed, overwrite=not assumed)

        state.metrics.record(
            provider=provider, mode=mode, client_key=client_ip, phash=phash, cache_hit=False,
            ocr_used=pr.ocr_used, vlm_ran=pr.vlm_ran, ocr_seconds=pr.ocr_seconds,
            vlm_seconds=pr.vlm_seconds, total_seconds=time.perf_counter() - t_start,
            is_meme=pr.result.get("isMeme") if pr.ok else None,
            filename_slug=pr.result.get("filenameSlug") if pr.ok else None,
            error=pr.result.get("error"),
        )
        return _provider_response(pr, {"X-Cache": "MISS", "X-Phash": phash})

    @app.post("/correct")
    async def correct_endpoint(request: Request):
        """
        "Rename last" correction flow. Request:
            {
              "image": "<base64 of the ORIGINAL image>", "mimeType", "locale", "mode",
              "provider", "apiKey", "model",   # model: optional, overrides the provider's configured default

              "phash": "<X-Phash from the flagged result>",
              "cache_hit": bool,           # was the flagged result served from cache?
              "previous_slug": "<the filenameSlug the user is flagging as wrong>"
            }
        See README "Rename last correction flow" for scenario A vs B.
        """
        t_start = time.perf_counter()
        payload = await _read_payload(request)
        if isinstance(payload, JSONResponse):
            return payload
        f, err = _common_fields(payload)
        if err:
            return err
        stored_phash = payload.get("phash") or ""
        previous_slug = payload.get("previous_slug") or ""
        was_cache_hit = payload.get("cache_hit")
        was_cache_hit = False if was_cache_hit is None else was_cache_hit
        if not isinstance(stored_phash, str) or not isinstance(previous_slug, str) \
                or not isinstance(was_cache_hit, bool):
            return _json_error(400, "invalid_field_types")
        if not f["image"]:
            return JSONResponse(content=_error("empty_image"))
        previous_slug = previous_slug.strip()[:MAX_REJECT_SLUG_CHARS]

        state = request.app.state
        client_ip = _client_ip(request)
        provider, mode = f["provider"], f["mode"]

        image_b64, fresh_phash, detected_mime, decode_err = await asyncio.to_thread(_decode_and_hash, f["image"])
        if decode_err:
            return JSONResponse(content=_error(decode_err))

        # Both scenarios force a fresh provider pass with the rejected slug as
        # a negative example; only the cache handling afterwards differs.
        pr = await providers.run_provider(
            state.http, provider, image_b64, detected_mime or f["mime_type"], f["locale"], mode,
            reject_slug=previous_slug or None, api_key=f["api_key"], client_ip=client_ip,
            model=f["model"],
        )
        common = dict(provider=provider, mode=mode, client_key=client_ip, cache_hit=False,
                      was_correction=True, previous_wrong_slug=previous_slug,
                      correction_was_cache_hit=was_cache_hit)
        if not pr.ok:
            state.metrics.record(**common, phash=fresh_phash, error=pr.result.get("error"),
                                 total_seconds=time.perf_counter() - t_start)
            return _provider_response(pr, {"X-Phash": fresh_phash})

        assumed = mode == "manual" and providers.assumes_meme_in_manual_mode(provider)
        if was_cache_hit:
            # Scenario A: overwrite the entry that served the wrong answer so
            # near-duplicates get the corrected one. If it vanished (expired /
            # evicted), store under the current phash so the fix isn't lost.
            target = stored_phash if cache.is_valid_phash(stored_phash) else fresh_phash
            updated = await state.cache.update(target, pr.result, is_meme_known=not assumed)
            if not updated:
                await state.cache.store(fresh_phash, pr.result, is_meme_known=not assumed)
            log.info("correction (cache-hit path): phash=%s updated=%s", target, updated)
        else:
            # Scenario B: the wrong answer was never cached; a correction is
            # one user's judgment, not automatically everyone's cached answer.
            log.info("correction (fresh-call path): phash=%s (cache untouched)", fresh_phash)

        state.metrics.record(
            **common, phash=fresh_phash, ocr_used=pr.ocr_used, vlm_ran=pr.vlm_ran,
            ocr_seconds=pr.ocr_seconds, vlm_seconds=pr.vlm_seconds,
            total_seconds=time.perf_counter() - t_start,
            is_meme=pr.result["isMeme"], filename_slug=pr.result["filenameSlug"],
        )
        return JSONResponse(content=pr.result, headers={"X-Phash": fresh_phash})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, proxy_headers=True,
                forwarded_allow_ips=config.FORWARDED_ALLOW_IPS)
