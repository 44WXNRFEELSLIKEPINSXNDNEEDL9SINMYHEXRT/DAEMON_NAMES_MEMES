# Daemon Names Memes

**English | [Русский](README.ru.md)**

A browser extension that renames images as you save them: right-click any
image → "Save image as meme" → a vision model classifies it and the file is
saved with a meaningful name (`distracted-boyfriend.jpg`) in the meme's own
language and script — or with a clean date if it's not a meme.

This repo contains three parts:

| folder | what it is |
|---|---|
| `extension/` | Manifest V3 Chromium extension (no build step) |
| `worker/` | Cloudflare Worker — the shared keyless "Daemon" classifier (Gemini) |
| `service/` | Classifier service: Qwen3-VL-4B + OCR, Docker, platform-agnostic (see below) |

---

## Extension

### What it can do

- Works with plenty of AI providers. Use your own key for Google, Anthropic,
  OpenAI, OpenRouter, Groq, Mistral, or xAI. Or just use the shared Daemon
  server and skip the key entirely.
- Smart limits. Your own keys have no limit by default. The shared Daemon
  server is capped at 5 calls a minute, enforced server side.
- Optional prefix for every renamed file; pick your date format for
  non-memes; a stats dashboard in the popup; EN + RU localization.

### How to start

1. Open `chrome://extensions` in any Chromium browser.
2. Turn on Developer mode.
3. Click "Load unpacked" and pick the `extension` folder.

### Provider lineup

| Provider | Needs a key | Default limit | Good to know |
|---|---|---|---|
| Daemon | No | 5 per minute (server side) | The default. Shared server, Gemini. |
| Google | Yes | No limit | Gemini vision |
| Anthropic | Yes | No limit | Claude vision |
| OpenAI | Yes | No limit | GPT vision |
| OpenRouter | Yes | No limit | One key for lots of models |
| Groq | Yes | No limit | Llama vision, very fast |
| Mistral | Yes | No limit | Pixtral vision |
| xAI | Yes | No limit | Grok vision |

Your keys stay in your browser and only go to the provider you picked.

---

## Cloudflare Worker (the "Daemon" server)

Only needed if you want the keyless Daemon provider:

```bash
cd worker
npm install
npx wrangler secret put GEMINI_API_KEY
npm run deploy
```

The deploy applies a Durable Object migration (`new_sqlite_classes`) that
powers per-IP server-side rate limiting (free plan needs SQLite-backed DOs;
the config already uses them). `npm run dev` / `npm test` (vitest) for local
development.

---

## Self-hosted classifier service (`service/`)

A portable, self-hosted alternative to the shared Daemon worker: a plain
HTTP JSON API in a standard Docker container that runs identically on any
Docker-capable host. No platform-specific code — all host configuration is
env vars (single config layer, `service/.env.example`).

### Architecture: two-stage pipeline

```
image (base64) ──► OCR pre-pass ──┬─ manual mode, conf ≥ threshold ──► slug from text, VLM skipped
                                  └─ otherwise ──────────────────────► Qwen3-VL-4B VLM ──► {isMeme, filenameSlug}
```

- **VLM**: `Qwen/Qwen3-VL-4B-Instruct-GGUF` (Q4_K_M, ~2.5 GB) + Q8_0 mmproj,
  served by llama-cpp-python, CPU-only, grammar-constrained JSON output.
  Images downscaled to ≤384 px before inference (benchmarked: 29 s vs 622 s
  at 1024 px, identical classification output).
- **OCR**: pluggable engine — `rapidocr` (default, ONNX/CPU, real confidence
  scores, dedicated Cyrillic pack), `lightonocr` (LightOnOCR-2-1B via
  transformers, optional heavy install), or `none`.
- **Modes** (request field `mode`):
  - `manual` — the user explicitly chose "save as meme", so `isMeme` is
    `true` by definition. OCR runs first; if its confidence ≥
    `OCR_CONFIDENCE_THRESHOLD` (named constant, default **0.7**), the slug is
    built directly from the recognized text in its native script (never
    transliterated) and the VLM is skipped. Below threshold → VLM generates
    the slug; no text + no locale → English slug.
  - `auto` — unattended (downloads-API callback). The VLM **always** runs —
    it's the only stage deciding `isMeme`. OCR runs first as a cheap
    pre-pass; high-confidence text is injected into the VLM prompt as
    "detected text" context. Per-request latency is logged as three separate
    numbers: `ocr=`, `vlm=`, `total=`.

### API contract

`POST /classify`

```json
// request
{"image": "<base64>", "mimeType": "image/png", "locale": "ru", "mode": "manual"}

// response (success) — exactly this shape, no "tags"
{"isMeme": true, "filenameSlug": "бедный-хомячок-в-ложке"}

// response (any failure) — JSON error contract, never an HTML error page
{"isMeme": false, "filenameSlug": "unknown", "error": "invalid_image"}
```

