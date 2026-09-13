"""
Two-stage pipeline: OCR pre-pass + Qwen3-VL-4B VLM.

Modes (request field "mode"):
  manual — user explicitly chose "save as meme"; isMeme is assumed True.
           OCR runs first; if confidence >= OCR_CONFIDENCE_THRESHOLD the slug
           is built directly from the recognized text (native script preserved,
           never transliterated) and the VLM is skipped entirely. Otherwise
           the VLM generates a description-based slug only.
  auto   — unattended (downloads-API callback). The VLM always runs because it
           is the only stage that decides isMeme. OCR runs first as a cheap
           pre-pass; high-confidence text is injected into the VLM prompt as
           "detected text" context.

Per-request latency is logged as three separate numbers: ocr=, vlm=, total=.
The core VLM logic here is the already-tested Qwen3-VL GGUF path (llama-cpp,
grammar-constrained JSON, 384px downscale) — unchanged except for dropping
"tags" from the contract and adding the OCR-context injection.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import re
import threading
import time

from PIL import Image

import config
import ocr

log = logging.getLogger("meme-classifier")

# --------------------------------------------------------------------------
# Slug helpers
# --------------------------------------------------------------------------
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WORD_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


def sanitize_slug(slug: str) -> str:
    """Filesystem-safe kebab slug; preserves any script (Cyrillic, Arabic...)."""
    slug = _UNSAFE_RE.sub("", slug or "").strip()
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[: config.SLUG_MAX_CHARS] or "unknown"


def slug_from_text(text: str) -> str:
    """
    Deterministic slug from OCR'd text (manual-mode fast path).
    Keeps the text's own language and native script — NO transliteration.
    Lowercases (only affects caseful scripts), keeps letters/digits, joins
    the first SLUG_MAX_WORDS words with hyphens.
    """
    words = _WORD_RE.findall(text.lower())
    if not words:
        return ""
    return sanitize_slug("-".join(words[: config.SLUG_MAX_WORDS]))


LOCALE_LANGUAGES = {
    "en": "English", "ru": "Russian", "uk": "Ukrainian", "be": "Belarusian",
    "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "it": "Italian", "pl": "Polish", "cs": "Czech", "tr": "Turkish",
    "ar": "Arabic", "fa": "Persian", "he": "Hebrew", "hi": "Hindi",
    "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
    "ur": "Urdu", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ms": "Malay",
    "nl": "Dutch", "sv": "Swedish", "no": "Norwegian", "da": "Danish",
    "fi": "Finnish", "el": "Greek", "hu": "Hungarian", "ro": "Romanian",
    "bg": "Bulgarian", "sr": "Serbian", "hr": "Croatian", "sk": "Slovak",
    "ka": "Georgian", "hy": "Armenian", "az": "Azerbaijani", "kk": "Kazakh",
    "uz": "Uzbek", "sw": "Swahili",
}


def _locale_language(locale: str | None) -> str:
    if locale:
        code = locale.strip().lower().replace("_", "-").split("-")[0]
        return LOCALE_LANGUAGES.get(code, "English")
    return "English"


def build_vlm_prompt(locale: str | None, ocr_text: str | None = None) -> str:
    lang = _locale_language(locale)
    ocr_context = ""
    if ocr_text and ocr_text.strip():
        # Inject OCR as context so the VLM does less visual text reading.
        snippet = ocr_text.strip()[:500]
        ocr_context = (
            f"\nAn OCR pre-pass detected this text in the image "
            f"(treat it as reliable context, but verify against the image):\n"
            f"---\n{snippet}\n---\n"
        )

    return f"""Analyze the attached image and answer in strict JSON.
{ocr_context}
1. "isMeme": true if the image is a meme — overlaid caption text, a recognizable meme template, a reaction-image format, or clearly satirical/humorous intent. This includes reply-culture formats common on X/TikTok/Reddit: a single symbol, short caption, or stock image used as a reply or punchline. Otherwise false.

2. "filenameSlug": a short description of the image, 3-6 words, lowercase, hyphen-separated (kebab-case), in this exact JSON shape:
{{"isMeme": true, "filenameSlug": "distracted-boyfriend-variant"}}

Rules for "filenameSlug":
- If the image contains visible text, base the slug on that text's meaning and write it in the SAME language and native script as the text (Cyrillic, Arabic, Devanagari, Hangul, etc.). Do NOT transliterate or romanize it into Latin letters.
- If there is no visible text in the image, write the slug in {lang}.
- Must be filesystem-safe: no slashes, colons, quotes, or other special punctuation. Only letters (any script), digits and hyphens.

