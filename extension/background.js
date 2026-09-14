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
  saveMethod: "direct",
  // Base URL of a server/ gateway. Empty = use the project's shared gateway
  // (OWNER_GATEWAY_URL in background.js); set it to override with your own.
  // daemon2 falls back to http://localhost:8090 when both are empty (dev).
  gatewayUrl: ""
};

const WORKER_URL = "https://daemon-meme.windown52358.workers.dev";

// The project's own server/ gateway deployment. ALL provider traffic routes
// through it by default, so every user shares one Redis phash cache and one
// SQLite metrics DB (that shared dataset is the point — see root README
// "Shared gateway"). Users can point the extension at their own gateway via
// Settings → Gateway URL; that override wins over this default.
// OWNER: fill this in once server/ is deployed somewhere (any Docker host —
// see server/README.md). While it's empty, the extension falls back to the
// legacy direct-call paths so nothing breaks before deployment.
const OWNER_GATEWAY_URL = "";

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

// Daemon2 (server/) is reached exclusively through classifyViaGateway() —
// mode "manual" for the explicit "save as meme" click (isMeme assumed true
// server-side), "auto" for the all-downloads flow. The gateway URL comes
// from settings (options page); defaults to localhost for development.
// Responses carry _phash/_cacheHit metadata for the "Rename last" flow.

// --- Shared gateway routing (all providers) ---------------------------------
// When a Gateway URL is configured (options page), EVERY provider's traffic
// goes through server/: shared Redis phash cache + shared SQLite metrics log
// for all of them, Redis rate limiting for the keyless ones (worker/daemon2).
// BYO-key providers still use the user's own key — the gateway just proxies
// the call, so cost stays the user's. With no gateway URL set, the legacy
// direct-call paths below are used unchanged (zero-config default).
const DAEMON2_DEFAULT_URL = "http://localhost:8090";

// Gateway URL resolution priority:
//   1. user-set Gateway URL (options page) — explicit override wins
//   2. OWNER_GATEWAY_URL — the project's own shared gateway (default once deployed)
//   3. null — no gateway: legacy direct calls; daemon2 alone still works
//      against localhost for development (DAEMON2_DEFAULT_URL).
function resolveGatewayUrl(settings) {
  const url = (settings.gatewayUrl || OWNER_GATEWAY_URL || "").trim();
  return url ? url.replace(/\/+$/, "") : null;
}

