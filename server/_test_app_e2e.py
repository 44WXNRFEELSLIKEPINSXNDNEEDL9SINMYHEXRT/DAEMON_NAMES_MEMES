"""
End-to-end test of server/app.py: cache gate, metrics, provider proxying and
the /correct endpoint — fakeredis + a mocked httpx transport standing in for
service/, the Cloudflare Worker and the BYO-key provider APIs (no Redis, no
models, no network). Exercises the real HTTP provider code paths.
"""
import sys, os, tempfile, json, base64, io, random, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

tmp = tempfile.mkdtemp()
os.environ["METRICS_DB_PATH"] = os.path.join(tmp, "e2e.sqlite3")
os.environ["SERVICE_URL"] = "http://service.test"
os.environ["WORKER_URL"] = "http://worker.test/"
os.environ["CACHE_ENABLED"] = "1"
os.environ["PHASH_HAMMING_THRESHOLD"] = "8"
os.environ["UPSTREAM_READ_TIMEOUT_S"] = "5"

import config, importlib
importlib.reload(config)

import fakeredis
import httpx
from PIL import Image
from fastapi.testclient import TestClient

import app as gw
import cache as cache_mod

calls = []
behaviour = {"worker_429": False, "service_timeout": False}


def upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    host = request.url.host
    calls.append({"host": host, "path": request.url.path, "body": body, "headers": dict(request.headers)})

    if host == "service.test":
        if behaviour["service_timeout"]:
            raise httpx.ReadTimeout("slow VLM", request=request)
        slug = "corrected-slug" if body.get("rejectSlug") else "original-slug"
        is_meme = True if body["mode"] == "manual" else body["image"] != IMG_NOT_MEME
        return httpx.Response(200, json={"isMeme": is_meme, "filenameSlug": slug}, headers={
            "X-Pipeline-Path": "vlm", "X-OCR-Seconds": "0.250", "X-VLM-Seconds": "20.500",
            "X-OCR-Used": "1", "X-VLM-Ran": "1",
        })
    if host == "worker.test":
        if behaviour["worker_429"]:
            return httpx.Response(429, json={"error": "rate_limited"})
        slug = "worker-corrected" if body.get("rejectSlug") else "worker slug/with:unsafe"
        return httpx.Response(200, json={"isMeme": True, "filenameSlug": slug, "tags": ["x"]})
    if host == "generativelanguage.googleapis.com":
        assert request.headers["x-goog-api-key"] == "g-key"
        assert config.GOOGLE_MODEL in request.url.path
        text = json.dumps({"isMeme": True, "filenameSlug": "byo-google-slug"})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})
    if host == "api.anthropic.com":
        assert request.headers["x-api-key"] == "c-key" and body["model"] == config.CLAUDE_MODEL
        return httpx.Response(200, json={"content": [{"type": "text", "text": "```json\n{\"isMeme\": false, \"filenameSlug\": \"cat photo\"}\n```"}]})
    if host == "api.groq.com":
        assert request.headers["authorization"] == "Bearer q-key" and body["model"] == config.GROQ_MODEL
        return httpx.Response(401, json={"error": {"message": "bad key"}})
    raise AssertionError(f"unexpected upstream call {request.url}")


