"""
OCR stage — pluggable engines behind one interface.

Engines (selected via OCR_ENGINE env var, see config.py):
  - "rapidocr"   : RapidOCR (PP-OCR models, ONNX Runtime, CPU). Sub-second
                   latency, small memory footprint, real per-line confidence
                   scores, and a dedicated Cyrillic recognition pack
                   (ru/be/uk/bg/sr-Cyrl + English). DEFAULT.
  - "lightonocr" : lightonai/LightOnOCR-2-1B via transformers (CPU float32).
                   End-to-end VLM OCR, very strong on documents, but ~2 GB RAM
                   at inference, multi-second CPU latency, emits no confidence
                   score, and its documented language list has no Russian/
                   Cyrillic. Included for A/B benchmarking; see README.
  - "none"       : OCR stage disabled; every request goes VLM-only.

All engines return OcrResult(text, confidence, engine, seconds) and never
raise — failures collapse to an empty result so the pipeline degrades to
VLM-only instead of erroring.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from PIL import Image

import config

log = logging.getLogger("meme-classifier.ocr")


@dataclass
class OcrResult:
    text: str = ""            # recognized text, lines joined with "\n"
    confidence: float = 0.0   # 0..1; engine-specific (see each backend)
    engine: str = "none"
    seconds: float = 0.0

    @property
    def usable(self) -> bool:
        """High-confidence text worth acting on (named threshold in config)."""
        return bool(self.text.strip()) and self.confidence >= config.OCR_CONFIDENCE_THRESHOLD


class BaseOcrEngine:
    name = "none"

    def load(self) -> None:  # pragma: no cover - trivial
        pass

    def recognize(self, img: Image.Image) -> OcrResult:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# RapidOCR
# ---------------------------------------------------------------------------
class RapidOcrEngine(BaseOcrEngine):
    """PP-OCR via ONNX Runtime. Per-line confidence from the rec model."""

    name = "rapidocr"

    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()

    def load(self) -> None:
        from rapidocr import EngineType, LangRec, ModelType, OCRVersion, RapidOCR

        t0 = time.perf_counter()
        params = {
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Cls.engine_type": EngineType.ONNXRUNTIME,
            "Rec.model_type": ModelType.MOBILE,
        }
        # Language pack + OCR version are env-configurable; a wrong value
        # raises here (caught by the loader) rather than silently misreading.
        try:
            params["Rec.lang_type"] = LangRec(config.OCR_LANG)
        except ValueError:
            log.warning("OCR_LANG=%r not a LangRec value; falling back to 'ch'", config.OCR_LANG)
            params["Rec.lang_type"] = LangRec.CH
        try:
            params["Rec.ocr_version"] = OCRVersion(config.OCR_VERSION)
        except ValueError:
            log.warning("OCR_VERSION=%r invalid; falling back to PP-OCRv5", config.OCR_VERSION)
            params["Rec.ocr_version"] = OCRVersion.PPOCRV5

        self._engine = RapidOCR(params=params)
        log.info("RapidOCR loaded in %.1fs (lang=%s, version=%s)",
                 time.perf_counter() - t0, config.OCR_LANG, config.OCR_VERSION)

    def recognize(self, img: Image.Image) -> OcrResult:
        t0 = time.perf_counter()
        import numpy as np

        if max(img.size) > config.OCR_MAX_IMAGE_SIDE:
            img = img.copy()
            img.thumbnail((config.OCR_MAX_IMAGE_SIDE, config.OCR_MAX_IMAGE_SIDE), Image.LANCZOS)
        arr = np.asarray(img.convert("RGB"))
        with self._lock:
            result = self._engine(arr)
        seconds = time.perf_counter() - t0

        # rapidocr v3 result: .txts (list[str] | None), .scores (list[float] | None)
        txts = getattr(result, "txts", None) or []
        scores = getattr(result, "scores", None) or []
        if not txts:
            return OcrResult("", 0.0, self.name, seconds)
        text = "\n".join(str(t) for t in txts).strip()
        # Mean per-line recognition score as the aggregate confidence.
        conf = float(sum(scores) / len(scores)) if scores else 0.0
        return OcrResult(text, conf, self.name, seconds)


# ---------------------------------------------------------------------------
# LightOnOCR-2-1B
# ---------------------------------------------------------------------------
class LightOnOcrEngine(BaseOcrEngine):
    """
    lightonai/LightOnOCR-2-1B (transformers>=5, CPU float32).

    Confidence caveat: this is an autoregressive VLM — it emits no calibrated
    confidence. Non-empty output is assigned LIGHTONOCR_DEFAULT_CONFIDENCE
    (a named constant, default 0.9); empty output gets 0.0. This is a
    heuristic, not a measurement — flagged in the README model-selection
    section.
    """

    name = "lightonocr"

    def __init__(self) -> None:
        self._model = None
        self._processor = None
        self._lock = threading.Lock()

    def load(self) -> None:
        import torch
        from transformers import LightOnOcrForConditionalGeneration, LightOnOcrProcessor

        t0 = time.perf_counter()
        repo = config.LIGHTONOCR_MODEL_REPO
        log.info("Loading LightOnOCR-2 from %s (CPU float32)...", repo)
        self._processor = LightOnOcrProcessor.from_pretrained(repo)
        self._model = LightOnOcrForConditionalGeneration.from_pretrained(
            repo, torch_dtype=torch.float32
        ).to("cpu")
        self._model.eval()
        log.info("LightOnOCR-2 loaded in %.1fs", time.perf_counter() - t0)

    def recognize(self, img: Image.Image) -> OcrResult:
        import torch

        t0 = time.perf_counter()
        if max(img.size) > config.LIGHTONOCR_IMAGE_SIDE:
            img = img.copy()
            img.thumbnail((config.LIGHTONOCR_IMAGE_SIDE, config.LIGHTONOCR_IMAGE_SIDE), Image.LANCZOS)
        conversation = [{"role": "user", "content": [{"type": "image", "image": img}]}]
        inputs = self._processor.apply_chat_template(
            conversation,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )
        with self._lock, torch.no_grad():
            output_ids = self._model.generate(
                **inputs, max_new_tokens=config.LIGHTONOCR_MAX_NEW_TOKENS
            )
        generated = output_ids[0, inputs["input_ids"].shape[1]:]
        text = self._processor.decode(generated, skip_special_tokens=True).strip()
        seconds = time.perf_counter() - t0

        conf = config.LIGHTONOCR_DEFAULT_CONFIDENCE if text else 0.0
        return OcrResult(text, conf, self.name, seconds)


class NullOcrEngine(BaseOcrEngine):
    name = "none"

    def recognize(self, img: Image.Image) -> OcrResult:
        return OcrResult("", 0.0, self.name, 0.0)


# ---------------------------------------------------------------------------
# Lazy singleton: OCR loads in the background so it never blocks API boot,
# and any load failure degrades to NullOcrEngine (pipeline stays VLM-only).
# ---------------------------------------------------------------------------
_engine: BaseOcrEngine | None = None
_engine_lock = threading.Lock()
_ready = threading.Event()
_error: str | None = None


def _init() -> None:
    global _engine, _error
    choice = config.OCR_ENGINE
    try:
        if choice == "rapidocr":
            eng = RapidOcrEngine()
        elif choice == "lightonocr":
            eng = LightOnOcrEngine()
        else:
            if choice != "none":
                log.warning("Unknown OCR_ENGINE=%r; OCR disabled", choice)
            _engine = NullOcrEngine()
            _ready.set()
            return
        eng.load()
        _engine = eng
    except Exception as e:  # noqa: BLE001 — degrade, never crash the service
        _error = f"{type(e).__name__}: {e}"
        log.exception("OCR engine %r failed to load; falling back to none", choice)
        _engine = NullOcrEngine()
    finally:
        _ready.set()


threading.Thread(target=_init, daemon=True).start()


def ocr_recognize(img: Image.Image) -> OcrResult:
    """
    Run the OCR stage. Never raises; on any failure returns an empty result
    (the pipeline then falls back to VLM-only), and the error is logged.
    """
    if not _ready.wait(timeout=config.VLM_LOAD_TIMEOUT_S):
        log.warning("OCR engine still initializing; skipping OCR stage")
        return OcrResult("", 0.0, "timeout", 0.0)
    assert _engine is not None
    if _engine.name == "none":
        return OcrResult("", 0.0, "none", 0.0)
    t0 = time.perf_counter()
    try:
        res = _engine.recognize(img)
        res.seconds = time.perf_counter() - t0
        log.info("OCR(%s) %.2fs conf=%.2f text=%r",
                 res.engine, res.seconds, res.confidence, res.text[:120])
        return res
    except Exception:  # noqa: BLE001
        log.exception("OCR stage failed; continuing without OCR")
        return OcrResult("", 0.0, "error", time.perf_counter() - t0)


def status() -> dict:
    return {
        "engine": _engine.name if _engine else "initializing",
        "configured": config.OCR_ENGINE,
        "confidence_threshold": config.OCR_CONFIDENCE_THRESHOLD,
        "error": _error,
    }
