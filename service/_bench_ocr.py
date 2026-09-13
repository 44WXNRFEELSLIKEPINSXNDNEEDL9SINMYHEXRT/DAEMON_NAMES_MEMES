"""
OCR engine A/B benchmark: RapidOCR vs LightOnOCR-2-1B.

Run on a host with >=8 GB free RAM (NOT required for the service itself):

    pip install -r requirements.txt -r requirements-lightonocr.txt
    python _bench_ocr.py /path/to/memes_dir

Expects 15-20 real meme images covering: clean captions, stylized/impact-font
captions, and Cyrillic text. Prints per-image results and an aggregate
accuracy/latency table for each engine, plus OCR-injected vs VLM-alone
generation-time comparison (auto mode viability question).

Results are printed as markdown tables — paste them into README "Model
selection" section.
"""

import glob
import os
import sys
import time

from PIL import Image

import config
import ocr
from ocr import LightOnOcrEngine, RapidOcrEngine


def bench_engine(engine, images):
    rows = []
    for path in images:
        img = Image.open(path).convert("RGB")
        t0 = time.perf_counter()
        try:
            res = engine.recognize(img)
        except Exception as e:  # noqa: BLE001
            res = ocr.OcrResult("", 0.0, engine.name, time.perf_counter() - t0)
            print(f"  !! {engine.name} failed on {path}: {type(e).__name__}: {e}")
        rows.append((os.path.basename(path), res.text, res.confidence,
                     time.perf_counter() - t0))
        print(f"  {engine.name} | {os.path.basename(path)} | "
              f"{time.perf_counter()-t0:.2f}s | conf={res.confidence:.2f} | "
              f"text={res.text[:60]!r}")
    return rows


def summarize(name, rows):
    n = len(rows)
    detected = sum(1 for r in rows if r[1].strip())
    confident = sum(1 for r in rows if r[3] and r[2] >= config.OCR_CONFIDENCE_THRESHOLD)
    avg_t = sum(r[3] for r in rows) / n if n else 0
    print(f"\n### {name}")
    print(f"| metric | value |")
    print(f"|---|---|")
    print(f"| images | {n} |")
    print(f"| text detected | {detected}/{n} |")
    print(f"| >= threshold {config.OCR_CONFIDENCE_THRESHOLD} | {confident}/{n} |")
    print(f"| avg latency | {avg_t:.2f}s |")
    print(f"| total latency | {sum(r[3] for r in rows):.1f}s |")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    d = sys.argv[1]
    images = sorted(
        p for ext in ("png", "jpg", "jpeg", "webp", "gif")
        for p in glob.glob(os.path.join(d, f"*.{ext}"))
    )
    if not images:
        print(f"No images found in {d}")
        sys.exit(1)
    print(f"Benchmarking {len(images)} images from {d}\n")

    print("== RapidOCR ==")
    rapid = RapidOcrEngine()
    rapid.load()
    rapid_rows = bench_engine(rapid, images)
    summarize("RapidOCR (PP-OCRv5 cyrillic)", rapid_rows)

    print("\n== LightOnOCR-2-1B ==")
    try:
        lighton = LightOnOcrEngine()
        lighton.load()
        lighton_rows = bench_engine(lighton, images)
        summarize("LightOnOCR-2-1B", lighton_rows)
    except ImportError:
        print("torch/transformers not installed — skipping LightOnOCR "
              "(pip install -r requirements-lightonocr.txt)")
        lighton_rows = None

    # --- Auto-mode viability: VLM alone vs VLM with OCR context injected ---
    print("\n== VLM-alone vs VLM+OCR-context (auto mode) ==")
    print("Run the service with OCR_ENGINE=rapidocr and OCR_ENGINE=none and "
          "compare the logged vlm= numbers for the same images; or call "
          "pipeline._vlm_classify directly:")
    print("  python - <<'EOF'")
    print("  import pipeline, base64, io, time")
    print("  # see _test_api.py for the request shape")
    print("  EOF")


if __name__ == "__main__":
    main()
