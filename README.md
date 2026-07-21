# Daemon Names Memes

**[English](#english) | [Русский](#russian)**

---

<a id="english"></a>

## For users

### What is this

You know how every picture you save ends up named something like `image (47).png` or `IMG_20260721.jpg`? This extension fixes that. Right click any image on a page, pick "Save image as meme", and it works out what the picture is and saves it with a name that actually makes sense.

If it's a meme, you get a short description like `distracted-boyfriend.jpg`. And here's the nice part: it keeps the language of the text in the meme. So a Russian meme gets a Russian name, a Japanese one gets a Japanese name, and so on. If it's not a meme, you get a clean date instead of a random string of numbers.

### What it can do

- Works with plenty of AI providers. Use your own key for Google, Anthropic, OpenAI, OpenRouter, Groq, Mistral, or xAI. Or just use the shared Daemon server and skip the key entirely.
- Smart limits. Your own keys have no limit by default. The shared Daemon server is capped at 5 calls a minute, and that cap is enforced on the server, so it can't be cheated.
- Optional prefix. Add a prefix to every file you rename, or leave it blank.
- Pick your date format for the files that aren't memes: system, ISO, DD-MM-YYYY, MM-DD-YYYY, or the long form.
- A little dashboard in the popup shows your active provider, how much of the limit you've used, total calls, memes named, and the last image you classified.
- The whole thing is available in English and Russian.

### How to start

1. Open `chrome://extensions` in any browser built on Chromium.
2. Turn on Developer mode.
3. Click "Load unpacked" and pick the `extension` folder.

That's it. Now right click any image and choose "Save image as meme".

### Which provider should I pick

If you just want it to work without signing up for anything, stick with Daemon. It's the default and needs no key.

If you have your own API key and want more speed or a specific model, open the popup, click Settings, and choose your provider. Paste the key, and you're set. Here's the lineup:

| Provider | Needs a key | Default limit | Good to know |
|---|---|---|---|
| Daemon | No | 5 per minute (server side) | The default. Runs on a shared server with Gemini. |
| Google | Yes | No limit | Gemini vision |
| Anthropic | Yes | No limit | Claude vision |
| OpenAI | Yes | No limit | GPT vision |
| OpenRouter | Yes | No limit | One key for lots of models |
| Groq | Yes | No limit | Llama vision, very fast |
| Mistral | Yes | No limit | Pixtral vision |
| xAI | Yes | No limit | Grok vision |

Your keys stay in your browser. They only ever go to the provider you picked.

---

## For developers

### How it works under the hood

1. You right click an image and pick "Save image as meme".
2. The extension grabs the image and sends it to the chosen classifier. That's either your own API key or the shared Daemon server.
3. The classifier answers with `{ isMeme, filenameSlug, tags }`.
4. The file gets downloaded:
   - Meme: `<prefix><slug>.<ext>`. The slug is in the meme's own language and script (Latin, Cyrillic, Arabic, Hangul, and more).
   - Not a meme: `<prefix><date>.<ext>` in your chosen date format.

### Project layout

```
├── extension/            # Manifest V3 browser extension
│   ├── background.js     # Service worker: menu, fetch, provider dispatch, rate limits, naming
│   ├── popup.html/.js    # Stats dashboard
│   ├── options.html/.js  # Settings: providers, keys, limits, prefix, date format
│   ├── manifest.json
│   ├── _locales/         # en, ru
│   └── images/
└── worker/               # Cloudflare Worker, the "Daemon" server
    ├── src/index.ts      # Gemini classification plus per IP rate limiting (Durable Object)
    ├── wrangler.jsonc
    └── ...
```

### Running the Daemon server

You only need this if you want the keyless Daemon provider.

```bash
cd worker
npm install
npx wrangler secret put GEMINI_API_KEY   # your Google Gemini API key
npm run deploy
```

Note: the deploy applies a Durable Object migration (`new_sqlite_classes`) that powers the server side rate limiting. On the free plan this needs SQLite backed Durable Objects, which the config already uses.

### Configuration

- API keys and provider: open the extension's options page (popup, then Settings). Keys are stored locally in the browser and only sent to the chosen provider.
- Worker secrets: `GEMINI_API_KEY` is set with `wrangler secret put` and never reaches the extension.

### Development

```bash
cd worker
npm run dev      # local worker (wrangler dev)
npm test         # vitest
```

The extension has no build step. After editing, just reload it from `chrome://extensions`.

---

<a id="russian"></a>

## Для пользователей

### Что это

Знакомо, когда каждая сохранённая картинка называется вроде `image (47).png` или `IMG_20260721.jpg`? Это расширение это чинит. Нажимаете правой кнопкой на любое изображение, выбираете «Сохранить изображение как мем», и расширение само понимает, что на картинке, и сохраняет её с нормальным именем.

Если это мем, получите короткое описание, например `distracted-boyfriend.jpg`. И вот что приятно: оно сохраняет язык текста в меме. Так русский мем получает русское имя, японский получает японское, и так далее. Если это не мем, вместо случайных цифр будет аккуратная дата.

### Что оно умеет

- Работает с разными провайдерами ИИ. Используйте свой ключ для Google, Anthropic, OpenAI, OpenRouter, Groq, Mistral или xAI. Или просто используйте общий сервер Daemon и вообще без ключа.
- Умные лимиты. Ваши собственные ключи по умолчанию без лимита. Общий сервер Daemon ограничен 5 вызовами в минуту, и этот лимит проверяется на сервере, так что обойти его нельзя.
- Необязательный префикс. Добавьте префикс к каждому переименованному файлу или оставьте пустым.
- Выберите формат даты для файлов, которые не мемы: системный, ISO, ДД-ММ-ГГГГ, ММ-ДД-ГГГГ или длинный.
- Небольшая панель во всплывающем окне показывает активного провайдера, сколько лимита использовано, всего вызовов, сколько мемов названо и последнее классифицированное изображение.
- Всё доступно на английском и русском.

### С чего начать

1. Откройте `chrome://extensions` в любом браузере на Chromium.
2. Включите режим разработчика.
3. Нажмите «Загрузить распакованное расширение» и выберите папку `extension`.

Готово. Теперь нажмите правой кнопкой на любое изображение и выберите «Сохранить изображение как мем».

### Какого провайдера выбрать

Если хотите, чтобы просто работало без регистраций, оставайтесь на Daemon. Он по умолчанию и не требует ключа.

Если у вас есть свой ключ API и хочется больше скорости или конкретную модель, откройте всплывающее окно, нажмите Settings и выберите провайдера. Вставьте ключ, и всё. Вот список:

| Провайдер | Нужен ключ | Лимит по умолчанию | Полезно знать |
|---|---|---|---|
| Daemon | Нет | 5 в минуту (на сервере) | По умолчанию. Работает на общем сервере с Gemini. |
| Google | Да | Без лимита | Gemini vision |
| Anthropic | Да | Без лимита | Claude vision |
| OpenAI | Да | Без лимита | GPT vision |
| OpenRouter | Да | Без лимита | Один ключ для множества моделей |
| Groq | Да | Без лимита | Llama vision, очень быстрый |
| Mistral | Да | Без лимита | Pixtral vision |
| xAI | Да | Без лимита | Grok vision |

Ваши ключи остаются в браузере. Они отправляются только тому провайдеру, которого вы выбрали.

---

## Для разработчиков

### Как это работает внутри

1. Вы нажимаете правой кнопкой на изображение и выбираете «Сохранить изображение как мем».
2. Расширение загружает изображение и отправляет его выбранному классификатору. Это либо ваш ключ API, либо общий сервер Daemon.
3. Классификатор отвечает `{ isMeme, filenameSlug, tags }`.
4. Файл скачивается:
   - Мем: `<префикс><slug>.<ext>`. Slug на языке и письменности самого мема (латиница, кириллица, арабица, хангыль и другие).
   - Не мем: `<префикс><дата>.<ext>` в выбранном формате даты.

### Структура проекта

```
├── extension/            # Браузерное расширение Manifest V3
│   ├── background.js     # Сервис-воркер: меню, загрузка, выбор провайдера, лимиты, имена
│   ├── popup.html/.js    # Панель статистики
│   ├── options.html/.js  # Настройки: провайдеры, ключи, лимиты, префикс, формат даты
│   ├── manifest.json
│   ├── _locales/         # en, ru
│   └── images/
└── worker/               # Cloudflare Worker, сервер «Daemon»
    ├── src/index.ts      # Классификация Gemini и ограничение по IP (Durable Object)
    ├── wrangler.jsonc
    └── ...
```

### Запуск сервера Daemon

Нужно только если вы хотите провайдер Daemon без ключа.

```bash
cd worker
npm install
npx wrangler secret put GEMINI_API_KEY   # ваш ключ API Google Gemini
npm run deploy
```

Примечание: при деплое применяется миграция Durable Object (`new_sqlite_classes`), которая обеспечивает ограничение частоты на сервере. На бесплатном тарифе нужны Durable Objects на базе SQLite, конфигурация уже их использует.

### Конфигурация

- Ключи API и провайдер: откройте страницу настроек расширения (всплывающее окно, затем Settings). Ключи хранятся локально в браузере и отправляются только выбранному провайдеру.
- Секреты Worker: `GEMINI_API_KEY` задаётся через `wrangler secret put` и никогда не попадает в расширение.

### Разработка

```bash
cd worker
npm run dev      # локальный worker (wrangler dev)
npm test         # vitest
```

У расширения нет шага сборки. После правок просто перезагрузите его на `chrome://extensions`.