async function classifyViaGateway(gatewayUrl, provider, base64, mimeType, locale, settings, mode) {
  const body = { image: base64, mimeType, locale, mode, provider };
  const apiKey = settings.apiKeys?.[provider];
  if (apiKey) body.apiKey = apiKey;

  const resp = await fetch(`${gatewayUrl}/classify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const cacheHit = resp.headers.get("x-cache") === "HIT";
  const phash = resp.headers.get("x-phash");
  if (resp.status === 429) {
    const rb = await resp.json().catch(() => ({}));
    throw new Error(`Gateway rate limited, retry in ${rb.retry_after_seconds ?? "?"}s`);
  }
  if (!resp.ok) throw new Error(`Gateway error: ${resp.status}`);
  const memeInfo = await resp.json();
  return { ...memeInfo, _cacheHit: cacheHit, _phash: phash };
}

// "Rename last" correction request against the gateway. Forces a fresh
// classification pass; the gateway decides how to handle the cache
// depending on whether the flagged result was itself a cache hit.
// Works for every provider the gateway knows (daemon2, worker, BYO-key).
async function correctViaGateway(gatewayUrl, { base64, mimeType, locale, mode, provider, apiKey, phash, cacheHit, previousSlug }) {
  const body = {
    image: base64, mimeType, locale, mode, provider,
    phash: phash || "", cache_hit: Boolean(cacheHit), previous_slug: previousSlug
  };
  if (apiKey) body.apiKey = apiKey;

  const resp = await fetch(`${gatewayUrl}/correct`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (resp.status === 429) {
    const rb = await resp.json().catch(() => ({}));
    throw new Error(`Gateway rate limited, retry in ${rb.retry_after_seconds ?? "?"}s`);
  }
  if (!resp.ok) throw new Error(`Gateway correction error: ${resp.status}`);
  return await resp.json();
}

// Which engine actually runs: the selected one if usable, otherwise the Daemon worker.
function resolveProvider(settings) {
  const provider = settings.apiProvider;
  if (provider === "worker") return "worker";
  // daemon2 has no BYO key concept and is never auto-selected as a fallback —
  // it's only used if the user has explicitly chosen it (and it's reachable;
  // see options.js, it's rendered as unavailable/unselectable by default).
  if (provider === "daemon2") return "daemon2";
  if (settings.apiKeys?.[provider]) return provider;
  return "worker";
}

async function classifyWith(provider, base64, mimeType, locale, settings, mode = "auto") {
  const apiKey = settings.apiKeys?.[provider];

  // Gateway routing: by default ALL providers go through the project's own
  // shared gateway (OWNER_GATEWAY_URL), so every user shares one Redis phash
  // cache + one SQLite metrics DB. A user-set Gateway URL overrides it.
  // daemon2 always uses a gateway (it IS the gateway's pipeline); if neither
  // URL is set it falls back to localhost for development.
  const gatewayUrl = resolveGatewayUrl(settings)
    || (provider === "daemon2" ? DAEMON2_DEFAULT_URL : null);
  if (gatewayUrl) {
    return classifyViaGateway(gatewayUrl, provider, base64, mimeType, locale, settings, mode);
  }

  // Legacy direct-call paths (no gateway anywhere — pre-deployment fallback).
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

async function recordStats(memeInfo, imageBlob, provider, sourceUrl, mode) {
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
    // "Rename last" (popup) needs enough state to re-run classification on
    // the SAME original image and correctly tell the gateway whether the
    // flagged result came from its cache. Extension has no filesystem
    // rename capability — a correction re-downloads a fresh copy under the
    // new name, it never touches the previously saved file (see popup.html
    // note + README "Rename last correction flow").
    lastPreview: {
      imageDataUrl: `data:${imageBlob.type};base64,${await blobToBase64(imageBlob)}`,
      filenameSlug: memeInfo.filenameSlug,
      isMeme: memeInfo.isMeme,
      timestamp: now,
      provider,
      mode: mode || "auto",
      sourceUrl: sourceUrl || null,
      mimeType: imageBlob.type,
      phash: memeInfo._phash || null,
      cacheHit: Boolean(memeInfo._cacheHit)
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
    // Explicit "save as meme" click = manual mode (daemon2 assumes isMeme
    // server-side; other providers ignore the mode field).
    const memeInfo = await classifyWith(provider, base64, blob.type, locale, settings, "manual");

    const filename = memeInfo.isMeme
      ? `${prefix}${sanitizeFilename(memeInfo.filenameSlug)}.${ext}`
      : fallbackFilename;

    await startDownload({ url: info.srcUrl, filename, saveAs });
    await recordStats(memeInfo, blob, provider, info.srcUrl, "manual");
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
    // Unattended downloads-API callback = auto mode (the model decides isMeme).
    const memeInfo = await classifyWith(provider, base64, blob.type || item.mime, locale, settings, "auto");

    const filename = memeInfo.isMeme
      ? `${prefix}${sanitizeFilename(memeInfo.filenameSlug)}.${ext}`
      : fallbackFilename;

    await startDownload({ url: item.url, filename, saveAs });
    await recordStats(memeInfo, blob, provider, item.url, "auto");
  } catch (err) {
    console.error("Auto-rename failed:", err);
    await startDownload({ url: item.url, filename: fallbackFilename, saveAs });
  }
});

// ---------------------------------------------------------------------------
// "Rename last" — correction flow, triggered from the popup.
//
// IMPORTANT LIMITATION: Chrome extensions cannot rename files on disk after
// download. What this does is trigger a FRESH classification (with the
// rejected slug injected as a negative example, and/or the shared cache
// entry corrected server-side) and re-download the SAME image under the new
// name. The previously downloaded file stays on disk — the popup tells the
// user they may want to delete it.
//
// The flow is chainable: after a correction, lastPreview is updated with the
// new result, so a second click corrects the latest attempt, not the original.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "rename-last") return false;

  (async () => {
    try {
      const settings = await chrome.storage.local.get(DEFAULTS);
      const last = settings.lastPreview;
      if (!last?.sourceUrl || !last.imageDataUrl) {
        sendResponse({ ok: false, error: "no_last_classification" });
        return;
      }

      // Corrections REQUIRE a gateway — the correction semantics (cache
      // overwrite vs negative-example re-roll) live in server/. Falls back
      // to the localhost dev gateway if none is configured; if that's not
      // running either, correctViaGateway throws and the error surfaces in
      // the popup status line.
      const gatewayUrl = resolveGatewayUrl(settings) || DAEMON2_DEFAULT_URL;
      const base64 = last.imageDataUrl.split(",")[1];
      const locale = chrome.i18n.getUILanguage();

      // Corrections always go through the gateway — it owns the cache entry
      // and the correction semantics (scenario A vs B). Works for any
      // provider the gateway knows; BYO keys are forwarded so cost stays
      // on the user's own account.
      const corrected = await correctViaGateway(gatewayUrl, {
        base64,
        mimeType: last.mimeType || "image/png",
        locale,
        mode: last.mode || "manual",
        provider: last.provider || "daemon2",
        apiKey: settings.apiKeys?.[last.provider] || null,
        phash: last.phash || "",
        cacheHit: Boolean(last.cacheHit),
        previousSlug: last.filenameSlug || ""
      });

      if (!corrected || corrected.error || !corrected.filenameSlug) {
        sendResponse({ ok: false, error: corrected?.error || "correction_failed" });
        return;
      }

      // Re-download the same source image under the corrected name.
      const prefix = settings.namingPrefix ? `${settings.namingPrefix}_` : "";
      const ext = (last.mimeType || "image/png").split("/")[1] || "jpg";
      const filename = `${prefix}${sanitizeFilename(corrected.filenameSlug)}.${ext}`;
      const saveAs = settings.saveMethod === "saveAs";
      await startDownload({ url: last.sourceUrl, filename, saveAs });

      // Update lastPreview to the corrected result so repeated clicks chain
      // onto the newest attempt. cacheHit=false: this fresh pass is not
      // served from cache, so a further correction is scenario B.
      await chrome.storage.local.set({
        lastPreview: {
          ...last,
          filenameSlug: corrected.filenameSlug,
          isMeme: corrected.isMeme,
          timestamp: Date.now(),
          cacheHit: false
        }
      });

      sendResponse({ ok: true, filenameSlug: corrected.filenameSlug, isMeme: corrected.isMeme });
    } catch (err) {
      console.error("Rename last failed:", err);
      sendResponse({ ok: false, error: String(err?.message || err) });
    }
  })();

  return true; // keep the message channel open for the async sendResponse
});
