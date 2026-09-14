"""
End-to-end test of server/app.py wiring: cache gate, rate limiting, metrics,
and the /correct endpoint — using fakeredis and a stubbed pipeline (no real
VLM/OCR load; this tests the GATEWAY logic, not classification quality,
which is already covered by service/'s own tests).
"""
import sys, os, tempfile, json, base64, io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Prevent the imported service/pipeline.py from loading the real VLM/OCR
# stack — this test exercises the GATEWAY logic only (cache/ratelimit/
# metrics/correction wiring), not classification quality.
os.environ["OCR_ENGINE"] = "none"
os.environ["VLM_MODEL_DIR"] = "/nonexistent"
os.environ["VLM_LOAD_TIMEOUT_S"] = "1"

from PIL import Image

tmp = tempfile.mkdtemp()
os.environ["METRICS_DB_PATH"] = os.path.join(tmp, "e2e.sqlite3")
os.environ["RATE_LIMIT_WINDOW_S"] = "60"
os.environ["RATE_LIMIT_DAEMON2_PER_WINDOW"] = "3"
os.environ["RATE_LIMIT_DEFAULT_PER_WINDOW"] = "3"
os.environ["CACHE_ENABLED"] = "1"
os.environ["PHASH_HAMMING_THRESHOLD"] = "8"

import config
importlib_reload_needed = True
import importlib
importlib.reload(config)

import fakeredis
import app as gw

# Patch in fakeredis instead of a real Redis connection.
fake_r = fakeredis.FakeStrictRedis(decode_responses=True)
gw._redis = fake_r
gw._phash_cache = None
gw._rate_limiter = None

# Stub the pipeline so this test doesn't need the VLM/OCR stack loaded.
call_log = []
def fake_classify_detailed(image_b64, mime_type, locale, mode, reject_slug=None):
    call_log.append({"mode": mode, "reject_slug": reject_slug})
    if reject_slug:
        slug = "corrected-slug"
    else:
        slug = "original-slug"
    return {
        "result": {"isMeme": True, "filenameSlug": slug},
        "ocr_seconds": 0.1, "vlm_seconds": 1.0, "total_seconds": 1.1,
        "ocr_text": "", "ocr_confidence": 0.0, "ocr_used": False,
        "vlm_ran": True, "path": "vlm",
    }

gw.service_pipeline = type("FakePipeline", (), {"classify_detailed": staticmethod(fake_classify_detailed)})

from fastapi.testclient import TestClient
client = TestClient(gw.app)


