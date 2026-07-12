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
  namingPrefix: "",
  dateFormat: "system"
};

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "download-as-meme",
    title: chrome.i18n.getMessage("contextMenuTitle"),
    contexts: ["image"]
  });
});

async function recordStats(memeInfo, imageBlob, provider) {
  const { stats, rateLimitUsage } = await chrome.storage.local.get(["stats", "rateLimitUsage"]);

  const newStats = {
    totalDownloaded: (stats?.totalDownloaded || 0) + (memeInfo.isMeme ? 1 : 0)
  };

  const now = Date.now();
  const usage = rateLimitUsage?.[provider] || { windowStart: 0, count: 0 };
  const withinWindow = now - usage.windowStart < 60000;
  const newUsage = {
    ...rateLimitUsage,
    [provider]: withinWindow
      ? { windowStart: usage.windowStart, count: usage.count + 1 }
      : { windowStart: now, count: 1 }
  };

  const imageDataUrl = await blobToBase64(imageBlob);

  chrome.storage.local.set({
    stats: newStats,
    rateLimitUsage: newUsage,
    lastPreview: {
      imageDataUrl: `data:${imageBlob.type};base64,${imageDataUrl}`,
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
    const provider = settings.apiProvider;

    const imgResp = await fetch(info.srcUrl);
    const blob = await imgResp.blob();
    const base64 = await blobToBase64(blob);
    const locale = chrome.i18n.getUILanguage();

    const classifyResp = await fetch("https://daemon-meme.windown52358.workers.dev", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: base64, mimeType: blob.type, locale })
    });
    const memeInfo = await classifyResp.json();

    const ext = blob.type.split("/")[1] || "jpg";
    const prefix = settings.namingPrefix ? `${settings.namingPrefix}_` : "";
    const filename = memeInfo.isMeme
      ? `${prefix}${memeInfo.filenameSlug}.${ext}`
      : `${prefix}${Date.now()}.${ext}`;

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