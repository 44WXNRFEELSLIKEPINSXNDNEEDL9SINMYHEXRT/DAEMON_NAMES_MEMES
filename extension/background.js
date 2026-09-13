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
  dateFormat: "system",
  downloadMode: "context",
  saveMethod: "direct"
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

// Chrome's Save As dialog opens in the directory of the suggested filename and
// does not remember where the user last saved, so remember it ourselves and
// pre-select it on the next save. The downloads API only accepts paths
// relative to the default Downloads folder (absolute paths cause an error), so
// folders outside of it cannot be pre-selected — that's a Chrome limitation.
const ROOT_PROBE_FILENAME = "__dnm_root_probe__.txt";

function dirnameOf(filePath) {
  const sepIndex = Math.max(filePath.lastIndexOf("/"), filePath.lastIndexOf("\\"));
  return sepIndex <= 0 ? null : filePath.slice(0, sepIndex);
}

// Discover the absolute path of the default Downloads folder by downloading a
// throwaway file with a bare suggested name and reading back where it landed.
function calibrateDownloadsRoot() {
  return new Promise((resolve) => {
    chrome.downloads.download(
      { url: "data:text/plain,root-probe", filename: ROOT_PROBE_FILENAME, saveAs: false },
      async (probeId) => {
        if (probeId == null || chrome.runtime.lastError) return resolve(null);
        let root = null;
        for (let i = 0; i < 40; i++) {
          await new Promise((r) => setTimeout(r, 50));
          const [item] = await chrome.downloads.search({ id: probeId });
          if (!item) break;
          if (item.state === "complete") {
            root = dirnameOf(item.filename || "");
            break;
          }
          if (item.state === "interrupted") break;
        }
        chrome.downloads.removeFile({ id: probeId }, () => {
          void chrome.runtime.lastError;
          chrome.downloads.erase({ id: probeId }, () => void chrome.runtime.lastError);
        });
        resolve(root);
      }
    );
  });
}

async function getDownloadsRoot() {
  const { downloadsRoot } = await chrome.storage.local.get("downloadsRoot");
  if (downloadsRoot) return downloadsRoot;
  const root = await calibrateDownloadsRoot();
  if (root) await chrome.storage.local.set({ downloadsRoot: root });
  return root || null;
}

async function startDownload({ url, filename, saveAs }) {
  if (!saveAs) {
    chrome.downloads.download({ url, filename, saveAs: false });
    return;
  }

  const base = filename.split(/[\\/]/).pop();
  const { lastSaveSubdir } = await chrome.storage.local.get("lastSaveSubdir");
  const target = lastSaveSubdir ? `${lastSaveSubdir.replace(/[\\/]+$/, "")}/${base}` : base;
  chrome.downloads.download({ url, filename: target, saveAs: true }, trackSaveAsDownload);
}

async function trackSaveAsDownload(id) {
  if (id == null || chrome.runtime.lastError) return;
  const { pendingSaveAsIds = [] } = await chrome.storage.session.get("pendingSaveAsIds");
  if (!pendingSaveAsIds.includes(id)) {
    await chrome.storage.session.set({ pendingSaveAsIds: [...pendingSaveAsIds, id] });
  }
}

async function untrackSaveAsDownload(id) {
  const { pendingSaveAsIds = [] } = await chrome.storage.session.get("pendingSaveAsIds");
  if (pendingSaveAsIds.includes(id)) {
    await chrome.storage.session.set({ pendingSaveAsIds: pendingSaveAsIds.filter(x => x !== id) });
  }
}

async function rememberSaveSubdirectory(filePath) {
  const dir = dirnameOf(filePath);
  if (!dir) return;
  const root = await getDownloadsRoot();
  if (!root) return;

  if (dir === root) {
    await chrome.storage.local.remove("lastSaveSubdir");
    return;
  }

  const sep = root.includes("\\") && !root.includes("/") ? "\\" : "/";
  if (dir.startsWith(root + sep)) {
    const subdir = dir.slice(root.length + 1).replace(/\\/g, "/");
    await chrome.storage.local.set({ lastSaveSubdir: subdir });
  }
  // A folder outside Downloads can't be suggested to the Save As dialog, so
  // keep the previous remembered subfolder.
}

