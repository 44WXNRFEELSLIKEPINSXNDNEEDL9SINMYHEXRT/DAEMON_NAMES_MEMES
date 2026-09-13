<a id="english"></a>

# Daemon Names Memes

**[English](#english) | [Русский](#russian)**

---

<a id="english"></a>

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
| DAEMON | No | 5 per minute (server side) | The default. Shared server, Gemini. |
| DAEMON2 | No | — | **Unavailable.** Self-hosted `service/` (Qwen3-VL-4B + OCR) — see below. Shown as a disabled card in Settings; can't be selected until it's actually hosted somewhere. |
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
at 384 px on a 4–12-thread CPU) and several-minute cold starts. **Cold start
is not the constraint worth optimizing right now** — see "Deployment
platforms" below: there's currently no free tier that can even boot this
container, so a slow boot on a host we can't launch on is moot. Do not "fix"
the model/quantization to shave latency without discussing it first.

### Rate limiting (self-hosted only)

`service/` has **no authentication** — anyone with the URL can call
`/classify`, and the single shared VLM already serializes every request
behind one lock at 20–30 s each. A per-IP sliding-window rate limiter
(`service/ratelimit.py`) is **enabled by default**: `RATE_LIMIT_PER_MINUTE`
(default 10), returns `429` + `Retry-After` + the same JSON error contract
(CORS still present). In-memory, process-local — fine for one instance; see
`service/README.md` for the full behavior and config knobs. The Cloudflare
Worker (`worker/`) already has its own server-side limit (5/min, enforced by
a Durable Object) — this is the equivalent protection for the self-hosted
path.

### Caching — deliberately not implemented (PoC, not production)

Content-hash → result caching (skip re-classifying an image already seen)
would cut real cost meaningfully in production, but this is a pet project /
proof of concept, not a production deployment — there's no traffic pattern
to justify the complexity yet. **Next goal**, not done now. Tracked as
future work, not a gap in the current scope.

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

---

<a id="russian"></a>

# Daemon Names Memes

