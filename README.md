# Daemon Names Memes

**English** | [Русский](README.ru.md)

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

The extension has no build step, reload it from `chrome://extensions` after editing.
