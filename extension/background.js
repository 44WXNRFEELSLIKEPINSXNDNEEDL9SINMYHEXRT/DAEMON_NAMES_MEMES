const DEFAULTS = {
  apiProvider: "google",
  apiKeys: { google: "", claude: "", openai: "" },
  rateLimits: { google: 60, claude: 20, openai: 20 },
  rateLimitUsage: {
    google: { windowStart: 0, count: 0 },
    claude: { windowStart: 0, count: 0 },
    openai: { windowStart: 0, count: 0 }
  },
  stats: { totalDownloaded: 0, totalClassified: 0 },
  namingPrefix: "",
  dateFormat: "system"
};

const WORKER_URL = "https://daemon-meme.windown52358.workers.dev";

const MODELS = {
  google: "gemini-3.1-flash-lite",
  claude: "claude-3-5-haiku-latest",
  openai: "gpt-4o-mini"
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
    `https://generativelanguage.googleapis.com/v1beta/models/${MODELS.google}:generateContent`,
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
      model: MODELS.claude,
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

async function classifyWithOpenAI(base64, mimeType, locale, apiKey) {
  const resp = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model: MODELS.openai,
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
  return await resp.json();
}

const CLASSIFIERS = {
  google: classifyWithGoogle,
  claude: classifyWithClaude,
  openai: classifyWithOpenAI
};

async function classifyImage(base64, mimeType, locale, settings) {
  const provider = settings.apiProvider;
  const apiKey = settings.apiKeys?.[provider];

  if (apiKey) {
    const info = await CLASSIFIERS[provider](base64, mimeType, locale, apiKey);
    return { info, viaWorker: false };
  }

  const info = await classifyWithWorker(base64, mimeType, locale);
  return { info, viaWorker: true };
}

function isRateLimited(usage, limit, now = Date.now()) {
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

async function recordStats(memeInfo, imageBlob, provider, countUsage) {
  const { stats, rateLimitUsage } = await chrome.storage.local.get(["stats", "rateLimitUsage"]);

  const newStats = {
    totalDownloaded: (stats?.totalDownloaded || 0) + (memeInfo.isMeme ? 1 : 0),
    totalClassified: (stats?.totalClassified || 0) + 1
  };

  const now = Date.now();
  const toStore = {
    stats: newStats,
    lastPreview: {
      imageDataUrl: `data:${imageBlob.type};base64,${await blobToBase64(imageBlob)}`,
      filenameSlug: memeInfo.filenameSlug,
      isMeme: memeInfo.isMeme,
      timestamp: now
    }
  };

  if (countUsage) {
    const usage = rateLimitUsage?.[provider] || { windowStart: 0, count: 0 };
    const withinWindow = now - usage.windowStart < 60000;
    toStore.rateLimitUsage = {
      ...rateLimitUsage,
      [provider]: withinWindow
        ? { windowStart: usage.windowStart, count: usage.count + 1 }
        : { windowStart: now, count: 1 }
    };
  }

  chrome.storage.local.set(toStore);
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "download-as-meme") return;

  try {
    const settings = await chrome.storage.local.get(DEFAULTS);
    const provider = settings.apiProvider;
    const prefix = settings.namingPrefix ? `${settings.namingPrefix}_` : "";

    const imgResp = await fetch(info.srcUrl);
    const blob = await imgResp.blob();
    const ext = blob.type.split("/")[1] || "jpg";
    const fallbackFilename = `${prefix}${dateStamp(settings.dateFormat)}.${ext}`;

    const hasKey = !!settings.apiKeys?.[provider];
    if (hasKey && isRateLimited(settings.rateLimitUsage?.[provider], settings.rateLimits[provider])) {
      chrome.downloads.download({ url: info.srcUrl, filename: fallbackFilename });
      return;
    }

    const base64 = await blobToBase64(blob);
    const locale = chrome.i18n.getUILanguage();
    const { info: memeInfo, viaWorker } = await classifyImage(base64, blob.type, locale, settings);

    const filename = memeInfo.isMeme
      ? `${prefix}${sanitizeFilename(memeInfo.filenameSlug)}.${ext}`
      : fallbackFilename;

    chrome.downloads.download({ url: info.srcUrl, filename });
    await recordStats(memeInfo, blob, provider, !viaWorker);
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