**[English](#english) | [Русский](#russian)**

---

Браузерное расширение, которое переименовывает картинки при сохранении:
правый клик на любое изображение → «Сохранить изображение как мем» →
визионная модель определяет, что на картинке, и файл сохраняется с осмысленным
именем (`distracted-boyfriend.jpg`) на языке и в письменности самого мема —
или с аккуратной датой, если это не мем.

В репозитории три части:

| папка | что это |
|---|---|
| `extension/` | Chromium-расширение Manifest V3 (без шага сборки) |
| `worker/` | Cloudflare Worker — общий сервер «Daemon» без ключа (Gemini) |
| `service/` | Сервис классификации: Qwen3-VL-4B + OCR, Docker, платформенно-независимый (см. ниже) |

---

## Расширение

### Что оно умеет

- Работает с разными провайдерами ИИ: свой ключ для Google, Anthropic,
  OpenAI, OpenRouter, Groq, Mistral или xAI — либо общий сервер Daemon
  вообще без ключа.
- Умные лимиты: свои ключи без лимита, общий сервер Daemon — 5 вызовов в
  минуту, лимит проверяется на сервере.
- Необязательный префикс имён, выбор формата даты для не-мемов, панель
  статистики в попапе, локализация EN + RU.

### С чего начать

1. Откройте `chrome://extensions` в любом Chromium-браузере.
2. Включите режим разработчика.
3. Нажмите «Загрузить распакованное расширение» и выберите папку `extension`.

### Провайдеры

| Провайдер | Нужен ключ | Лимит | Полезно знать |
|---|---|---|---|
| DAEMON | Нет | 5/мин (на сервере) | По умолчанию, общий сервер, Gemini |
| DAEMON2 | Нет | — | **Недоступен.** Самостоятельно хостируемый `service/` (Qwen3-VL-4B + OCR) — см. ниже. В настройках показан как отключённая карточка; выбрать нельзя, пока не появится реальный хостинг. |
| Google | Да | Без лимита | Gemini vision |
| Anthropic | Да | Без лимита | Claude vision |
| OpenAI | Да | Без лимита | GPT vision |
| OpenRouter | Да | Без лимита | Один ключ на множество моделей |
| Groq | Да | Без лимита | Llama vision, очень быстрый |
| Mistral | Да | Без лимита | Pixtral vision |
| xAI | Да | Без лимита | Grok vision |

Ключи остаются в браузере и отправляются только выбранному провайдеру.

---

## Cloudflare Worker (сервер «Daemon»)

Нужен только для бессключевого провайдера Daemon:

```bash
cd worker
npm install
npx wrangler secret put GEMINI_API_KEY
npm run deploy
```

При деплое применяется миграция Durable Object (`new_sqlite_classes`) —
она обеспечивает серверное ограничение частоты по IP (на бесплатном тарифе
нужны DO на SQLite; конфиг уже их использует). Для локальной разработки:
`npm run dev` / `npm test` (vitest).

---

## Самостоятельно хостируемый сервис классификации (`service/`)

Портативная альтернатива общему worker'у Daemon: обычный HTTP JSON API в
стандартном Docker-контейнере, одинаково работающий на любом хосте с
Docker. Никакого платформенно-специфичного кода — вся конфигурация хоста
через переменные окружения (единый слой конфигурации,
`service/.env.example`).

### Архитектура: двухэтапный конвейер

```
картинка (base64) ──► OCR ──┬─ manual, уверенность ≥ порога ──► slug из текста, VLM пропускается
                            └─ иначе ─────────────────────────► Qwen3-VL-4B VLM ──► {isMeme, filenameSlug}
```

- **VLM**: `Qwen/Qwen3-VL-4B-Instruct-GGUF` (Q4_K_M, ~2.5 ГБ) + mmproj Q8_0
  через llama-cpp-python, только CPU, грамматически ограниченный JSON-вывод.
  Картинки уменьшаются до ≤384 px перед инференсом (замерено: 29 с против
  622 с при 1024 px при идентичном результате классификации).
- **OCR**: подключаемый движок — `rapidocr` (по умолчанию, ONNX/CPU,
  настоящие оценки уверенности, отдельный кириллический пакет),
  `lightonocr` (LightOnOCR-2-1B через transformers, опциональная тяжёлая
  установка) или `none`.
- **Режимы** (поле запроса `mode`):
  - `manual` — пользователь явно выбрал «сохранить как мем», поэтому
    `isMeme` по определению `true`. Сначала OCR; если уверенность ≥
    `OCR_CONFIDENCE_THRESHOLD` (именованная константа, по умолчанию
    **0.7**), slug строится прямо из распознанного текста в его родной
    письменности (без транслитерации), VLM пропускается. Ниже порога →
    slug генерирует VLM; нет текста и нет локали → английский slug.
  - `auto` — автоматический (колбэк downloads API). VLM **всегда**
    выполняется — только он решает `isMeme`. OCR работает как дешёвый
    предпоиск; текст с высокой уверенностью подставляется в промпт VLM как
    контекст («detected text»). Задержки логируются тремя отдельными
    числами: `ocr=`, `vlm=`, `total=`.

### Контракт API

`POST /classify`

```json
// запрос
{"image": "<base64>", "mimeType": "image/png", "locale": "ru", "mode": "manual"}

// ответ (успех) — ровно эта форма, без "tags"
{"isMeme": true, "filenameSlug": "бедный-хомячок-в-ложке"}

// ответ (любая ошибка) — JSON-контракт, никогда не HTML-страница ошибки
{"isMeme": false, "filenameSlug": "unknown", "error": "invalid_image"}
```

Типы ошибок: `empty_image`, `payload_too_large`, `invalid_base64`,
`invalid_image`, `invalid_json_body`, `invalid_field_types`, `invalid_mode`,
`model_loading_timeout`, `model_unavailable`, `model_load_error`,
`bad_model_output`, `internal_error` или имя класса исключения генерации.

`GET /health` → статус этапов VLM + OCR. CORS:
`Access-Control-Allow-Origin: *` на **каждом** пути ответа (успех, ошибки,
500, preflight) — вызывающая сторона это origin `chrome-extension://`.

```bash
IMG=$(base64 -w0 meme.png)
curl -s -X POST http://localhost:8080/classify \
  -H "Content-Type: application/json" \
  -d "{\"image\": \"$IMG\", \"mimeType\": \"image/png\", \"locale\": \"ru\", \"mode\": \"manual\"}"
```

```js
// из background-скрипта расширения
const res = await fetch("https://<HOST>/classify", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ image: base64, mimeType: "image/png", locale, mode: "manual" }),
});
const { isMeme, filenameSlug } = await res.json();
```

### Запуск

```bash
cd service
docker build -t daemon-names-memes .
docker run --rm -p 8080:8080 \
  -v dnm-model-cache:/root/.cache/huggingface \
  daemon-names-memes
```

Веса моделей (~3 ГБ) скачиваются при первом запуске (подключите volume,
чтобы перезапуски были мгновенными, или заранее положите файлы в
`service/models/`). Требования: **≥ 4 ГБ RAM** (VLM Q4_K_M на пике ~3.5 ГБ
RSS), любой x86_64-хост с Docker, доступ к huggingface.co. Только CPU;
ожидайте многомиллисекундные задержки на запрос (~20–30 с при 384 px на
4–12-поточном CPU) и холодные старты в несколько минут. **Сейчас холодный
старт — не та проблема, которую стоит оптимизировать**: см. раздел
«Платформы деплоя» ниже — бесплатного тарифа, способного вообще запустить
этот контейнер, пока нет, так что медленный старт на хосте, на котором мы
всё равно не можем запуститься, не имеет значения. Не «чините» задержку
сменой модели/квантизации без обсуждения.

### Rate limiting (только для self-hosted)

У `service/` **нет аутентификации** — вызвать `/classify` может кто угодно
с URL, а единственный общий VLM уже сериализует каждый запрос за одной
блокировкой по 20–30 с. По умолчанию **включён** rate limiter по IP со
скользящим окном (`service/ratelimit.py`): `RATE_LIMIT_PER_MINUTE`
(по умолчанию 10), при превышении — `429` + `Retry-After` + тот же
JSON-контракт ошибки (CORS сохраняется). В памяти процесса — годится для
одного инстанса; подробности и настройки в `service/README.md`. У
Cloudflare Worker (`worker/`) уже есть свой серверный лимит (5/мин через
Durable Object) — это эквивалентная защита для self-hosted пути.

### Кэширование — сознательно не реализовано (PoC, не продакшн)

Кэш по хешу содержимого (не переклассифицировать уже виденную картинку)
дал бы реальную экономию в продакшне, но это pet-проект / proof of concept,
а не продакшн-деплой — пока нет паттерна нагрузки, оправдывающего эту
сложность. **Следующая цель**, не сделано сейчас. Это будущая работа, а не
пробел в текущей области охвата.

### Платформы деплоя (доказательство портативности)

Образ не привязан к хосту; различаются только команды деплоя.

**Fly.io** (платный инстанс, например `shared-cpu-2x` с 4 ГБ):

```bash
fly launch --image <registry>/daemon-names-memes --no-deploy
fly volumes create dnm_models --size 5      # постоянный кэш HF
fly deploy
```

**Любой Docker-VPS** (Hetzner/DO/…), нулевая привязка к платформе:

```bash
docker pull <registry>/daemon-names-memes
docker run -d --restart unless-stopped -p 8080:8080 \
  -v /opt/dnm/models:/root/.cache/huggingface \
  -e OCR_CONFIDENCE_THRESHOLD=0.7 \
  daemon-names-memes
```

О бесплатных тарифах на 2026-09: **HF Spaces** не принимает Gradio/Docker
Spaces на бесплатном тарифе (нужен PRO); **Render** на бесплатном тарифе
вообще не запускает Docker, а платный Starter (2 ГБ) роняет эту модель по
OOM (нужно ≥4 ГБ); у **Fly.io** бесплатного тарифа больше нет. Сейчас
**нет по-настоящему бесплатного хоста** под контейнер с CPU-моделью на
4 ГБ RAM — закладывайте ~$5–10/мес за небольшой VPS; это заодно и самый
портативный вариант.

### Выбор OCR-модели

| Критерий | RapidOCR (PP-OCRv5, ONNX) | LightOnOCR-2-1B (transformers) |
|---|---|---|
| Кириллица | ✅ отдельный пакет `cyrillic` (ru/be/uk/bg/sr + en); также `ru`, `eslav` | ❌ документированные языки: en/fr/de/es/it/nl/pt/sv/da/zh/ja — **русского нет** |
| Оценки уверенности | ✅ настоящие построчные оценки rec → порог работает как задумано | ❌ нет — эвристическая константа (0.9 при непустом выводе), не измерение |
| Задержка на CPU (типично) | ~0.2–1 с на картинку | несколько секунд; авторегрессионная VLM 1B на CPU |
| Потребление RAM | ~300 МБ | ~2 ГБ сверх VLM |
| Вес установки | маленький (ONNX Runtime) | torch + transformers≥5 (~3 ГБ пакетов) |
| Стилизованные мемы (impact-шрифт) | слабее (обучен на документах/сценах) | сильнее (end-to-end VLM OCR) |
| Лицензия | Apache-2.0 (модели © Baidu) | Apache-2.0 |

**Предварительный выбор: RapidOCR (пакет `cyrillic`) по умолчанию**, потому
что для *этого* конвейера — кириллические подписи мемов, настоящий порог
уверенности, субсекундный предпоиск в рамках тайминг-бюджета auto-режима,
минимальный RAM сверх 3.5 ГБ VLM — он выигрывает по всем осям, кроме
устойчивости к стилизованным шрифтам. Отсутствие документированной
кириллицы у LightOnOCR само по себе почти исключает его для RU-кейса;
отсутствие оценок уверенности также ломает семантику порога, на которой
держится быстрый путь manual-режима. Оба движка реализованы и переключаются
переменной `OCR_ENGINE`, так что бенчмарк может решить вопрос эмпирически.

### Конфигурация

Все настройки — переменные окружения, см. `service/.env.example` (сервер,
ограничения запросов, файлы/разрешение/потоки VLM, движок/язык/порог OCR,
ограничения slug). Ничего платформенно-специфичного нигде не захардкожено.

---

## Разработка

```bash
cd worker && npm run dev && npm test        # Cloudflare worker
cd service && python _test_light.py         # проверки контракта без моделей
cd service && PORT=7861 python app.py       # полный сервис (нужны модели)
cd service && python _test_api.py           # сквозные тесты API
```

У расширения нет шага сборки — просто перезагрузите его на
`chrome://extensions`.
