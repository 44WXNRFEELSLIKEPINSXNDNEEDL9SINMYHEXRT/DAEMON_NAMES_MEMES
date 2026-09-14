"""
Provider dispatch — every classification backend the extension can use,
behind ONE interface so they all share the gateway's Redis cache, Redis
rate limiting, and SQLite metrics log (server/app.py enforces those
uniformly regardless of which provider ends up doing the actual model call).

Providers:
  daemon2  — self-hosted OCR+VLM pipeline (service/pipeline.py), imported
             in-process. The only provider with real manual/auto mode
             semantics and reject_slug-based correction built in natively.
  worker   — proxies to the Cloudflare Worker (worker/src/index.ts). Needs
             its own reject_slug support added there for the correction flow
             to produce a genuinely different answer (done — see
             worker/src/index.ts's rejectSlug handling).
  google / claude / openai / openrouter / groq / mistral / xai — BYO-API-key
             providers. The gateway calls the same HTTP APIs background.js
             used to call directly, so a self-hosted gateway becomes a
             transparent proxy: same cost model (user's own key/quota), but
             now benefits from the shared cache + shared metrics log too.

Every provider function returns the same "detail" shape as
service/pipeline.classify_detailed() so app.py's cache/metrics/correction
logic is provider-agnostic:

    {
      "result": {"isMeme": bool, "filenameSlug": str} | error contract,
      "ocr_seconds": float, "vlm_seconds": float, "total_seconds": float,
      "vlm_ran": bool, "path": str,
    }
"""

from __future__ import annotations

import json
import logging
import re
import time

import config

log = logging.getLogger("meme-classifier.providers")

GOOGLE_MODEL = "gemini-3.1-flash-lite"
CLAUDE_MODEL = "claude-3-5-haiku-latest"

OPENAI_COMPATIBLE = {
    "openai":     {"endpoint": "https://api.openai.com/v1/chat/completions",       "model": "gpt-4o-mini"},
    "openrouter": {"endpoint": "https://openrouter.ai/api/v1/chat/completions",    "model": "openai/gpt-4o-mini"},
    "groq":       {"endpoint": "https://api.groq.com/openai/v1/chat/completions",  "model": "llama-3.2-90b-vision-preview"},
    "mistral":    {"endpoint": "https://api.mistral.ai/v1/chat/completions",       "model": "pixtral-12b-2409"},
    "xai":        {"endpoint": "https://api.x.ai/v1/chat/completions",            "model": "grok-2-vision-1212"},
}

BYO_KEY_PROVIDERS = frozenset({"google", "claude", *OPENAI_COMPATIBLE.keys()})
KNOWN_PROVIDERS = frozenset({"daemon2", "worker", *BYO_KEY_PROVIDERS})


def _empty_detail(result: dict, seconds: float = 0.0, vlm_ran: bool = True, path: str = "provider") -> dict:
    return {
        "result": result,
        "ocr_seconds": 0.0, "vlm_seconds": seconds, "total_seconds": seconds,
        "ocr_used": False, "vlm_ran": vlm_ran, "path": path,
    }


def _error_detail(err_type: str, seconds: float = 0.0) -> dict:
    return _empty_detail({"isMeme": False, "filenameSlug": "unknown", "error": err_type}, seconds, vlm_ran=False, path="error")


_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _sanitize_slug(slug: str) -> str:
    slug = _UNSAFE_RE.sub("", slug or "").strip()
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:80] or "unknown"


def _build_legacy_prompt(locale: str, reject_slug: str | None) -> str:
    """Same prompt shape the extension's own background.js used to send
    directly to these BYO-key providers, plus the negative-example injection
    for the correction flow (identical wording to service/pipeline.py's, so
    correction behavior is consistent across every provider)."""
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


def _parse_legacy_json(raw_text: str | None) -> dict:
    try:
        cleaned = (raw_text or "").strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        obj = json.loads(cleaned)
        is_meme = bool(obj.get("isMeme"))
        slug = _sanitize_slug(str(obj.get("filenameSlug", "")))
        return {"isMeme": is_meme, "filenameSlug": slug}
    except (json.JSONDecodeError, TypeError, AttributeError):
        log.warning("Unparseable provider output: %r", (raw_text or "")[:300])
        return {"isMeme": False, "filenameSlug": "unknown", "error": "bad_model_output"}


# --------------------------------------------------------------------------
# daemon2 — in-process service/pipeline.py (already tested elsewhere)
# --------------------------------------------------------------------------
def run_daemon2(service_pipeline, image_b64: str, mime_type: str, locale: str,
                mode: str, reject_slug: str | None) -> dict:
    return service_pipeline.classify_detailed(
        image_b64, mime_type, locale, mode, reject_slug=reject_slug
    )


