# Daemon Names Memes

**[English](#english) | [Русский](#russian)**

---

<a id="english"></a>

> Rename the images you save from the web with meaningful, meme-aware filenames — powered by AI vision models.

Right-click any image on a page → **Save image as meme**. The extension asks a vision model whether it's a meme and, if so, downloads it under a short, descriptive name (e.g. `distracted-boyfriend.jpg`) instead of a random hash. Non-memes get a clean date-based name.

## How it works

1. You right-click an image and choose **Save image as meme**.
2. The extension fetches the image and sends it to the selected classifier — your own API key, or the shared Daemon server.
3. The classifier replies with `{ isMeme, filenameSlug, tags }`.
4. The image is downloaded:
   - **Meme** → `<prefix><slug>.<ext>` — the slug is written in the meme's own language and script (Latin, Cyrillic, Arabic, Hangul, …).
   - **Not a meme** → `<prefix><date>.<ext>` using your chosen date format.

## Features

- **Multiple AI providers** — bring your own key for Google (Gemini), Anthropic (Claude), OpenAI, OpenRouter, Groq, Mistral, or xAI (Grok); or use the shared **Daemon** server with no key at all.
- **Rate limiting** — per-minute caps per provider. Your own keys default to **no limit** (`0`); the shared Daemon server is capped at **5/min and enforced server-side**, so it can't be bypassed from the extension.
- **Filename prefix** — an optional prefix prepended to every renamed file.
- **Date format** — choose how non-meme filenames are dated (system, ISO, DD-MM-YYYY, MM-DD-YYYY, long).
- **Native-script names** — meme slugs preserve the language of the text in the image.
- **Live stats popup** — active provider & key, rate-limit usage, total API calls, memes named, and a preview of the last classified image.
- **Bilingual UI** — English and Russian.

## Providers

| Provider | Key needed | Default limit | Notes |
|---|---|---|---|
| **Daemon** | No | 5/min (server-enforced) | Default. Shared Cloudflare Worker using Gemini. |
| Google | Yes | No limit | Gemini vision |
| Anthropic | Yes | No limit | Claude vision |
| OpenAI | Yes | No limit | GPT vision |
| OpenRouter | Yes | No limit | Many models, one key |
| Groq | Yes | No limit | Llama vision, fast |
| Mistral | Yes | No limit | Pixtral vision |
| xAI | Yes | No limit | Grok vision |

## Project structure

```
├── extension/            # Manifest V3 browser extension
│   ├── background.js     # Service worker: menu, fetch, provider dispatch, rate limits, naming
│   ├── popup.html/.js    # Stats dashboard
│   ├── options.html/.js  # Settings (providers, keys, limits, prefix, date format)
│   ├── manifest.json
│   ├── _locales/         # en, ru
│   └── images/
└── worker/               # Cloudflare Worker (the "Daemon" server)
    ├── src/index.ts      # Gemini classification + per-IP rate limiting (Durable Object)
    ├── wrangler.jsonc
    └── ...
```

## Installation

### Extension

1. Open `chrome://extensions` (any Chromium-based browser).
2. Enable **Developer mode**.
3. Click **Load unpacked** and select the `extension/` folder.

### Worker (Daemon server)

The worker is only needed if you want the keyless **Daemon** provider.

```bash
cd worker
npm install
npx wrangler secret put GEMINI_API_KEY   # your Google Gemini API key
npm run deploy
```

> The deploy applies a Durable Object migration (`new_sqlite_classes`) used for server-side rate limiting. On the free plan this requires SQLite-backed Durable Objects, which the config already uses.

## Configuration

- **API keys & provider** — open the extension's options page (popup → **Settings**). Keys are stored locally in your browser and sent only to the chosen provider.
- **Worker secrets** — `GEMINI_API_KEY` is set via `wrangler secret put` and is never exposed to the extension.

## Development

```bash
cd worker
npm run dev      # local worker (wrangler dev)
npm test         # vitest
```

The extension has no build step — reload it from `chrome://extensions` after editing.

---

<a id="russian"></a>

> Переименовывайте сохраняемые из интернета изображения, давая им осмысленные имена на основе мемов — с помощью ИИ-моделей зрения.

Нажмите правой кнопкой мыши на любое изображение на странице → **Сохранить изображение как мем**. Расширение спросит модель зрения, мем ли это, и если да — загрузит файл с коротким описательным именем (например, `distracted-boyfriend.jpg`) вместо случайного хэша. Не-мемы получают аккуратное имя на основе даты.

## Как это работает

1. Вы нажимаете правой кнопкой мыши на изображение и выбираете **Сохранить изображение как мем**.
2. Расширение загружает изображение и отправляет его выбранному классификатору — вашему собственному API-ключу или общему серверу Daemon.
3. Классификатор возвращает `{ isMeme, filenameSlug, tags }`.
4. Изображение загружается:
   - **Мем** → `<префикс><slug>.<ext>` — slug записывается на языке и письменности самого мема (латиница, кириллица, арабица, хангыль, …).
   - **Не мем** → `<префикс><дата>.<ext>` в выбранном вами формате даты.

## Возможности

- **Несколько ИИ-провайдеров** — используйте свой ключ для Google (Gemini), Anthropic (Claude), OpenAI, OpenRouter, Groq, Mistral или xAI (Grok); либо общий сервер **Daemon** вообще без ключа.
- **Ограничение частоты** — лимиты в минуту для каждого провайдера. Для ваших ключей по умолчанию **без лимита** (`0`); общий сервер Daemon ограничен **5/мин и контролируется на стороне сервера**, так что обойти это из расширения нельзя.
- **Префикс имени файла** — необязательный префикс для каждого переименованного файла.
- **Формат даты** — выберите, как датируются имена не-мемов (системный, ISO, ДД-ММ-ГГГГ, ММ-ДД-ГГГГ, длинный).
- **Имена на родной письменности** — slug мема сохраняет язык текста на изображении.
- **Живая статистика во всплывающем окне** — активный провайдер и ключ, использование лимита, всего API-вызовов, сколько мемов названо, превью последнего классифицированного изображения.
- **Двуязычный интерфейс** — английский и русский.

## Провайдеры

| Провайдер | Нужен ключ | Лимит по умолчанию | Примечания |
|---|---|---|---|
| **Daemon** | Нет | 5/мин (на сервере) | По умолчанию. Общий Cloudflare Worker на Gemini. |
| Google | Да | Без лимита | Gemini vision |
| Anthropic | Да | Без лимита | Claude vision |
| OpenAI | Да | Без лимита | GPT vision |
| OpenRouter | Да | Без лимита | Много моделей, один ключ |
| Groq | Да | Без лимита | Llama vision, быстрый |
| Mistral | Да | Без лимита | Pixtral vision |
| xAI | Да | Без лимита | Grok vision |

## Структура проекта

```
├── extension/            # Браузерное расширение Manifest V3
│   ├── background.js     # Сервис-воркер: меню, загрузка, выбор провайдера, лимиты, имена
│   ├── popup.html/.js    # Панель статистики
│   ├── options.html/.js  # Настройки (провайдеры, ключи, лимиты, префикс, формат даты)
│   ├── manifest.json
│   ├── _locales/         # en, ru
│   └── images/
└── worker/               # Cloudflare Worker (сервер «Daemon»)
    ├── src/index.ts      # Классификация Gemini + ограничение по IP (Durable Object)
    ├── wrangler.jsonc
    └── ...
```

## Установка

### Расширение

1. Откройте `chrome://extensions` (любой браузер на Chromium).
2. Включите **Режим разработчика**.
3. Нажмите **Загрузить распакованное расширение** и выберите папку `extension/`.

### Worker (сервер Daemon)

Worker нужен только если вы хотите использовать провайдер **Daemon** без ключа.

```bash
cd worker
npm install
npx wrangler secret put GEMINI_API_KEY   # ваш API-ключ Google Gemini
npm run deploy
```

> При деплое применяется миграция Durable Object (`new_sqlite_classes`), используемая для ограничения частоты на стороне сервера. На бесплатном тарифе требуются Durable Objects на базе SQLite — конфигурация уже использует их.

## Конфигурация

- **API-ключи и провайдер** — откройте страницу настроек расширения (всплывающее окно → **Settings**). Ключи хранятся локально в браузере и отправляются только выбранному провайдеру.
- **Секреты Worker** — `GEMINI_API_KEY` задаётся через `wrangler secret put` и никогда не передаётся расширению.

## Разработка

```bash
cd worker
npm run dev      # локальный worker (wrangler dev)
npm test         # vitest
```

У расширения нет шага сборки — после правок просто перезагрузите его на `chrome://extensions`.