Respond with JSON only, no prose, no markdown fences."""


# --------------------------------------------------------------------------
# VLM loading (unchanged tested path: llama-cpp GGUF, background thread)
# --------------------------------------------------------------------------
_model = None
_model_lock = threading.Lock()   # llama.cpp instances are not thread-safe
_model_ready = threading.Event()
_model_error: str | None = None

RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "isMeme": {"type": "boolean"},
        "filenameSlug": {"type": "string"},
    },
    "required": ["isMeme", "filenameSlug"],
    "additionalProperties": False,
}


def _resolve_model_paths() -> tuple[str, str]:
    local_model = f"{config.LOCAL_MODEL_DIR}/{config.VLM_MODEL_FILE}"
    local_mmproj = f"{config.LOCAL_MODEL_DIR}/{config.VLM_MMPROJ_FILE}"
    import os
    if os.path.exists(local_model) and os.path.exists(local_mmproj):
        log.info("Using local model files in %s", config.LOCAL_MODEL_DIR)
        return local_model, local_mmproj
    from huggingface_hub import hf_hub_download
    log.info("Downloading %s from the Hub...", config.VLM_MODEL_REPO)
    return (
        hf_hub_download(config.VLM_MODEL_REPO, config.VLM_MODEL_FILE),
        hf_hub_download(config.VLM_MODEL_REPO, config.VLM_MMPROJ_FILE),
    )


def _load_model() -> None:
    global _model, _model_error
    try:
        t0 = time.perf_counter()
        model_path, mmproj_path = _resolve_model_paths()
        from llama_cpp import Llama
        from llama_cpp.llama_chat_format import Qwen25VLChatHandler
        # llama-cpp-python 0.3.35 exposes the Qwen2.5-VL handler; Qwen3-VL uses
        # the same chat structure (image tokens + system/user turns) — this is
        # the supported, tested path for this model on CPU.
        handler = Qwen25VLChatHandler(clip_model_path=mmproj_path, verbose=False)
        _model = Llama(
            model_path=model_path,
            chat_handler=handler,
            n_ctx=config.VLM_N_CTX,
            n_gpu_layers=0,        # CPU only
            n_threads=config.VLM_N_THREADS,
            verbose=False,
        )
        log.info("VLM loaded in %.1fs", time.perf_counter() - t0)
        _model_ready.set()
    except Exception as e:  # noqa: BLE001
        _model_error = f"{type(e).__name__}: {e}"
        log.exception("Failed to load VLM")
        _model_ready.set()


threading.Thread(target=_load_model, daemon=True).start()


# --------------------------------------------------------------------------
# VLM call
# --------------------------------------------------------------------------
def _error_response(err_type: str) -> dict:
    resp = dict(config.FALLBACK_RESPONSE)
    resp["error"] = err_type
    return resp


def _parse_model_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    is_meme = obj.get("isMeme")
    slug = obj.get("filenameSlug")
    if not isinstance(is_meme, bool) or not isinstance(slug, str):
        return None
    return {"isMeme": is_meme, "filenameSlug": sanitize_slug(slug)}


def _wait_for_vlm() -> str | None:
    """Returns an error type string if the VLM is not usable, else None."""
    if not _model_ready.wait(timeout=config.VLM_LOAD_TIMEOUT_S):
        return "model_loading_timeout"
    if _model_error:
        return "model_load_error"
    if _model is None:
        return "model_unavailable"
    return None


def _vlm_classify(data_uri: str, locale: str | None, ocr_text: str | None) -> dict | None:
    messages = [
        {"role": "system", "content": "You are a strict JSON image classifier. You respond with a single JSON object and nothing else."},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": build_vlm_prompt(locale, ocr_text)},
            ],
        },
    ]
    try:
        with _model_lock:
            completion = _model.create_chat_completion(
                messages=messages,
                max_tokens=config.VLM_MAX_NEW_TOKENS,
                temperature=0.0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "meme_classification",
                        "schema": RESPONSE_JSON_SCHEMA,
                        "strict": True,
                    },
                },
            )
        text = completion["choices"][0]["message"]["content"] or ""
    except Exception as e:  # noqa: BLE001 — OOM, llama errors
        log.exception("VLM generation failed")
        raise type(e)
    parsed = _parse_model_json(text)
    if parsed is None:
        log.warning("Unparseable VLM output: %r", text[:300])
    return parsed


# --------------------------------------------------------------------------
# Image preprocessing
# --------------------------------------------------------------------------
def _preprocess(image_b64: str) -> tuple[Image.Image | None, str | None, str | None]:
    """
    Returns (pil_image, vlm_data_uri, error_type).
    pil_image is at OCR resolution (caller downscales per stage);
    vlm_data_uri is the 384px JPEG data URI for the VLM.
    """
    try:
        if "," in image_b64[:64] and image_b64.strip().lower().startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]
        raw = base64.b64decode(image_b64, validate=True)
        img = Image.open(io.BytesIO(raw))
        img.load()  # force full decode -> catches truncated/corrupt images
        img = img.convert("RGB")
        vlm_img = img.copy()
        if max(vlm_img.size) > config.VLM_MAX_IMAGE_SIDE:
            vlm_img.thumbnail((config.VLM_MAX_IMAGE_SIDE, config.VLM_MAX_IMAGE_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        vlm_img.save(buf, format="JPEG", quality=85)
        data_uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        return img, data_uri, None
    except (binascii.Error, ValueError):
        return None, None, "invalid_base64"
    except Exception:  # noqa: BLE001 — malformed/unsupported image
        log.exception("Image preprocessing failed")
        return None, None, "invalid_image"


# --------------------------------------------------------------------------
# Pipeline entry
# --------------------------------------------------------------------------
def classify(image_b64: str, mime_type: str = "image/png", locale: str = "",
             mode: str = "auto") -> dict:
    """
    Full two-stage pipeline. Never raises; returns the JSON error contract.
    Latency is logged in three separate numbers: ocr=, vlm=, total=.
    """
    t_start = time.perf_counter()
    mode = (mode or "auto").strip().lower()
    if mode not in ("manual", "auto"):
        return _error_response("invalid_mode")

    if not image_b64 or not isinstance(image_b64, str):
        return _error_response("empty_image")
    if len(image_b64) > config.MAX_B64_CHARS:
        log.warning("Rejected oversized base64 payload: %d chars", len(image_b64))
        return _error_response("payload_too_large")

    img, data_uri, err = _preprocess(image_b64)
    if err:
        return _error_response(err)

    # ---- Stage 1: OCR pre-pass (both modes) ----
    ocr_res = ocr.ocr_recognize(img)  # type: ignore[arg-type]  # img is not None here
    ocr_seconds = ocr_res.seconds

    # ---- manual mode: OCR fast path ----
    if mode == "manual":
        if ocr_res.usable:
            slug = slug_from_text(ocr_res.text)
            if slug and slug != "unknown":
                total = time.perf_counter() - t_start
                log.info(
                    "classify ok | mode=manual path=ocr-fast | ocr=%.2fs vlm=0.00s total=%.2fs "
                    "| isMeme=True slug=%r conf=%.2f",
                    ocr_seconds, total, slug, ocr_res.confidence,
                )
                # User chose "save as meme" -> isMeme is True by definition.
                return {"isMeme": True, "filenameSlug": slug}
        # Low-confidence / no OCR text -> VLM for the slug only; isMeme=True.
        vlm_err = _wait_for_vlm()
        if vlm_err:
            # VLM down in manual mode: user intent still says meme. Best-effort
            # slug from any OCR text, else the fallback contract.
            slug = slug_from_text(ocr_res.text) if ocr_res.text else ""
            if slug and slug != "unknown":
                return {"isMeme": True, "filenameSlug": slug}
            return _error_response(vlm_err)
        t_vlm0 = time.perf_counter()
        try:
            parsed = _vlm_classify(data_uri, locale or None,
                                   ocr_res.text if ocr_res.text.strip() else None)
        except Exception as e:  # noqa: BLE001
            return _error_response(type(e).__name__)
        t_vlm = time.perf_counter() - t_vlm0
        if parsed is None:
            return _error_response("bad_model_output")
        result = {"isMeme": True, "filenameSlug": parsed["filenameSlug"]}
        total = time.perf_counter() - t_start
        log.info(
            "classify ok | mode=manual path=vlm-slug | ocr=%.2fs vlm=%.2fs total=%.2fs "
            "| isMeme=True slug=%r",
            ocr_seconds, t_vlm, total, result["filenameSlug"],
        )
        return result

    # ---- auto mode: VLM always runs (it decides isMeme) ----
    vlm_err = _wait_for_vlm()
    if vlm_err:
        return _error_response(vlm_err)
    t_vlm0 = time.perf_counter()
    try:
        parsed = _vlm_classify(data_uri, locale or None,
                               ocr_res.text if ocr_res.usable else None)
    except Exception as e:  # noqa: BLE001
        return _error_response(type(e).__name__)
    t_vlm = time.perf_counter() - t_vlm0
    if parsed is None:
        return _error_response("bad_model_output")
    total = time.perf_counter() - t_start
    log.info(
        "classify ok | mode=auto ocr_injected=%s | ocr=%.2fs vlm=%.2fs total=%.2fs "
        "| isMeme=%s slug=%r",
        bool(ocr_res.usable), ocr_seconds, t_vlm, total,
        parsed["isMeme"], parsed["filenameSlug"],
    )
    return parsed


def vlm_status() -> dict:
    return {
        "status": "ready" if _model is not None else ("error" if _model_error else "loading"),
        "model": f"{config.VLM_MODEL_REPO} {config.VLM_MODEL_FILE}",
        "error": _model_error,
    }
