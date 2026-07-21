const DEFAULTS = {
  apiProvider: "worker",
  apiKeys: { google: "", claude: "", openai: "", openrouter: "", groq: "", mistral: "", xai: "" },
  rateLimits: { google: 0, claude: 0, openai: 0, openrouter: 0, groq: 0, mistral: 0, xai: 0 },
  rateLimitUsage: {
    worker: { windowStart: 0, count: 0 },
    google: { windowStart: 0, count: 0 },
    claude: { windowStart: 0, count: 0 },
    openai: { windowStart: 0, count: 0 },
    openrouter: { windowStart: 0, count: 0 },
    groq: { windowStart: 0, count: 0 },
    mistral: { windowStart: 0, count: 0 },
    xai: { windowStart: 0, count: 0 }
  },
  stats: { totalDownloaded: 0, totalClassified: 0 },
  namingPrefix: "",
  dateFormat: "system"
};

const WORKER_URL = "https://daemon-meme.windown52358.workers.dev";

// Enforced server-side by the worker itself, so it can't be raised from the extension.
const WORKER_RATE_LIMIT = 5;

const GOOGLE_MODEL = "gemini-3.1-flash-lite";
const CLAUDE_MODEL = "claude-3-5-haiku-latest";

// Providers that speak the OpenAI chat-completions protocol (Bearer key, image_url content parts).
const OPENAI_COMPATIBLE = {
  openai:     { endpoint: "https://api.openai.com/v1/chat/completions",       model: "gpt-4o-mini" },
  openrouter: { endpoint: "https://openrouter.ai/api/v1/chat/completions",    model: "openai/gpt-4o-mini" },
  groq:       { endpoint: "https://api.groq.com/openai/v1/chat/completions",  model: "llama-3.2-90b-vision-preview" },
  mistral:    { endpoint: "https://api.mistral.ai/v1/chat/completions",       model: "pixtral-12b-2409" },
  xai:        { endpoint: "https://api.x.ai/v1/chat/completions",             model: "grok-2-vision-1212" }
};

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "download-as-meme",
    title: chrome.i18n.getMessage("contextMenuTitle"),
    contexts: ["image"]
  });
});

function buildPrompt(locale) {
  return `Look at this image. Determine if it's a meme (has overlaid text, a recognizable meme template, or is clearly satirical/humorous internet content).
Respond ONLY with JSON in this exact shape, no markdown fences:
{"isMeme": boolean, "filenameSlug": "short-kebab-case-description", "tags": ["tag1","tag2"]}

filenameSlug rules:
- 3-6 words, lowercase, hyphenated.
- If the image contains visible text, base the slug on that text's meaning and write it in that text's own language and native script (Cyrillic, Arabic, Devanagari, Hangul, etc). Do NOT transliterate or romanize into Latin letters.
- If there is no visible text in the image, default to this language: ${locale}.
- Must be safe as a filename (no slashes, colons, or quotes).`;
}

function parseMemeInfo(rawText) {
  try {
    const cleaned = String(rawText ?? "")
      .trim()
      .replace(/^```(?:json)?/i, "")
      .replace(/```$/, "")
      .trim();
    return JSON.parse(cleaned);
  } catch (error) {
    console.error("Meme info parse error:", error);
    return { isMeme: false, filenameSlug: "unknown", tags: [], error: "classification_failed" };
  }
}

