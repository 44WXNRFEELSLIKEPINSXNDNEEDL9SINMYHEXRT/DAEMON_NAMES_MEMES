# service/ — technical reference

User-facing docs (EN/RU, deployment guides, model selection): see
`README.md` (English) and `README.ru.md` (Russian) in the repo root.

## Files

| file | purpose |
|---|---|
| `app.py` | FastAPI entrypoint (`POST /classify`, `GET /health`), CORS, optional Gradio debug UI (`ENABLE_GRADIO_UI=1`) |
| `config.py` | single config layer — every setting env-overridable, no hardcoded host config |
| `pipeline.py` | two-stage pipeline: OCR pre-pass + Qwen3-VL-4B GGUF VLM; manual/auto modes; per-stage latency logging |
| `ocr.py` | pluggable OCR engines: `rapidocr` (default), `lightonocr`, `none` |
| `requirements.txt` | pinned default runtime |
| `requirements-lightonocr.txt` | optional heavy stack (torch + transformers≥5) for the LightOnOCR engine |
| `Dockerfile` | platform-agnostic CPU image; `--build-arg INSTALL_LIGHTONOCR=1` for the optional engine |
| `.env.example` | all env vars with defaults and comments |
| `_test_api.py` | end-to-end API tests (needs a running server + one image) |
| `_test_light.py` | model-free checks (contract, slug helpers, wiring) |
| `_bench_ocr.py` | OCR A/B benchmark harness (run on a host with ≥8 GB RAM) |

## Output contract

```json
{"isMeme": true, "filenameSlug": "short-kebab-slug"}
```

Error shape (any failure): `{"isMeme": false, "filenameSlug": "unknown",
"error": "<short type>"}`. **No `tags` field anywhere — removed from the
contract, do not re-add.**

## Request

`POST /classify`:

```json
{"image": "<base64>", "mimeType": "image/png", "locale": "ru", "mode": "manual"}
```

`mode`: `"manual"` (user clicked "save as meme" → isMeme assumed true; OCR
fast path above `OCR_CONFIDENCE_THRESHOLD`, else VLM for the slug only) or
`"auto"` (default; VLM always runs and decides isMeme; confident OCR text is
injected into the VLM prompt as context).

## Latency logging

Every successful request logs three separate numbers:

```
classify ok | mode=auto ocr_injected=True | ocr=0.42s vlm=21.37s total=21.95s | isMeme=True slug='...'
```

## Run locally

```bash
uv venv --python 3.11 .venv
VIRTUAL_ENV=$PWD/.venv uv pip install -r requirements.txt
hf download Qwen/Qwen3-VL-4B-Instruct-GGUF \
  Qwen3VL-4B-Instruct-Q4_K_M.gguf mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf \
  --local-dir models
PORT=7861 .venv/bin/python app.py
```