# --------------------------------------------------------------------------
# worker — proxies to the Cloudflare Worker (own Gemini key + own DO rate
# limit; the gateway's rate limiter and cache still apply on top, since
# every provider shares them — see app.py).
# --------------------------------------------------------------------------
def run_worker(image_b64: str, mime_type: str, locale: str, reject_slug: str | None) -> dict:
    import httpx

    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            config.WORKER_URL,
            json={"image": image_b64, "mimeType": mime_type, "locale": locale,
                 "rejectSlug": reject_slug} if reject_slug else
            {"image": image_b64, "mimeType": mime_type, "locale": locale},
            timeout=60,
        )
    except httpx.HTTPError as e:
        return _error_detail(f"worker_{type(e).__name__}", time.perf_counter() - t0)
    seconds = time.perf_counter() - t0

    if resp.status_code == 429:
        return _error_detail("rate_limited", seconds)
    if resp.status_code != 200:
        return _error_detail(f"worker_http_{resp.status_code}", seconds)

    try:
        data = resp.json()
    except json.JSONDecodeError:
        return _error_detail("bad_model_output", seconds)

    if "error" in data:
        return _error_detail(data["error"], seconds)

    result = {
        "isMeme": bool(data.get("isMeme")),
        "filenameSlug": _sanitize_slug(str(data.get("filenameSlug", ""))),
    }
    return _empty_detail(result, seconds, path="worker-proxy")


# --------------------------------------------------------------------------
# BYO-API-key providers — the gateway makes the same call background.js used
# to make directly, so results land in the shared cache + metrics log too.
# --------------------------------------------------------------------------
def run_byo_key(provider: str, image_b64: str, mime_type: str, locale: str,
                api_key: str, reject_slug: str | None) -> dict:
    import httpx

    if not api_key:
        return _error_detail("missing_api_key")

    prompt = _build_legacy_prompt(locale, reject_slug)
    t0 = time.perf_counter()
    try:
        if provider == "google":
            resp = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GOOGLE_MODEL}:generateContent",
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime_type, "data": image_b64}},
                    ]}],
                    "generationConfig": {"response_mime_type": "application/json"},
                },
                timeout=60,
            )
            seconds = time.perf_counter() - t0
            if resp.status_code != 200:
                return _error_detail(f"google_http_{resp.status_code}", seconds)
            data = resp.json()
            text = (
                data.get("candidates", [{}])[0]
                .get("content", {}).get("parts", [{}])[0].get("text")
            )
            parsed = _parse_legacy_json(text)

        elif provider == "claude":
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json", "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": CLAUDE_MODEL, "max_tokens": 300,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                        {"type": "text", "text": prompt},
                    ]}],
                },
                timeout=60,
            )
            seconds = time.perf_counter() - t0
            if resp.status_code != 200:
                return _error_detail(f"claude_http_{resp.status_code}", seconds)
            data = resp.json()
            text = "".join(block.get("text", "") for block in data.get("content", []))
            parsed = _parse_legacy_json(text)

        elif provider in OPENAI_COMPATIBLE:
            cfg = OPENAI_COMPATIBLE[provider]
            resp = httpx.post(
                cfg["endpoint"],
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                json={
                    "model": cfg["model"],
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
                    ]}],
                },
                timeout=60,
            )
            seconds = time.perf_counter() - t0
            if resp.status_code != 200:
                return _error_detail(f"{provider}_http_{resp.status_code}", seconds)
            data = resp.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content")
            parsed = _parse_legacy_json(text)

        else:
            return _error_detail("unknown_provider")

    except httpx.HTTPError as e:
        return _error_detail(f"{provider}_{type(e).__name__}", time.perf_counter() - t0)
    except (KeyError, IndexError, TypeError):
        log.exception("Unexpected %s response shape", provider)
        return _error_detail("bad_model_output", time.perf_counter() - t0)

    if "error" in parsed:
        return _error_detail(parsed["error"], seconds)
    return _empty_detail(parsed, seconds, path=f"{provider}-byo-key")


def run_provider(provider: str, service_pipeline, image_b64: str, mime_type: str,
                 locale: str, mode: str, reject_slug: str | None,
                 api_key: str | None) -> dict:
    """Single dispatch point app.py calls — every provider ends up here so
    the cache/rate-limit/metrics gate in app.py stays provider-agnostic."""
    if provider == "daemon2":
        return run_daemon2(service_pipeline, image_b64, mime_type, locale, mode, reject_slug)
    if provider == "worker":
        return run_worker(image_b64, mime_type, locale, reject_slug)
    if provider in BYO_KEY_PROVIDERS:
        return run_byo_key(provider, image_b64, mime_type, locale, api_key or "", reject_slug)
    return _error_detail("unknown_provider")
