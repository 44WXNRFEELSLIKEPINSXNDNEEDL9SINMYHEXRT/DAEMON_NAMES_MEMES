"""
Single configuration layer for the Daemon Names Memes classifier service.

Everything here is overridable via environment variables so the same code and
container image run unchanged on any Docker-capable host (HF Spaces, Render,
Fly.io, Railway, a plain VPS). No platform-specific branches live anywhere
else in the codebase.
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


# --- Server -----------------------------------------------------------------
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8080)             # every PaaS injects $PORT; honor it
ENABLE_GRADIO_UI = _env_bool("ENABLE_GRADIO_UI", False)  # optional debug UI

# --- Request guards -----------------------------------------------------------
MAX_B64_CHARS = _env_int("MAX_B64_CHARS", 8_000_000)        # ~6 MB raw image
MAX_REQUEST_BYTES = _env_int("MAX_REQUEST_BYTES", 12_000_000)

# --- VLM (Qwen3-VL-4B GGUF) ---------------------------------------------------
VLM_MODEL_REPO = os.environ.get("VLM_MODEL_REPO", "Qwen/Qwen3-VL-4B-Instruct-GGUF")
VLM_MODEL_FILE = os.environ.get("VLM_MODEL_FILE", "Qwen3VL-4B-Instruct-Q4_K_M.gguf")
VLM_MMPROJ_FILE = os.environ.get("VLM_MMPROJ_FILE", "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf")
LOCAL_MODEL_DIR = os.environ.get(
    "VLM_MODEL_DIR", os.path.join(os.path.dirname(__file__), "models")
)
# Benchmarked: 384px -> ~29s/request; 1024px -> ~622s with identical output.
VLM_MAX_IMAGE_SIDE = _env_int("VLM_MAX_IMAGE_SIDE", 384)
VLM_MAX_NEW_TOKENS = _env_int("VLM_MAX_NEW_TOKENS", 200)
VLM_N_CTX = _env_int("VLM_N_CTX", 4096)
VLM_N_THREADS = _env_int("VLM_N_THREADS", max(1, (os.cpu_count() or 2) - 1))
VLM_LOAD_TIMEOUT_S = _env_int("VLM_LOAD_TIMEOUT_S", 600)

# --- OCR ----------------------------------------------------------------------
# Engines: "rapidocr" (default; ONNX CPU, sub-second, has a Cyrillic pack),
#          "lightonocr" (LightOnOCR-2-1B via transformers; heavy, no documented
#                        Cyrillic support), "none" (disable the OCR stage).
OCR_ENGINE = os.environ.get("OCR_ENGINE", "rapidocr").strip().lower()

# Named, documented constant — NOT a magic number. OCR results with confidence
# >= this threshold short-circuit the manual-mode pipeline (slug built straight
# from the recognized text) and get injected into the VLM prompt in auto mode.
OCR_CONFIDENCE_THRESHOLD = _env_float("OCR_CONFIDENCE_THRESHOLD", 0.7)

# RapidOCR recognition language pack. "cyrillic" covers ru/be/uk/bg/sr-Cyrl
# + English (PP-OCRv5). Use "ch" for the bundled default (Chinese+English) or
# "latin"/"en"/"korean"/"japan"/"arabic"/"devanagari" per the model list at
# https://rapidai.github.io/RapidOCRDocs/main/model_list/
OCR_LANG = os.environ.get("OCR_LANG", "cyrillic")
# RapidOCR OCR version for the recognition model (PP-OCRv5 / PP-OCRv4).
OCR_VERSION = os.environ.get("OCR_VERSION", "PP-OCRv5")
# Max side (px) for the image handed to the OCR stage. OCR generally wants a
# higher resolution than the VLM; 1024 is a good speed/accuracy compromise.
OCR_MAX_IMAGE_SIDE = _env_int("OCR_MAX_IMAGE_SIDE", 1024)
# LightOnOCR emits no confidence score; this is the constant assigned when it
# returns non-empty text (documented — do not treat as a measured confidence).
LIGHTONOCR_DEFAULT_CONFIDENCE = _env_float("LIGHTONOCR_DEFAULT_CONFIDENCE", 0.9)
LIGHTONOCR_MODEL_REPO = os.environ.get("LIGHTONOCR_MODEL_REPO", "lightonai/LightOnOCR-2-1B")
LIGHTONOCR_MAX_NEW_TOKENS = _env_int("LIGHTONOCR_MAX_NEW_TOKENS", 512)
# Rendered/recommended input size for LightOnOCR (docs: longest dim ~1540px).
LIGHTONOCR_IMAGE_SIDE = _env_int("LIGHTONOCR_IMAGE_SIDE", 1024)

# --- Output contract ----------------------------------------------------------
# The response shape is exactly {"isMeme": bool, "filenameSlug": str}.
# "tags" was removed from the contract — do not re-add it.
FALLBACK_RESPONSE = {"isMeme": False, "filenameSlug": "unknown"}

# Slug length: first N words of recognized text are used when OCR
# short-circuits the VLM in manual mode.
SLUG_MAX_WORDS = _env_int("SLUG_MAX_WORDS", 6)
SLUG_MAX_CHARS = _env_int("SLUG_MAX_CHARS", 80)