chrome.downloads.onChanged.addListener(async (delta) => {
  const state = delta.state?.current;
  if (state !== "complete" && state !== "interrupted") return;

  const { pendingSaveAsIds = [] } = await chrome.storage.session.get("pendingSaveAsIds");
  if (!pendingSaveAsIds.includes(delta.id)) return;

  if (state === "complete") {
    const [item] = await chrome.downloads.search({ id: delta.id });
    if (item?.filename) await rememberSaveSubdirectory(item.filename);
  }
  await untrackSaveAsDownload(delta.id);
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "download-as-meme") return;

  try {
    const settings = await chrome.storage.local.get(DEFAULTS);
    const provider = resolveProvider(settings);
    const prefix = settings.namingPrefix ? `${settings.namingPrefix}_` : "";
    const saveAs = settings.saveMethod === "saveAs";

    const imgResp = await fetch(info.srcUrl);
    const blob = await imgResp.blob();
    const ext = blob.type.split("/")[1] || "jpg";
    const fallbackFilename = `${prefix}${dateStamp(settings.dateFormat)}.${ext}`;

    const limit = provider === "worker" ? WORKER_RATE_LIMIT : settings.rateLimits[provider];
    if (isRateLimited(settings.rateLimitUsage?.[provider], limit)) {
      await startDownload({ url: info.srcUrl, filename: fallbackFilename, saveAs });
      return;
    }

    const base64 = await blobToBase64(blob);
    const locale = chrome.i18n.getUILanguage();
    const memeInfo = await classifyWith(provider, base64, blob.type, locale, settings);

    const filename = memeInfo.isMeme
      ? `${prefix}${sanitizeFilename(memeInfo.filenameSlug)}.${ext}`
      : fallbackFilename;

    await startDownload({ url: info.srcUrl, filename, saveAs });
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

// Auto-rename: intercept every image download, classify, and re-issue with a proper name.
chrome.downloads.onCreated.addListener(async (item) => {
  // Skip downloads this extension initiated (context-menu flow or our own re-downloads).
  if (item.byExtensionId === chrome.runtime.id) return;

  const settings = await chrome.storage.local.get(DEFAULTS);
  if (settings.downloadMode !== "all") return;

  const isImage =
    (item.mime && item.mime.startsWith("image/")) ||
    /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i.test(item.url);
  if (!isImage) return;

  // Blob / data URLs can't be re-fetched after cancel — leave them alone.
  if (item.url.startsWith("blob:") || item.url.startsWith("data:")) return;

  try {
    await chrome.downloads.cancel(item.id);
  } catch {
    return; // Already completed or canceled — nothing to rename.
  }

  const prefix = settings.namingPrefix ? `${settings.namingPrefix}_` : "";
  const saveAs = settings.saveMethod === "saveAs";
  const ext =
    item.filename && item.filename.includes(".")
      ? item.filename.split(".").pop()
      : "jpg";
  const fallbackFilename = `${prefix}${dateStamp(settings.dateFormat)}.${ext}`;

  try {
    const provider = resolveProvider(settings);
    const limit = provider === "worker" ? WORKER_RATE_LIMIT : settings.rateLimits[provider];

    if (isRateLimited(settings.rateLimitUsage?.[provider], limit)) {
      await startDownload({ url: item.url, filename: fallbackFilename, saveAs });
      return;
    }

    const imgResp = await fetch(item.url);
    const blob = await imgResp.blob();
    const base64 = await blobToBase64(blob);
    const locale = chrome.i18n.getUILanguage();
    const memeInfo = await classifyWith(provider, base64, blob.type || item.mime, locale, settings);

    const filename = memeInfo.isMeme
      ? `${prefix}${sanitizeFilename(memeInfo.filenameSlug)}.${ext}`
      : fallbackFilename;

    await startDownload({ url: item.url, filename, saveAs });
    await recordStats(memeInfo, blob, provider);
  } catch (err) {
    console.error("Auto-rename failed:", err);
    await startDownload({ url: item.url, filename: fallbackFilename, saveAs });
  }
});
