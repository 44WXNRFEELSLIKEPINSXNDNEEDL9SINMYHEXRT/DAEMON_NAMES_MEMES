const DEFAULTS = {
  apiProvider: "google",
  apiKeys: { google: "", claude: "", openai: "" },
  rateLimits: { google: 60, claude: 20, openai: 20 },
  rateLimitUsage: {
    google: { windowStart: 0, count: 0 },
    claude: { windowStart: 0, count: 0 },
    openai: { windowStart: 0, count: 0 }
  },
  stats: { totalDownloaded: 0 },
  lastPreview: null // { imageDataUrl, filenameSlug, isMeme, timestamp }
};

function maskKey(key) {
  if (!key) return null;
  if (key.length <= 4) return "•".repeat(key.length);
  return "•".repeat(Math.max(key.length - 4, 4)) + key.slice(-4);
}

function currentUsage(usage, windowMs = 60000) {
  const now = Date.now();
  if (!usage || now - usage.windowStart > windowMs) return 0;
  return usage.count;
}

function render(settings) {
  const provider = settings.apiProvider;
  const key = settings.apiKeys[provider];
  const limit = settings.rateLimits[provider];
  const used = currentUsage(settings.rateLimitUsage[provider]);

  document.getElementById("providerChip").textContent = provider;

  const keyEl = document.getElementById("keyValue");
  if (key) {
    keyEl.textContent = maskKey(key);
    keyEl.classList.remove("unset");
  } else {
    keyEl.textContent = "no key set";
    keyEl.classList.add("unset");
  }

  document.getElementById("rateUsed").textContent = used;
  document.getElementById("rateLimit").textContent = `/${limit} per min`;

  const pct = Math.min((used / limit) * 100, 100);
  const bar = document.getElementById("rateBar");
  bar.style.width = `${pct}%`;
  bar.classList.toggle("warn", pct >= 80);

  document.getElementById("totalCount").textContent = settings.stats.totalDownloaded;

  const thumb = document.getElementById("previewThumb");
  const info = document.getElementById("previewInfo");

  if (settings.lastPreview) {
    thumb.src = settings.lastPreview.imageDataUrl;
    thumb.style.display = "block";
    info.innerHTML = `
      <span class="preview-name">${settings.lastPreview.filenameSlug}</span>
      <span class="preview-tag">${settings.lastPreview.isMeme ? "classified as meme" : "not a meme"}</span>
    `;
  } else {
    thumb.style.display = "none";
    info.innerHTML = `<span class="preview-empty">No memes classified yet</span>`;
  }
}

function load() {
  chrome.storage.local.get(DEFAULTS, render);
}

document.addEventListener("DOMContentLoaded", () => {
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
