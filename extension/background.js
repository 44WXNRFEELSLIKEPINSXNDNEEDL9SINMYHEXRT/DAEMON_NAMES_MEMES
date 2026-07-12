chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "download-as-meme",
    title: "Download as meme",
    contexts: ["image"]
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "download-as-meme") return;

  try {
    const imgResp = await fetch(info.srcUrl);
    const blob = await imgResp.blob();
    const base64 = await blobToBase64(blob);
    const locale = chrome.i18n.getUILanguage();

    const classifyResp = await fetch("https://daemon-meme.windown52358.workers.dev", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: base64, mimeType: blob.type, locale })
    });
    const { isMeme, filenameSlug } = await classifyResp.json();

    const ext = blob.type.split("/")[1] || "jpg";
    const filename = isMeme
      ? `${filenameSlug}.${ext}`
      : `${Date.now()}.${ext}`;

    chrome.downloads.download({ url: info.srcUrl, filename });
  } catch (err) {
    // fall back to a normal download so the click never feels broken
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