def make_b64(seed):
    import random
    rng = random.Random(seed)
    img = Image.new("RGB", (100, 100))
    pixels = [
        (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        for _ in range(100 * 100)
    ]
    img.putdata(pixels)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# --- 1. health check ---
r = client.get("/health")
assert r.status_code == 200, r.text
h = r.json()
assert h["redis"]["ok"] is True
assert h["cache"]["enabled"] is True
print("health OK:", h["cache"], h["rate_limit"])

# --- 2. first classify: cache miss, runs pipeline ---
img1 = make_b64(1)
r = client.post("/classify", json={"image": img1, "mode": "auto"})
assert r.status_code == 200, r.text
body = r.json()
assert body == {"isMeme": True, "filenameSlug": "original-slug"}, body
assert r.headers.get("x-cache") == "MISS"
print("first classify: MISS OK, headers:", dict(r.headers))

# --- 3. same exact image again: cache HIT, pipeline NOT called again ---
calls_before = len(call_log)
r2 = client.post("/classify", json={"image": img1, "mode": "auto"})
assert r2.status_code == 200
assert r2.json() == {"isMeme": True, "filenameSlug": "original-slug"}
assert r2.headers.get("x-cache") == "HIT"
assert len(call_log) == calls_before, "pipeline should NOT have been called on a cache hit"
print("second identical request: HIT OK, pipeline not re-invoked")

# --- 4. rate limiting: 3rd request from same client should now be blocked ---
# (2 classify calls above + this one = 3rd -> still allowed; 4th blocked)
r3 = client.post("/classify", json={"image": make_b64(2), "mode": "auto"})
assert r3.status_code == 200, r3.text
r4 = client.post("/classify", json={"image": make_b64(3), "mode": "auto"})
assert r4.status_code == 429, r4.text
body4 = r4.json()
assert body4["error"] == "rate_limited"
assert "retry_after_seconds" in body4
print("rate limiting: 4th request blocked with explicit contract:", body4)

# --- 5. BYO-key provider bypasses rate limiting entirely ---
# Stub run_byo_key so no real HTTP calls to Google happen; this also lets us
# verify BYO-key results populate the SHARED cache and metrics log.
import providers as providers_mod
byo_calls = []
def fake_run_byo_key(provider, image_b64, mime_type, locale, api_key, reject_slug):
    byo_calls.append({"provider": provider, "api_key": api_key, "reject_slug": reject_slug})
    return {
        "result": {"isMeme": True, "filenameSlug": f"byo-{provider}-slug"},
        "ocr_seconds": 0.0, "vlm_seconds": 0.2, "total_seconds": 0.2,
        "ocr_used": False, "vlm_ran": True, "path": f"{provider}-byo-key-stub",
    }
_orig_run_byo_key = providers_mod.run_byo_key
providers_mod.run_byo_key = fake_run_byo_key

for i in range(10):
    r5 = client.post("/classify", json={
        "image": make_b64(100 + i), "mode": "auto", "provider": "google",
        "apiKey": "sk-fake-user-key",
    })
    # Must never be 429: BYO-key providers are exempt from the gateway's
    # rate limiter (the user's own key = the user's own cost).
    assert r5.status_code == 200, (i, r5.text)
    assert r5.json()["filenameSlug"] == "byo-google-slug"
assert len(byo_calls) == 10 and all(c["api_key"] == "sk-fake-user-key" for c in byo_calls)
print("BYO-key provider bypasses rate limit + apiKey forwarded OK (10/10)")

# BYO results must land in the SHARED cache: re-send image #100 -> cache HIT
r5hit = client.post("/classify", json={
    "image": make_b64(100), "mode": "auto", "provider": "google",
    "apiKey": "sk-fake-user-key",
})
assert r5hit.headers.get("x-cache") == "HIT", r5hit.headers
assert len(byo_calls) == 10, "provider must NOT be called on a cache hit"
print("BYO-key result served from shared cache on repeat OK")

# ...and a DIFFERENT provider requesting the same image also hits the shared
# cache (that's the whole point of 'shares database'): provider-agnostic keys.
r5cross = client.post("/classify", json={
    "image": make_b64(100), "mode": "auto", "provider": "claude",
    "apiKey": "sk-fake-claude-key",
})
assert r5cross.headers.get("x-cache") == "HIT", r5cross.headers
assert r5cross.json()["filenameSlug"] == "byo-google-slug"
print("cross-provider shared cache OK (claude request hit google-cached result)")

providers_mod.run_byo_key = _orig_run_byo_key

# --- 5b. unknown provider rejected cleanly ---
r5b = client.post("/classify", json={"image": make_b64(42), "provider": "skynet"})
assert r5b.status_code == 400 and r5b.json()["error"] == "unknown_provider", r5b.text
print("unknown provider rejected OK")

# --- 6. correction flow: scenario A (cache hit) ---
call_log.clear()
# distinct XFF identity: the rate-limit phase above exhausted 'testclient's
# 3-request quota; RATE_LIMIT_TRUST_PROXY=1 (default) means XFF gives us a
# fresh bucket — which also verifies proxy-header keying end-to-end.
CORR_HEADERS = {"x-forwarded-for": "10.20.30.40"}
r6 = client.post("/correct", json={
    "image": img1, "mode": "auto", "phash": "", "cache_hit": True,
    "previous_slug": "original-slug",
}, headers=CORR_HEADERS)
assert r6.status_code == 200, r6.text
body6 = r6.json()
assert body6["filenameSlug"] == "corrected-slug", body6
assert len(call_log) == 1 and call_log[0]["reject_slug"] == "original-slug"
print("correction scenario A (cache hit): fresh VLM call with reject_slug OK:", body6)

# verify the cache entry for img1's phash was actually updated
import cache as cache_mod
phash1 = cache_mod.compute_phash(Image.open(io.BytesIO(base64.b64decode(img1))))
lookup = gw._get_cache().lookup(phash1)
assert lookup.hit and lookup.result["filenameSlug"] == "corrected-slug", lookup
print("cache entry overwritten after scenario-A correction OK:", lookup.result)

# --- 7. correction flow: scenario B (was fresh call, not cache) ---
call_log.clear()
img2 = make_b64(777)
r7 = client.post("/correct", json={
    "image": img2, "mode": "auto", "phash": "", "cache_hit": False,
    "previous_slug": "some-other-wrong-slug",
}, headers=CORR_HEADERS)
assert r7.status_code == 200, r7.text
assert call_log[0]["reject_slug"] == "some-other-wrong-slug"
print("correction scenario B (fresh call): reject_slug injected OK")

# img2 must NOT have been cached by the scenario-B correction path
phash2 = cache_mod.compute_phash(Image.open(io.BytesIO(base64.b64decode(img2))))
lookup2 = gw._get_cache().lookup(phash2)
assert not lookup2.hit, "scenario B must not populate the cache"
print("cache NOT touched after scenario-B correction OK")

# --- 8. metrics were actually written for all of the above ---
import sqlite3
conn = sqlite3.connect(os.environ["METRICS_DB_PATH"])
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM classification_log").fetchall()
assert len(rows) >= 6, len(rows)
corrections = [r for r in rows if r["was_correction"]]
assert len(corrections) == 2
scenario_a = [r for r in corrections if r["correction_was_cache_hit"] == 1]
scenario_b = [r for r in corrections if r["correction_was_cache_hit"] == 0]
assert len(scenario_a) == 1 and len(scenario_b) == 1
cache_hits_logged = [r for r in rows if r["cache_hit"] == 1]
assert len(cache_hits_logged) >= 1
print(f"metrics log has {len(rows)} rows, {len(corrections)} corrections "
      f"({len(scenario_a)} scenario-A, {len(scenario_b)} scenario-B), "
      f"{len(cache_hits_logged)} cache hits logged")
conn.close()

print("\nALL GATEWAY E2E TESTS PASSED")