Error types: `empty_image`, `payload_too_large`, `invalid_base64`,
`invalid_image`, `invalid_json_body`, `invalid_field_types`, `invalid_mode`,
`model_loading_timeout`, `model_unavailable`, `model_load_error`,
`bad_model_output`, `internal_error`, or a generation exception class name.

`GET /health` → VLM + OCR stage status. CORS: `Access-Control-Allow-Origin: *`
on **every** response path (success, errors, 500s, preflight) — the caller is
a `chrome-extension://` origin.

```bash
IMG=$(base64 -w0 meme.png)
curl -s -X POST http://localhost:8080/classify \
  -H "Content-Type: application/json" \
  -d "{\"image\": \"$IMG\", \"mimeType\": \"image/png\", \"locale\": \"ru\", \"mode\": \"manual\"}"
```

```js
// from the extension's background script
const res = await fetch("https://<HOST>/classify", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ image: base64, mimeType: "image/png", locale, mode: "manual" }),
});
const { isMeme, filenameSlug } = await res.json();
```

### Running it

```bash
cd service
docker build -t daemon-names-memes .
docker run --rm -p 8080:8080 \
  -v dnm-model-cache:/root/.cache/huggingface \
  daemon-names-memes
```

The 3 GB of model weights download on first boot (mount a volume to make
restarts instant, or pre-seed `service/models/`). Requirements: **≥ 4 GB RAM**
(VLM Q4_K_M peaks ~3.5 GB RSS), any x86_64 Docker host, outbound access to
huggingface.co. CPU-only; expect multi-second latency per request (~20–30 s
at 384 px on a 4–12-thread CPU) and several-minute cold starts. **This is a
known constraint of free/cheap CPU hosting, not a bug — do not "fix" it by
changing the model without discussing it first.**

### Deployment platforms (portability proof)

The image is host-agnostic; only the deploy commands differ.

**Fly.io** (paid instance, e.g. `shared-cpu-2x` with 4 GB):

```bash
fly launch --image <registry>/daemon-names-memes --no-deploy
fly volumes create dnm_models --size 5      # persists the HF cache
fly deploy
```

**Any Docker VPS** (Hetzner/DO/…), zero platform coupling:

```bash
docker pull <registry>/daemon-names-memes
docker run -d --restart unless-stopped -p 8080:8080 \
  -v /opt/dnm/models:/root/.cache/huggingface \
  -e OCR_CONFIDENCE_THRESHOLD=0.7 \
  daemon-names-memes
```

Notes on free tiers as of 2026-09: **HF Spaces** rejects Gradio/Docker Spaces
on the free tier (PRO required); **Render** free tier doesn't run Docker at
all and its paid 2 GB Starter OOMs this model (needs ≥4 GB); **Fly.io** has
no free tier anymore. There is currently **no genuinely free host** that fits
a 4 GB-RAM CPU model container — budget ~$5–10/mo for a small VPS, which is
also the most portable option.

### OCR model selection


| Criterion | RapidOCR (PP-OCRv5, ONNX) | LightOnOCR-2-1B (transformers) |
|---|---|---|
| Cyrillic support | ✅ dedicated `cyrillic` rec pack (ru/be/uk/bg/sr + en); also `ru`, `eslav` | ❌ documented languages: en/fr/de/es/it/nl/pt/sv/da/zh/ja — **no Russian** |
| Confidence scores | ✅ real per-line rec scores → threshold works as specified | ❌ none — heuristic constant (0.9 on non-empty output), not a measurement |
| CPU latency (typical) | ~0.2–1 s per image | multi-second; autoregressive 1B VLM on CPU |
| RAM cost | ~300 MB | ~2 GB extra on top of the VLM |
| Install weight | small (ONNX Runtime) | torch + transformers≥5 (~3 GB of packages) |
| Stylized/impact-font memes | weaker (trained on documents/scenes) | stronger (end-to-end VLM OCR) |
| License | Apache-2.0 (model © Baidu) | Apache-2.0 |

**Provisional pick: RapidOCR (`cyrillic` pack) as default**, because for
*this* pipeline — Cyrillic meme captions, a real confidence threshold,
sub-second pre-pass inside an auto-mode timing budget, minimal RAM on top of
the 3.5 GB VLM — it wins on every axis except stylized-font robustness.
LightOnOCR's lack of documented Cyrillic support alone nearly rules it out
for the RU use case; its no-confidence-score limitation also breaks the
threshold semantics the manual-mode fast path depends on. Both engines are
implemented and switchable via `OCR_ENGINE` env var, so the benchmark can
settle it empirically.

### Configuration

All knobs are env vars — see `service/.env.example` (server, request guards,
VLM model files/resolution/threads, OCR engine/language/threshold, slug
limits). Nothing host-specific is hardcoded anywhere.

---

## Development

```bash
cd worker && npm run dev && npm test        # Cloudflare worker
cd service && python _test_light.py         # model-free contract checks
cd service && PORT=7861 python app.py       # full service (needs models)
cd service && python _test_api.py           # end-to-end API tests
```

The extension has no build step — reload it from `chrome://extensions`.
