"""
Daemon Names Memes — image classification service.

FastAPI is the entrypoint: a plain HTTP JSON API, platform-agnostic
(any Docker-capable host; all host config via env vars, see config.py).

  POST /classify  {"image": "<base64>", "mimeType": "image/png",
                   "locale": "ru", "mode": "manual"|"auto",
                   "rejectSlug": "<optional, correction flow>"}
  ->              {"isMeme": bool, "filenameSlug": "..."}

The Gradio UI is OPTIONAL (ENABLE_GRADIO_UI=1) — a debug front for the same
pipeline, never the primary interface.
"""

import json
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
import ocr
import pipeline
import ratelimit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meme-classifier")

app = FastAPI(title="Daemon Names Memes classifier", docs_url="/docs")

# CORS: the caller is a chrome-extension:// origin — wildcard on EVERY path,
# including errors (the exception handler below preserves it via JSONResponse
# going back through the same middleware stack).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _error(err_type: str) -> dict:
    resp = dict(config.FALLBACK_RESPONSE)
    resp["error"] = err_type
    return resp


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Even a 500 keeps the JSON error contract + CORS headers."""
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content=_error("internal_error"))


@app.get("/health")
async def health():
    vlm = pipeline.vlm_status()
    return {
        "vlm": vlm,
        "ocr": ocr.status(),
        "mode_default": "auto",
        "rate_limit": {
            "enabled": config.RATE_LIMIT_ENABLED,
            "per_minute": config.RATE_LIMIT_PER_MINUTE,
            "window_s": config.RATE_LIMIT_WINDOW_S,
        },
    }


@app.post("/classify")
async def classify_endpoint(request: Request):
    allowed, remaining, retry_after = ratelimit.check_and_record(request)
    if not allowed:
        log.warning("Rate limit hit for %s", ratelimit.client_key(request))
        resp = JSONResponse(status_code=429, content=_error("rate_limited"))
        resp.headers["Retry-After"] = str(int(retry_after) + 1)
        return resp

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > config.MAX_REQUEST_BYTES:
        return JSONResponse(status_code=413, content=_error("payload_too_large"))

    body = await request.body()
    if len(body) > config.MAX_REQUEST_BYTES:
        return JSONResponse(status_code=413, content=_error("payload_too_large"))

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content=_error("invalid_json_body"))

    image = payload.get("image") or payload.get("imageBase64") or ""
    mime_type = payload.get("mimeType") or "image/png"
    locale = payload.get("locale") or ""
    mode = payload.get("mode") or "auto"
    # Optional "Rename last" negative example (sent by the server/ gateway).
    reject_slug = payload.get("rejectSlug") or None
    if not all(isinstance(v, str) for v in (image, mime_type, locale, mode)) \
            or not isinstance(reject_slug, (str, type(None))):
        return JSONResponse(status_code=400, content=_error("invalid_field_types"))
    if reject_slug:
        reject_slug = reject_slug.strip()[:120] or None

    # Blocking model work runs off the event loop so /health stays responsive.
    import anyio
    detail = await anyio.to_thread.run_sync(
        lambda: pipeline.classify_detailed(image, mime_type, locale, mode, reject_slug=reject_slug)
    )
    resp = JSONResponse(content=detail["result"])
    if remaining >= 0:
        resp.headers["X-RateLimit-Remaining"] = str(remaining)
    # Per-stage timings for the gateway's metrics log; the body contract is
    # unchanged for direct callers.
    resp.headers["X-Pipeline-Path"] = str(detail.get("path", "unknown"))
    resp.headers["X-OCR-Seconds"] = f"{detail.get('ocr_seconds', 0.0):.3f}"
    resp.headers["X-VLM-Seconds"] = f"{detail.get('vlm_seconds', 0.0):.3f}"
    resp.headers["X-OCR-Used"] = "1" if detail.get("ocr_used") else "0"
    resp.headers["X-VLM-Ran"] = "1" if detail.get("vlm_ran") else "0"
    return resp


# --------------------------------------------------------------------------
# Optional Gradio debug UI (ENABLE_GRADIO_UI=1). Not the primary interface.
# --------------------------------------------------------------------------
if config.ENABLE_GRADIO_UI:
    try:
        import gradio as gr

        def gradio_classify(image_b64: str, mime_type: str, locale: str, mode: str) -> str:
            return json.dumps(
                pipeline.classify(image_b64, mime_type, locale, mode),
                ensure_ascii=False, indent=2,
            )

        with gr.Blocks(title="Daemon Names Memes classifier") as demo:
            gr.Markdown(
                "## Daemon Names Memes — classifier API (debug UI)\n"
                "Primary endpoint: `POST /classify`. This UI calls the same pipeline."
            )
            with gr.Row():
                with gr.Column():
                    in_b64 = gr.Textbox(label="image (base64)", lines=6)
                    in_mime = gr.Textbox(label="mimeType", value="image/png")
                    in_locale = gr.Textbox(label="locale (optional)", value="")
                    in_mode = gr.Dropdown(label="mode", choices=["auto", "manual"], value="auto")
                    btn = gr.Button("Classify")
                out_json = gr.JSON(label="response")
            btn.click(gradio_classify, inputs=[in_b64, in_mime, in_locale, in_mode],
                      outputs=out_json, api_name="classify")

        app = gr.mount_gradio_app(app, demo, path="/")
        log.info("Gradio debug UI mounted at / (ENABLE_GRADIO_UI=1)")
    except ImportError:
        log.warning("ENABLE_GRADIO_UI=1 but gradio is not installed; API only")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
