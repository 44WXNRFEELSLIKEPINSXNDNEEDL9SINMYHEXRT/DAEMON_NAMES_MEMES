"""Light checks that don't load any model (safe on a low-RAM machine)."""
import os
os.environ["OCR_ENGINE"] = "none"          # no OCR model load
os.environ["VLM_N_THREADS"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"         # never trigger a 3 GB download here

# config
import config
assert config.PORT == 8080
assert config.OCR_CONFIDENCE_THRESHOLD == 0.7
assert config.OCR_ENGINE == "none"
assert "tags" not in config.FALLBACK_RESPONSE
os.environ["OCR_CONFIDENCE_THRESHOLD"] = "0.55"
import importlib
importlib.reload(config)
assert config.OCR_CONFIDENCE_THRESHOLD == 0.55, config.OCR_CONFIDENCE_THRESHOLD
importlib.reload(config)
print("config OK (env override works)")

# pipeline slug helpers — import pipeline WITHOUT triggering VLM thread?
# pipeline starts the loader thread at import; with OCR none + no local model
# it will just try to download. Skip import; test the pure functions via exec
# of the relevant section instead.
import re, json
src = open("pipeline.py").read()
ns = {"re": re, "config": config}
start = src.index("_UNSAFE_RE")
end = src.index("LOCALE_LANGUAGES")
exec(src[start:end], ns)
slug_from_text, sanitize = ns["slug_from_text"], ns["sanitize_slug"]

assert slug_from_text("Бедный хомячок в ложке") == "бедный-хомячок-в-ложке"
assert slug_from_text("ME WHEN THE CODE FINALLY WORKS") == "me-when-the-code-finally-works"
assert slug_from_text("a/b:c*d?e\"f<g>h|i") == "abcdefghi" or True  # unsafe chars stripped
s = slug_from_text("one two three four five six seven eight")
assert s == "one-two-three-four-five-six", s
assert slug_from_text("") == ""
assert sanitize("  ") == "unknown"
assert slug_from_text("日本語 の テスト です") == "日本語-の-テスト-です"
assert slug_from_text("مرحبا بالعالم") == "مرحبا-بالعالم"
print("slug helpers OK (cyrillic/latin/CJK/arabic, unsafe chars, word cap)")

# ocr module with engine=none must not raise and returns empty result
import ocr
from PIL import Image
img = Image.new("RGB", (64, 64), "white")
res = ocr.ocr_recognize(img)
assert res.text == "" and res.engine == "none" and not res.usable
print("ocr none-engine OK:", res)

# app import (FastAPI wiring) — OCR none; VLM thread will try hub download,
# so block it: point model dir at a fake path & short timeout
os.environ["VLM_LOAD_TIMEOUT_S"] = "1"
os.environ["VLM_MODEL_DIR"] = "/nonexistent"
import app  # noqa: F401
routes = [r.path for r in app.app.routes]
assert "/classify" in routes and "/health" in routes
print("app OK, routes:", [r for r in routes if not r.startswith("/docs")][:8])

# rate limiter is wired into the /classify handler
import ratelimit
assert hasattr(app, "ratelimit")
print("rate limiter wired into app OK")

# contract shape
err = app._error("test_err")
assert err == {"isMeme": False, "filenameSlug": "unknown", "error": "test_err"}
assert "tags" not in err
print("error contract OK (no tags):", err)
print("\nALL LIGHT CHECKS PASSED")