async function classifyWithGoogle(base64, mimeType, locale, apiKey) {
  const resp = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${GOOGLE_MODEL}:generateContent`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
      body: JSON.stringify({
        contents: [{
          parts: [
            { text: buildPrompt(locale) },
            { inline_data: { mime_type: mimeType, data: base64 } }
          ]
        }],
        generationConfig: { response_mime_type: "application/json" }
      })
    }
  );
  const data = await resp.json();
  return parseMemeInfo(data.candidates?.[0]?.content?.parts?.[0]?.text);
}

async function classifyWithClaude(base64, mimeType, locale, apiKey) {
  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
      // Required by Anthropic for requests issued from a browser/extension context.
      "anthropic-dangerous-direct-browser-access": "true"
    },
    body: JSON.stringify({
      model: CLAUDE_MODEL,
      max_tokens: 300,
      messages: [{
        role: "user",
        content: [
          { type: "image", source: { type: "base64", media_type: mimeType, data: base64 } },
          { type: "text", text: buildPrompt(locale) }
        ]
      }]
    })
  });
  const data = await resp.json();
  const text = (data.content || []).map(block => block.text || "").join("");
  return parseMemeInfo(text);
}

async function classifyOpenAICompatible(endpoint, model, base64, mimeType, locale, apiKey) {
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model,
      response_format: { type: "json_object" },
      messages: [{
        role: "user",
        content: [
          { type: "text", text: buildPrompt(locale) },
          { type: "image_url", image_url: { url: `data:${mimeType};base64,${base64}` } }
        ]
      }]
    })
  });
  const data = await resp.json();
  return parseMemeInfo(data.choices?.[0]?.message?.content);
}

async function classifyWithWorker(base64, mimeType, locale) {
  const resp = await fetch(WORKER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image: base64, mimeType, locale })
  });
  if (!resp.ok) throw new Error(`Daemon worker error: ${resp.status}`);
  return await resp.json();
}

// Which engine actually runs: the selected one if usable, otherwise the Daemon worker.
function resolveProvider(settings) {
  const provider = settings.apiProvider;
  if (provider === "worker") return "worker";
  if (settings.apiKeys?.[provider]) return provider;
  return "worker";
}

async function classifyWith(provider, base64, mimeType, locale, settings) {
  const apiKey = settings.apiKeys?.[provider];

  if (provider === "worker") return classifyWithWorker(base64, mimeType, locale);
  if (provider === "google") return classifyWithGoogle(base64, mimeType, locale, apiKey);
  if (provider === "claude") return classifyWithClaude(base64, mimeType, locale, apiKey);

  const cfg = OPENAI_COMPATIBLE[provider];
  return classifyOpenAICompatible(cfg.endpoint, cfg.model, base64, mimeType, locale, apiKey);
}

function isRateLimited(usage, limit, now = Date.now()) {
  if (!limit || limit <= 0) return false; // 0 (or unset) means no limit
  const withinWindow = usage && now - usage.windowStart < 60000;
  const count = withinWindow ? usage.count : 0;
  return count >= limit;
}

function formatDate(date, format) {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");

  switch (format) {
    case "iso":
      return `${yyyy}-${mm}-${dd}`;
    case "dmy":
      return `${dd}-${mm}-${yyyy}`;
    case "mdy":
      return `${mm}-${dd}-${yyyy}`;
    case "long":
      return date.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
    case "system":
    default:
      return date.toLocaleDateString();
  }
}

function sanitizeFilename(part) {
  return String(part ?? "")
    .replace(/[\\/:*?"<>|\u0000-\u001f\s]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "");
}

function dateStamp(format) {
  return sanitizeFilename(formatDate(new Date(), format));
}

async function recordStats(memeInfo, imageBlob, provider) {
  const { stats, rateLimitUsage } = await chrome.storage.local.get(["stats", "rateLimitUsage"]);

  const newStats = {
    totalDownloaded: (stats?.totalDownloaded || 0) + (memeInfo.isMeme ? 1 : 0),
    totalClassified: (stats?.totalClassified || 0) + 1
  };

  const now = Date.now();
  const usage = rateLimitUsage?.[provider] || { windowStart: 0, count: 0 };
  const withinWindow = now - usage.windowStart < 60000;

  chrome.storage.local.set({
    stats: newStats,
    rateLimitUsage: {
      ...rateLimitUsage,
      [provider]: withinWindow
        ? { windowStart: usage.windowStart, count: usage.count + 1 }
        : { windowStart: now, count: 1 }
    },
    lastPreview: {
      imageDataUrl: `data:${imageBlob.type};base64,${await blobToBase64(imageBlob)}`,
      filenameSlug: memeInfo.filenameSlug,
      isMeme: memeInfo.isMeme,
      timestamp: now
    }
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "download-as-meme") return;

  try {
    const settings = await chrome.storage.local.get(DEFAULTS);
    const provider = resolveProvider(settings);
    const prefix = settings.namingPrefix ? `${settings.namingPrefix}_` : "";

    const imgResp = await fetch(info.srcUrl);
    const blob = await imgResp.blob();
    const ext = blob.type.split("/")[1] || "jpg";
    const fallbackFilename = `${prefix}${dateStamp(settings.dateFormat)}.${ext}`;

    const limit = provider === "worker" ? WORKER_RATE_LIMIT : settings.rateLimits[provider];
    if (isRateLimited(settings.rateLimitUsage?.[provider], limit)) {
      chrome.downloads.download({ url: info.srcUrl, filename: fallbackFilename });
      return;
    }

    const base64 = await blobToBase64(blob);
    const locale = chrome.i18n.getUILanguage();
    const memeInfo = await classifyWith(provider, base64, blob.type, locale, settings);

    const filename = memeInfo.isMeme
      ? `${prefix}${sanitizeFilename(memeInfo.filenameSlug)}.${ext}`
      : fallbackFilename;

    chrome.downloads.download({ url: info.srcUrl, filename });
    await recordStats(memeInfo, blob, provider);
  } catch (err) {
    console.error("Meme classify failed:", err);
    chrome.downloads.download({ url: info.srcUrl });
  }
});

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result.split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}
