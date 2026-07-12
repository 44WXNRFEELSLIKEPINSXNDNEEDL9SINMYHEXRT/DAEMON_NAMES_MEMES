const DEFAULTS = {
  apiProvider: "google",
  apiKeys: { google: "", claude: "", openai: "" },
  rateLimits: { google: 60, claude: 20, openai: 20 },
  namingPrefix: "",
  dateFormat: "system"
};

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

function setDefaultProvider(providerValue) {
  document.querySelectorAll(".provider").forEach(el => {
    el.classList.toggle("is-default", el.dataset.provider === providerValue);
  });
}

function updateDatePreview() {
  const format = document.getElementById("dateFormat").value;
  document.getElementById("datePreview").textContent = formatDate(new Date(), format);
}

function load() {
  chrome.storage.local.get(DEFAULTS, (settings) => {
    document.querySelector(`input[name="apiProvider"][value="${settings.apiProvider}"]`).checked = true;
    setDefaultProvider(settings.apiProvider);

    ["google", "claude", "openai"].forEach(p => {
      document.getElementById(`key-${p}`).value = settings.apiKeys[p] || "";
      document.getElementById(`rate-${p}`).value = settings.rateLimits[p];
    });

    document.getElementById("prefix").value = settings.namingPrefix || "";
    document.getElementById("dateFormat").value = settings.dateFormat || "system";
    updateDatePreview();
  });
}

function save() {
  const apiProvider = document.querySelector('input[name="apiProvider"]:checked').value;

  const settings = {
    apiProvider,
    apiKeys: {
      google: document.getElementById("key-google").value.trim(),
      claude: document.getElementById("key-claude").value.trim(),
      openai: document.getElementById("key-openai").value.trim()
    },
    rateLimits: {
      google: Number(document.getElementById("rate-google").value) || DEFAULTS.rateLimits.google,
      claude: Number(document.getElementById("rate-claude").value) || DEFAULTS.rateLimits.claude,
      openai: Number(document.getElementById("rate-openai").value) || DEFAULTS.rateLimits.openai
    },
    namingPrefix: document.getElementById("prefix").value.trim(),
    dateFormat: document.getElementById("dateFormat").value
  };

  chrome.storage.local.set(settings, () => {
    const toast = document.getElementById("toast");
    toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), 1800);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  load();

  document.querySelectorAll('input[name="apiProvider"]').forEach(radio => {
    radio.addEventListener("change", (e) => setDefaultProvider(e.target.value));
  });

  document.querySelectorAll(".toggle-visibility").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset.target);
      const isHidden = target.type === "password";
      target.type = isHidden ? "text" : "password";
      btn.textContent = isHidden ? "hide" : "show";
    });
  });

  document.getElementById("dateFormat").addEventListener("change", updateDatePreview);
  document.getElementById("saveBtn").addEventListener("click", save);
});
