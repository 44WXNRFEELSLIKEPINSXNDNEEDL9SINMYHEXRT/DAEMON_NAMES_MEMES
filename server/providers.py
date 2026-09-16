"""
Provider dispatch — every classification backend the extension can use,
behind ONE async interface, so app.py's cache + metrics logic is
provider-agnostic. The gateway never runs a model itself: every provider is
an upstream HTTP call over one shared, pooled httpx.AsyncClient.

Providers:
  daemon2  — a service/ deployment at SERVICE_URL (OCR+VLM pipeline). Opt-in:
             with SERVICE_URL unset the provider answers 503, cache hits are
             still served. The client IP is forwarded as X-Forwarded-For so
             service/'s own per-IP limiter keeps working behind the gateway
             (run service/ with RATE_LIMIT_TRUST_PROXY=1 and don't expose it).
  worker   — the Cloudflare Worker at WORKER_URL (worker/src/index.ts).
  google / claude / openai / openrouter / groq / mistral / xai — BYO-API-key
             providers. The user's key is forwarded per request, never stored
             or logged.

Every provider returns a ProviderResult: the {isMeme, filenameSlug[, error]}
contract plus timing metadata and the HTTP status the gateway should answer
with (200 normally; 429/502/503/504 when the upstream failed in a way the
client should see, instead of a fake "not a meme" answer).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

import config

log = logging.getLogger("meme-classifier.providers")

OPENAI_COMPATIBLE = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
}

BYO_KEY_PROVIDERS = frozenset({"google", "claude", *OPENAI_COMPATIBLE})
KNOWN_PROVIDERS = frozenset({"daemon2", "worker", *BYO_KEY_PROVIDERS})


def model_for(provider: str) -> str:
    return {
        "google": config.GOOGLE_MODEL,
        "claude": config.CLAUDE_MODEL,
        "openai": config.OPENAI_MODEL,
        "openrouter": config.OPENROUTER_MODEL,
        "groq": config.GROQ_MODEL,
        "mistral": config.MISTRAL_MODEL,
        "xai": config.XAI_MODEL,
    }[provider]


def assumes_meme_in_manual_mode(provider: str) -> bool:
    """daemon2 treats manual mode as "user says it's a meme" and never asks
    the model for isMeme; every other provider decides isMeme regardless."""
    return provider == "daemon2"


@dataclass
class ProviderResult:
    result: dict
    status: int = 200
    retry_after: int | None = None
    ocr_seconds: float = 0.0
    vlm_seconds: float = 0.0
    total_seconds: float = 0.0
    ocr_used: bool = False
    vlm_ran: bool = False
    path: str = "provider"

    @property
    def ok(self) -> bool:
        return self.status == 200 and "error" not in self.result


def _error(err_type: str, seconds: float = 0.0, status: int = 200,
           retry_after: int | None = None) -> ProviderResult:
    return ProviderResult(
        result={"isMeme": False, "filenameSlug": "unknown", "error": err_type},
        status=status, retry_after=retry_after, total_seconds=seconds, path="error",
    )


_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _sanitize_slug(slug: str) -> str:
    slug = _UNSAFE_RE.sub("", slug or "").strip()
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:80] or "unknown"


def _retry_after(resp: httpx.Response) -> int | None:
    try:
        return max(0, int(float(resp.headers["retry-after"])))
    except (KeyError, ValueError):
        return None


def _upstream_failure(provider: str, resp: httpx.Response, seconds: float) -> ProviderResult:
    if resp.status_code == 429:
        return _error("rate_limited", seconds, status=429, retry_after=_retry_after(resp) or 60)
    if resp.status_code in (401, 403):
        return _error(f"{provider}_unauthorized", seconds, status=502)
    return _error(f"{provider}_http_{resp.status_code}", seconds, status=502)


def _transport_failure(provider: str, exc: httpx.HTTPError, seconds: float) -> ProviderResult:
    status = 504 if isinstance(exc, httpx.TimeoutException) else 502
    log.warning("%s upstream %s: %s", provider, type(exc).__name__, exc)
    return _error(f"{provider}_{type(exc).__name__}", seconds, status=status)


def build_prompt(locale: str, reject_slug: str | None) -> str:
    """Same prompt the extension's background.js sends directly, plus the
    negative example for the correction flow (wording shared with
    service/pipeline.py and worker/src/index.ts)."""
    rejection = ""
    if reject_slug:
        rejection = (
            f"\nA previous attempt at naming this image produced: '{reject_slug}'. "
            f"This was flagged as incorrect by the user. Look at the image again "
            f"more carefully and produce a different, more accurate description — "
            f"do not repeat the previous answer or a close variant of it.\n"
        )
    return f"""Look at this image. Determine if it's a meme (has overlaid text, a recognizable meme template, or is clearly satirical/humorous internet content).
{rejection}Respond ONLY with JSON in this exact shape, no markdown fences:
{{"isMeme": boolean, "filenameSlug": "short-kebab-case-description"}}

