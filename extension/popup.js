// The Daemon worker is capped server-side; not user-editable.
const WORKER_RATE_LIMIT = 5;

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
  lastPreview: null, // { imageDataUrl, filenameSlug, isMeme, timestamp,
                     //   provider, mode, sourceUrl, mimeType, phash, cacheHit }
  gatewayUrl: ""
};

function maskKey(key) {
  if (!key) return null;
  return "••••••••" + key.slice(-4);
}

function currentUsage(usage, windowMs = 60000) {
  const now = Date.now();
  if (!usage || now - usage.windowStart > windowMs) return 0;
  return usage.count;
}

function render(settings) {
  const provider = settings.apiProvider;
  const key = settings.apiKeys[provider];
  const limit = provider === "worker" ? WORKER_RATE_LIMIT : settings.rateLimits[provider];
  const used = currentUsage(settings.rateLimitUsage[provider]);

  document.getElementById("providerChip").textContent = provider === "worker" ? "daemon" : provider;

  const keyEl = document.getElementById("keyValue");
  keyEl.classList.remove("unset", "server");
  if (provider === "worker") {
    keyEl.textContent = "daemon server · no key needed";
    keyEl.classList.add("server");
  } else if (provider === "daemon2") {
    keyEl.textContent = "self-hosted gateway · no key needed";
    keyEl.classList.add("server");
  } else if (key) {
    keyEl.textContent = maskKey(key);
  } else {
    keyEl.textContent = "no key set";
    keyEl.classList.add("unset");
  }

  const bar = document.getElementById("rateBar");
  if (limit && limit > 0) {
    document.getElementById("rateUsed").textContent = used;
    document.getElementById("rateLimit").textContent = `/${limit} per min`;
    const pct = Math.min((used / limit) * 100, 100);
    bar.style.width = `${pct}%`;
    bar.classList.toggle("warn", pct >= 80);
  } else {
    document.getElementById("rateUsed").textContent = "";
    document.getElementById("rateLimit").textContent = "no limit";
    bar.style.width = "0%";
    bar.classList.remove("warn");
  }

  document.getElementById("totalCount").textContent = settings.stats.totalDownloaded;
  document.getElementById("quotaUsed").textContent = settings.stats.totalClassified;

  const thumb = document.getElementById("previewThumb");
  const info = document.getElementById("previewInfo");

  if (settings.lastPreview) {
    thumb.src = settings.lastPreview.imageDataUrl;
    thumb.style.display = "block";
    info.innerHTML = `
      <span class="preview-name">${settings.lastPreview.filenameSlug}</span>
      <span class="preview-tag">${settings.lastPreview.isMeme ? "classified as meme" : "not a meme"}</span>
    `;
    // "Rename last" needs the original image source URL to re-download from;
    // records saved before this feature existed (or from direct-call providers
    // without gateway metadata) still work — the correction just runs
    // scenario B (fresh call) without cache bookkeeping.
    renameLastBtn.disabled = !settings.lastPreview.sourceUrl;
  } else {
    thumb.style.display = "none";
    info.innerHTML = `<span class="preview-empty">No memes classified yet</span>`;
    renameLastBtn.disabled = true;
  }
}

let renameLastBtn;

function load() {
  chrome.storage.local.get(DEFAULTS, render);
}

function setRenameStatus(text, cls) {
  const el = document.getElementById("renameLastStatus");
  el.textContent = text;
  el.className = `rename-last-status ${cls}`;
}

async function onRenameLast() {
  renameLastBtn.disabled = true;
  setRenameStatus("correcting…", "");
  try {
    const resp = await chrome.runtime.sendMessage({ type: "rename-last" });
    if (resp?.ok) {
      setRenameStatus(`saved as "${resp.filenameSlug}"`, "ok");
    } else {
      setRenameStatus(`failed: ${resp?.error || "unknown error"}`, "err");
    }
  } catch (err) {
    setRenameStatus(`failed: ${err?.message || err}`, "err");
  } finally {
    load(); // re-render from updated lastPreview (chains onto the new attempt)
  }
}

document.addEventListener("DOMContentLoaded", () => {
  renameLastBtn = document.getElementById("renameLastBtn");
  renameLastBtn.addEventListener("click", onRenameLast);
  load();

  document.getElementById("openOptions").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });

  // live refresh if background.js updates stats while popup is open
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local") load();
  });
});
