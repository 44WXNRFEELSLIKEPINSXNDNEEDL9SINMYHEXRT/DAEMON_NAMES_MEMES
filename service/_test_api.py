"""
End-to-end tests for the classifier service (new contract, no "tags").

Start the server first:  PORT=7861 .venv/bin/python app.py
Then:                    .venv/bin/python _test_api.py

Needs one real image: defaults to ../worker/test.png (override with $1).
"""
import base64, json, sys, time, urllib.request, urllib.error

BASE = "http://localhost:7861"
IMG_PATH = sys.argv[1] if len(sys.argv) > 1 else "../worker/test.png"


def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Origin": "chrome-extension://abcdefgh"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            status, resp, hdrs = r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        status, resp, hdrs = e.code, e.read(), dict(e.headers)
    dt = time.time() - t0
    cors = hdrs.get("access-control-allow-origin")
    print(f"[{status}] {path} in {dt:.1f}s | CORS={cors}")
    try:
        parsed = json.loads(resp)
        print("   ", json.dumps(parsed, ensure_ascii=False)[:300])
        assert "tags" not in parsed, "REGRESSION: 'tags' present in response!"
        return status, parsed, hdrs
    except json.JSONDecodeError:
        print("    RAW:", resp[:200])
        return status, None, hdrs


img = base64.b64encode(open(IMG_PATH, "rb").read()).decode()
print(f"{IMG_PATH} base64 length: {len(img)}")

# manual mode — OCR fast path or VLM slug; isMeme must be true
post("/classify", {"image": img, "mimeType": "image/png", "locale": "ru", "mode": "manual"})
# auto mode — VLM decides isMeme, OCR injected as context when confident
post("/classify", {"image": img, "mimeType": "image/png", "locale": "en", "mode": "auto"})
# default mode = auto
post("/classify", {"image": img, "mimeType": "image/png"})
# invalid mode -> error contract
post("/classify", {"image": img, "mimeType": "image/png", "mode": "turbo"})
# truncated image -> invalid_image
post("/classify", {"image": "data:image/png;base64," + img[:200000], "mode": "manual"})
# invalid base64
post("/classify", {"image": "!!!not-base64!!!", "mode": "auto"})
# corrupt bytes
post("/classify", {"image": base64.b64encode(b"not an image").decode(), "mode": "manual"})
# oversized
post("/classify", {"image": "A" * 9_000_000, "mode": "auto"})
# empty
post("/classify", {"image": "", "mode": "auto"})
# bad field types
post("/classify", {"image": 123, "mode": "auto"})
# health shows both stages
req = urllib.request.Request(BASE + "/health", headers={"Origin": "chrome-extension://x"})
with urllib.request.urlopen(req, timeout=10) as r:
    print(f"[{r.status}] /health | CORS={dict(r.headers).get('access-control-allow-origin')}")
    print("   ", r.read().decode()[:300])
# preflight
req = urllib.request.Request(BASE + "/classify", method="OPTIONS",
                             headers={"Origin": "chrome-extension://abcdefgh",
                                      "Access-Control-Request-Method": "POST",
                                      "Access-Control-Request-Headers": "content-type"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        h = dict(r.headers)
        print(f"[{r.status}] OPTIONS | ACAO={h.get('access-control-allow-origin')}")
except urllib.error.HTTPError as e:
    print(f"[{e.code}] OPTIONS FAILED")
print("\nDone. Remember: 'tags' must not appear anywhere above.")