filenameSlug rules:
- 3-6 words, lowercase, hyphenated.
- If the image contains visible text, base the slug on that text's meaning and write it in that text's own language and native script (Cyrillic, Arabic, Devanagari, Hangul, etc). Do NOT transliterate or romanize into Latin letters.
- If there is no visible text in the image, default to this language: {locale}.
- Must be safe as a filename (no slashes, colons, or quotes)."""


def _parse_model_json(raw_text: str | None) -> dict:
    try:
        cleaned = (raw_text or "").strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        obj = json.loads(cleaned)
        return {
            "isMeme": bool(obj.get("isMeme")),
            "filenameSlug": _sanitize_slug(str(obj.get("filenameSlug", ""))),
        }
    except (json.JSONDecodeError, TypeError, AttributeError):
        log.warning("Unparseable provider output: %r", (raw_text or "")[:300])
        return {"isMeme": False, "filenameSlug": "unknown", "error": "bad_model_output"}


def _header_float(resp: httpx.Response, name: str) -> float:
    try:
        return float(resp.headers.get(name, 0.0))
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# daemon2 — service/ over HTTP
# --------------------------------------------------------------------------
async def run_daemon2(client: httpx.AsyncClient, image_b64: str, mime_type: str, locale: str,
                      mode: str, reject_slug: str | None, client_ip: str | None) -> ProviderResult:
    if not config.SERVICE_URL:
        return _error("provider_unavailable", status=503)
    body = {"image": image_b64, "mimeType": mime_type, "locale": locale, "mode": mode}
    if reject_slug:
        body["rejectSlug"] = reject_slug
    headers = {"X-Forwarded-For": client_ip} if client_ip else {}

    t0 = time.perf_counter()
    try:
        resp = await client.post(f"{config.SERVICE_URL}/classify", json=body, headers=headers)
    except httpx.HTTPError as e:
        return _transport_failure("daemon2", e, time.perf_counter() - t0)
    seconds = time.perf_counter() - t0

    if resp.status_code != 200:
        return _upstream_failure("daemon2", resp, seconds)
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return _error("bad_model_output", seconds, status=502)
    if not isinstance(data, dict):
        return _error("bad_model_output", seconds, status=502)
    if "error" in data:
        # service/'s contract errors (vlm_loading, invalid_image, ...) are
        # answered with 200 + error body there; pass them through unchanged.
        return _error(str(data["error"]), seconds)

    # service/app.py reports per-stage timings as response headers.
    return ProviderResult(
        result={
            "isMeme": bool(data.get("isMeme")),
            "filenameSlug": _sanitize_slug(str(data.get("filenameSlug", ""))),
        },
        ocr_seconds=_header_float(resp, "x-ocr-seconds"),
        vlm_seconds=_header_float(resp, "x-vlm-seconds"),
        total_seconds=seconds,
        ocr_used=resp.headers.get("x-ocr-used") == "1",
        vlm_ran=resp.headers.get("x-vlm-ran", "1") == "1",
        path=f"service:{resp.headers.get('x-pipeline-path', 'unknown')}",
    )


# --------------------------------------------------------------------------
# worker — Cloudflare Worker
# --------------------------------------------------------------------------
async def run_worker(client: httpx.AsyncClient, image_b64: str, mime_type: str, locale: str,
                     reject_slug: str | None) -> ProviderResult:
    if not config.WORKER_URL:
        return _error("provider_unavailable", status=503)
    body = {"image": image_b64, "mimeType": mime_type, "locale": locale}
    if reject_slug:
        body["rejectSlug"] = reject_slug

    t0 = time.perf_counter()
    try:
        resp = await client.post(config.WORKER_URL, json=body)
    except httpx.HTTPError as e:
        return _transport_failure("worker", e, time.perf_counter() - t0)
    seconds = time.perf_counter() - t0

    if resp.status_code != 200:
        return _upstream_failure("worker", resp, seconds)
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return _error("bad_model_output", seconds, status=502)
    if not isinstance(data, dict):
        return _error("bad_model_output", seconds, status=502)
    if "error" in data:
        return _error(str(data["error"]), seconds)

    return ProviderResult(
        result={
            "isMeme": bool(data.get("isMeme")),
            "filenameSlug": _sanitize_slug(str(data.get("filenameSlug", ""))),
        },
        vlm_seconds=seconds, total_seconds=seconds, vlm_ran=True, path="worker",
    )


# --------------------------------------------------------------------------
# BYO-API-key providers
# --------------------------------------------------------------------------
def _byo_request(provider: str, image_b64: str, mime_type: str, prompt: str,
                 api_key: str, model: str | None = None) -> tuple[str, dict, dict]:
    model = model or model_for(provider)
    if provider == "google":
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": api_key},
            {
                "contents": [{"parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": image_b64}},
                ]}],
                "generationConfig": {"response_mime_type": "application/json"},
            },
        )
    if provider == "claude":
        return (
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            {
                "model": model, "max_tokens": 300,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                    {"type": "text", "text": prompt},
                ]}],
            },
        )
    return (
        OPENAI_COMPATIBLE[provider],
        {"Authorization": f"Bearer {api_key}"},
        {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
            ]}],
        },
    )


def _byo_text(provider: str, data: dict) -> str | None:
    if provider == "google":
        return data["candidates"][0]["content"]["parts"][0]["text"]
    if provider == "claude":
        return "".join(block.get("text", "") for block in data.get("content", []))
    return data["choices"][0]["message"]["content"]


async def run_byo_key(client: httpx.AsyncClient, provider: str, image_b64: str, mime_type: str,
                      locale: str, api_key: str, reject_slug: str | None,
                      model: str | None = None) -> ProviderResult:
    if not api_key:
        return _error("missing_api_key", status=400)

    url, headers, body = _byo_request(provider, image_b64, mime_type,
                                      build_prompt(locale, reject_slug), api_key, model)
    t0 = time.perf_counter()
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        return _transport_failure(provider, e, time.perf_counter() - t0)
    seconds = time.perf_counter() - t0

    if resp.status_code != 200:
        return _upstream_failure(provider, resp, seconds)
    try:
        parsed = _parse_model_json(_byo_text(provider, resp.json()))
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        log.warning("Unexpected %s response shape", provider)
        return _error("bad_model_output", seconds)

    if "error" in parsed:
        return _error(parsed["error"], seconds)
    return ProviderResult(result=parsed, vlm_seconds=seconds, total_seconds=seconds,
                          vlm_ran=True, path=f"{provider}-byo-key")


async def run_provider(client: httpx.AsyncClient, provider: str, image_b64: str, mime_type: str,
                       locale: str, mode: str, reject_slug: str | None = None,
                       api_key: str | None = None, client_ip: str | None = None,
                       model: str | None = None) -> ProviderResult:
    """Single dispatch point app.py calls."""
    if provider == "daemon2":
        return await run_daemon2(client, image_b64, mime_type, locale, mode, reject_slug, client_ip)
    if provider == "worker":
        return await run_worker(client, image_b64, mime_type, locale, reject_slug)
    if provider in BYO_KEY_PROVIDERS:
        return await run_byo_key(client, provider, image_b64, mime_type, locale, api_key or "", reject_slug, model)
    return _error("unknown_provider", status=400)


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            config.UPSTREAM_READ_TIMEOUT_S, connect=config.UPSTREAM_CONNECT_TIMEOUT_S,
        ),
        limits=httpx.Limits(
            max_connections=config.UPSTREAM_MAX_CONNECTIONS,
            max_keepalive_connections=min(20, config.UPSTREAM_MAX_CONNECTIONS),
        ),
        headers={"Content-Type": "application/json"},
        follow_redirects=False,
    )