def make_b64(seed):
    rng = random.Random(seed)
    img = Image.new("RGB", (64, 64))
    img.putdata([(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)) for _ in range(64 * 64)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def phash_of(b64):
    return cache_mod.compute_phash(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"))


def upstream_calls(host):
    return [c for c in calls if c["host"] == host]


IMG_NOT_MEME = make_b64(5)
fake_r = fakeredis.FakeAsyncRedis(decode_responses=True)
http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
application = gw.create_app(redis_client=fake_r, http_client=http_client)

with TestClient(application) as client:
    # --- 1. health ---
    r = client.get("/health")
    assert r.status_code == 200, r.text
    h = r.json()
    assert h["status"] == "ok" and h["redis"]["ok"] and h["metrics"]["enabled"], h
    assert h["providers"]["daemon2"] and h["providers"]["worker"], h
    assert "metrics_db" not in h and "rate_limit" not in h
    assert client.get("/docs").status_code == 404
    print("health OK, docs hidden by default:", h)

    # --- 2. daemon2 miss -> proxied to service/ over HTTP ---
    img1 = make_b64(1)
    r = client.post("/classify", json={"image": img1, "mode": "auto", "provider": "daemon2",
                                       "locale": "ru"}, headers={"X-Forwarded-For": "6.6.6.6"})
    assert r.status_code == 200 and r.json() == {"isMeme": True, "filenameSlug": "original-slug"}, r.text
    assert r.headers["x-cache"] == "MISS" and r.headers["x-phash"] == phash_of(img1)
    svc = upstream_calls("service.test")[-1]
    assert svc["path"] == "/classify" and svc["body"]["mode"] == "auto" and "rejectSlug" not in svc["body"]
    # client-supplied XFF must NOT be trusted (uvicorn proxy headers decide
    # who may set it); the peer address is what gets forwarded.
    assert svc["headers"]["x-forwarded-for"] == "testclient", svc["headers"]
    print("daemon2 miss proxied to SERVICE_URL, spoofed XFF ignored OK")

    # --- 3. identical image: cache HIT, upstream not called ---
    n = len(calls)
    r = client.post("/classify", json={"image": img1, "mode": "auto", "provider": "daemon2"})
    assert r.json() == {"isMeme": True, "filenameSlug": "original-slug"} and r.headers["x-cache"] == "HIT"
    assert len(calls) == n
    print("identical image HIT, upstream skipped OK")

    # --- 4. no gateway rate limiting: many cached requests all succeed ---
    for _ in range(40):
        assert client.post("/classify", json={"image": img1, "provider": "daemon2"}).status_code == 200
    print("no gateway-side rate limit (40 requests) OK")

    # --- 5. daemon2 manual results (assumed isMeme) never become auto verdicts ---
    r = client.post("/classify", json={"image": IMG_NOT_MEME, "mode": "manual", "provider": "daemon2"})
    assert r.json()["isMeme"] is True and r.headers["x-cache"] == "MISS"
    n = len(calls)
    r = client.post("/classify", json={"image": IMG_NOT_MEME, "mode": "auto", "provider": "daemon2"})
    assert r.headers["x-cache"] == "MISS" and len(calls) == n + 1, "auto must not reuse an assumed isMeme"
    assert r.json() == {"isMeme": False, "filenameSlug": "original-slug"}
    r = client.post("/classify", json={"image": IMG_NOT_MEME, "mode": "auto", "provider": "daemon2"})
    assert r.headers["x-cache"] == "HIT" and r.json()["isMeme"] is False
    r = client.post("/classify", json={"image": IMG_NOT_MEME, "mode": "manual", "provider": "daemon2"})
    assert r.headers["x-cache"] == "HIT" and r.json()["isMeme"] is True, "manual daemon2 always isMeme"
    print("manual/auto cache semantics OK")

    # --- 6. worker: default provider, data: URI accepted, slug sanitized ---
    img_w = make_b64(2)
    r = client.post("/classify", json={"image": "data:image/png;base64," + img_w})
    assert r.status_code == 200 and r.json() == {"isMeme": True, "filenameSlug": "worker-slugwithunsafe"}, r.text
    wk = upstream_calls("worker.test")[-1]
    assert wk["body"]["image"] == img_w and wk["body"]["mimeType"] == "image/png"
    print("worker default provider + data: URI stripped + slug sanitized OK")

    # --- 7. upstream 429 surfaces as 429 (not a fake 'not a meme'), not cached ---
    behaviour["worker_429"] = True
    img_rl = make_b64(3)
    r = client.post("/classify", json={"image": img_rl, "provider": "worker"})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited", r.text
    assert int(r.headers["retry-after"]) == r.json()["retry_after_seconds"] > 0
    behaviour["worker_429"] = False
    r = client.post("/classify", json={"image": img_rl, "provider": "worker"})
    assert r.status_code == 200 and r.headers["x-cache"] == "MISS"
    print("upstream 429 propagated with retry_after_seconds, not cached OK")

    # --- 8. upstream timeout -> 504; service down never poisons cache ---
    behaviour["service_timeout"] = True
    img_to = make_b64(4)
    r = client.post("/classify", json={"image": img_to, "provider": "daemon2"})
    assert r.status_code == 504 and r.json()["error"] == "daemon2_ReadTimeout", r.text
    behaviour["service_timeout"] = False
    print("upstream timeout -> 504 OK")

    # --- 9. BYO-key providers: key forwarded, shared cache across providers ---
    img_g = make_b64(100)
    r = client.post("/classify", json={"image": img_g, "provider": "google", "apiKey": "g-key", "locale": "en"})
    assert r.status_code == 200 and r.json() == {"isMeme": True, "filenameSlug": "byo-google-slug"}, r.text
    r = client.post("/classify", json={"image": img_g, "provider": "claude", "apiKey": "c-key"})
    assert r.headers["x-cache"] == "HIT" and r.json()["filenameSlug"] == "byo-google-slug"
    r = client.post("/classify", json={"image": make_b64(101), "provider": "claude", "apiKey": "c-key"})
    assert r.json() == {"isMeme": False, "filenameSlug": "cat-photo"}, r.text
    r = client.post("/classify", json={"image": make_b64(102), "provider": "groq", "apiKey": "q-key"})
    assert r.status_code == 502 and r.json()["error"] == "groq_unauthorized", r.text
    r = client.post("/classify", json={"image": make_b64(103), "provider": "openai"})
    assert r.status_code == 400 and r.json()["error"] == "missing_api_key", r.text
    assert all("g-key" not in json.dumps(c["body"]) for c in calls), "API key must not be in bodies"
    print("BYO-key forwarding, shared cross-provider cache, upstream auth errors OK")

    # --- 10. request validation ---
    assert client.post("/classify", json={"image": img1, "provider": "skynet"}).json()["error"] == "unknown_provider"
    assert client.post("/classify", json={"image": img1, "mode": "weird"}).status_code == 400
    assert client.post("/classify", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/classify", json=[1, 2]).status_code == 400
    assert client.post("/classify", json={"image": 5}).json()["error"] == "invalid_field_types"
    assert client.post("/classify", json={"image": img1, "apiKey": 5}).json()["error"] == "invalid_field_types"
    assert client.post("/classify", json={"image": "!!!notbase64"}).json()["error"] == "invalid_base64"
    assert client.post("/classify", json={"image": base64.b64encode(b"hello").decode()}).json()["error"] == "invalid_image"
    assert client.post("/classify", json={"image": ""}).json()["error"] == "empty_image"
    big = "A" * (config.MAX_B64_CHARS + 4)
    assert client.post("/classify", json={"image": big}).status_code == 413
    print("request validation OK")

    # --- 11. CORS exposes the headers the extension reads ---
    r = client.post("/classify", json={"image": img1, "provider": "daemon2"},
                    headers={"Origin": "chrome-extension://abc"})
    exposed = r.headers.get("access-control-expose-headers", "").lower()
    assert "x-cache" in exposed and "x-phash" in exposed, r.headers
    pre = client.options("/classify", headers={"Origin": "chrome-extension://abc",
                                               "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200, pre.text
    print("CORS preflight + exposed X-Cache/X-Phash OK")

    # --- 12. /correct scenario A (flagged result was a cache hit) ---
    n_svc = len(upstream_calls("service.test"))
    r = client.post("/correct", json={
        "image": img1, "mode": "auto", "provider": "daemon2", "phash": phash_of(img1),
        "cache_hit": True, "previous_slug": "original-slug",
    })
    assert r.status_code == 200 and r.json()["filenameSlug"] == "corrected-slug", r.text
    svc = upstream_calls("service.test")
    assert len(svc) == n_svc + 1 and svc[-1]["body"]["rejectSlug"] == "original-slug"
    r = client.post("/classify", json={"image": img1, "provider": "daemon2"})
    assert r.headers["x-cache"] == "HIT" and r.json()["filenameSlug"] == "corrected-slug"
    print("correction scenario A: rejectSlug forwarded, cache entry overwritten OK")

    # --- 13. /correct scenario B (fresh call): cache untouched ---
    img_b = make_b64(777)
    r = client.post("/correct", json={
        "image": img_b, "mode": "auto", "provider": "worker", "phash": "",
        "cache_hit": False, "previous_slug": "some-other-wrong-slug",
    })
    assert r.status_code == 200 and r.json()["filenameSlug"] == "worker-corrected", r.text
    assert upstream_calls("worker.test")[-1]["body"]["rejectSlug"] == "some-other-wrong-slug"
    r = client.post("/classify", json={"image": img_b, "provider": "worker"})
    assert r.headers["x-cache"] == "MISS"
    print("correction scenario B: cache untouched OK")

    # --- 14. /correct scenario A with a bogus/expired phash stores under fresh phash ---
    img_c = make_b64(888)
    r = client.post("/correct", json={
        "image": img_c, "provider": "daemon2", "phash": "zzzz", "cache_hit": True,
        "previous_slug": "x" * 500,
    })
    assert r.status_code == 200
    assert len(upstream_calls("service.test")[-1]["body"]["rejectSlug"]) == 120
    r = client.post("/classify", json={"image": img_c, "provider": "daemon2"})
    assert r.headers["x-cache"] == "HIT" and r.json()["filenameSlug"] == "corrected-slug"
    assert client.post("/correct", json={"image": img_c, "cache_hit": "yes"}).status_code == 400
    assert client.post("/correct", json={"image": img_c, "cache_hit": None, "provider": "daemon2"}).status_code == 200
    # a JPEG (draft-decoded path) hashes and classifies fine
    jb = io.BytesIO(); Image.open(io.BytesIO(base64.b64decode(make_b64(4242)))).convert("CMYK").save(jb, "JPEG")
    r = client.post("/classify", json={"image": base64.b64encode(jb.getvalue()).decode(), "provider": "worker"})
    assert r.status_code == 200 and r.headers["x-cache"] == "MISS" and cache_mod.is_valid_phash(r.headers["x-phash"]), r.text
    # ...and a re-encoded copy of an already cached image is a near-duplicate HIT
    jc = io.BytesIO(); Image.open(io.BytesIO(base64.b64decode(img_c))).convert("RGB").save(jc, "JPEG", quality=70)
    r = client.post("/classify", json={"image": base64.b64encode(jc.getvalue()).decode(), "provider": "daemon2"})
    assert r.headers["x-cache"] == "HIT" and r.json()["filenameSlug"] == "corrected-slug", r.headers
    assert upstream_calls("worker.test")[-1]["body"]["mimeType"] == "image/jpeg"
    print("correction with invalid phash falls back to fresh phash, slug capped OK")

    # --- 15. metrics were written by the background writer ---
    assert application.state.metrics.flush(5)
    conn = sqlite3.connect(os.environ["METRICS_DB_PATH"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM classification_log").fetchall()
    corrections = [r for r in rows if r["was_correction"]]
    assert len(corrections) == 4, len(corrections)
    assert sum(1 for r in corrections if r["correction_was_cache_hit"] == 1) == 2
    assert sum(1 for r in corrections if r["correction_was_cache_hit"] == 0) == 2  # incl. cache_hit: null
    first = rows[0]
    assert first["provider"] == "daemon2" and first["ocr_seconds"] == 0.25 and first["vlm_seconds"] == 20.5
    assert first["ocr_used"] == 1 and first["vlm_ran"] == 1 and first["cache_hit"] == 0
    assert any(r["cache_hit"] == 1 and r["cache_hamming_distance"] == 0 for r in rows)
    assert any(r["error"] == "rate_limited" for r in rows)
    assert any(r["error"] == "invalid_base64" for r in rows)
    assert all(r["client_key"] == "testclient" for r in rows)
    conn.close()
    print(f"metrics log has {len(rows)} rows incl. upstream timings, errors and 4 corrections OK")

# --- 16. daemon2 disabled (no SERVICE_URL): misses -> 503, hits still served ---
config.SERVICE_URL = None
application2 = gw.create_app(redis_client=fake_r, http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
with TestClient(application2) as client:
    assert client.get("/health").json()["providers"]["daemon2"] is False
    r = client.post("/classify", json={"image": make_b64(999), "provider": "daemon2"})
    assert r.status_code == 503 and r.json()["error"] == "provider_unavailable", r.text
    r = client.post("/classify", json={"image": img1, "provider": "daemon2"})
    assert r.status_code == 200 and r.headers["x-cache"] == "HIT"
print("daemon2 opt-in: 503 without SERVICE_URL, cache hits still served OK")

# --- 17. Redis down: fail open, requests still complete via the provider ---
class DeadRedis:
    def pipeline(self, *a, **k): raise ConnectionError("down")
    async def get(self, *a, **k): raise ConnectionError("down")
    async def ping(self): raise ConnectionError("down")

application3 = gw.create_app(redis_client=DeadRedis(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
with TestClient(application3) as client:
    assert client.get("/health").json()["status"] == "degraded"
    r = client.post("/classify", json={"image": img1, "provider": "worker"})
    assert r.status_code == 200 and r.headers["x-cache"] == "MISS", r.text
print("Redis down: degraded health, requests fail open to provider OK")

print("\nALL GATEWAY E2E TESTS PASSED")
