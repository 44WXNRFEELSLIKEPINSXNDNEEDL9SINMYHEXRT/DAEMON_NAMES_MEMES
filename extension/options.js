// Providers with a `fixedLimit` are locked: no API key, and the rate is enforced
// server-side so it can't be changed here.
const PROVIDERS = [
  { id: "worker",     name: "DAEMON",     note: "Shared server · no key needed", fixedLimit: 5 },
  { id: "daemon2",    name: "DAEMON2",    note: "Qwen3-VL + OCR", unavailable: true },
  { id: "google",     name: "Google",     note: "Gemini vision" },
  { id: "claude",     name: "Anthropic",  note: "Claude vision" },
  { id: "openai",     name: "OpenAI",     note: "GPT vision" },
  { id: "openrouter", name: "OpenRouter", note: "Any model · one key" },
  { id: "groq",       name: "Groq",       note: "Llama vision · fast" },
  { id: "mistral",    name: "Mistral",    note: "Pixtral vision" },
  { id: "xai",        name: "xAI",        note: "Grok vision" }
];

const DEFAULTS = {
  apiProvider: "worker",
  apiKeys: { google: "", claude: "", openai: "", openrouter: "", groq: "", mistral: "", xai: "" },
  rateLimits: { google: 0, claude: 0, openai: 0, openrouter: 0, groq: 0, mistral: 0, xai: 0 },
  namingPrefix: "",
  dateFormat: "system",
  downloadMode: "context",
  saveMethod: "direct",
  gatewayUrl: ""
};

// "1.2.3.4:8090" -> "http://1.2.3.4:8090": fetch() needs a scheme.
function normalizeGatewayUrl(value) {
  const url = (value || "").trim().replace(/\/+$/, "");
  if (!url) return "";
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `http://${url}`;
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

function updateDatePreview() {
  const format = document.getElementById("dateFormat").value;
  document.getElementById("datePreview").textContent = formatDate(new Date(), format);
}

function setActiveProvider(providerValue) {
  document.querySelectorAll(".provider").forEach(el => {
    el.classList.toggle("is-active", el.dataset.provider === providerValue);
  });
}

function buildProviderCards() {
  const grid = document.getElementById("providerGrid");

  grid.innerHTML = PROVIDERS.map(p => {
    const locked = p.fixedLimit != null;
    const unavailable = p.unavailable === true;
    const note = locked ? `${p.note} · capped ${p.fixedLimit}/min` : p.note;

    if (unavailable) {
      // No radio, no key/rate fields — purely informational, cannot be selected.
      return `
        <div class="provider is-unavailable span-2" data-provider="${p.id}" aria-disabled="true">
          <div class="provider-row">
            <span class="provider-label provider-label-disabled">
              <input type="radio" disabled>
              <span class="provider-name">${p.name}</span>
            </span>
            <span class="provider-note">${note}</span>
            <span class="unavailable-tag">Unavailable</span>
          </div>
          <div class="provider-fields provider-fields-static">
            <p class="unavailable-hint">
              Two-stage OCR + Qwen3-VL-4B classifier — see <code>service/</code>. No free-tier
              CPU host fits the ≥4&nbsp;GB RAM footprint, so this provider isn't reachable yet.
              Cold start isn't the blocker here — it can't be launched anywhere free at all
              right now. Tracked for a future release once it's actually hosted somewhere.
            </p>
          </div>
        </div>`;
    }

    const spoiler = locked
      ? ""
      : `<button type="button" class="spoiler-toggle" aria-expanded="false" aria-label="Toggle ${p.name} settings"><span class="chevron">▸</span></button>`;

    const fields = locked
      ? ""
      : `<div class="provider-fields">
           <div class="key-field">
             <input type="password" class="key-input" id="key-${p.id}" placeholder="API key" autocomplete="off">
             <button type="button" class="toggle-visibility" data-target="key-${p.id}">show</button>
           </div>
           <div class="rate-row">
             <input type="number" class="rate-input" id="rate-${p.id}" min="0" step="1" placeholder="no limit">
             <span class="rate-unit">/ min</span>
           </div>
         </div>`;

    return `
      <div class="provider${locked ? " span-2" : ""}" data-provider="${p.id}">
        <div class="provider-row">
          <label class="provider-label">
            <input type="radio" name="apiProvider" value="${p.id}">
            <span class="provider-name">${p.name}</span>
          </label>
          <span class="provider-note">${note}</span>
          <span class="active-tag">Active</span>
          ${spoiler}
        </div>
        ${fields}
      </div>`;
  }).join("");
}

function load() {
  chrome.storage.local.get(DEFAULTS, (settings) => {
    const active = document.querySelector(`input[name="apiProvider"][value="${settings.apiProvider}"]`);
    if (active) active.checked = true;
    setActiveProvider(settings.apiProvider);

    PROVIDERS.forEach(p => {
      if (p.fixedLimit != null || p.unavailable) return; // locked or unavailable; no inputs
      document.getElementById(`key-${p.id}`).value = settings.apiKeys[p.id] || "";
      const rate = settings.rateLimits[p.id] ?? 0;
      document.getElementById(`rate-${p.id}`).value = rate > 0 ? rate : ""; // blank shows "no limit"
    });

    document.getElementById("prefix").value = settings.namingPrefix || "";
    document.getElementById("dateFormat").value = settings.dateFormat || "system";
    document.getElementById("downloadMode").value = settings.downloadMode || "context";
    document.getElementById("saveMethod").value = settings.saveMethod || "direct";
    document.getElementById("gatewayUrl").value = normalizeGatewayUrl(settings.gatewayUrl);
    updateDatePreview();
  });
}

function save() {
  const apiProvider = document.querySelector('input[name="apiProvider"]:checked').value;

  const apiKeys = {};
  const rateLimits = {};
  PROVIDERS.forEach(p => {
    if (p.fixedLimit != null || p.unavailable) return; // locked or unavailable; server-enforced / no inputs
    apiKeys[p.id] = document.getElementById(`key-${p.id}`).value.trim();
    rateLimits[p.id] = Number(document.getElementById(`rate-${p.id}`).value) || 0;
  });

  const settings = {
    apiProvider,
    apiKeys,
    rateLimits,
    namingPrefix: document.getElementById("prefix").value.trim(),
    dateFormat: document.getElementById("dateFormat").value,
    downloadMode: document.getElementById("downloadMode").value,
    saveMethod: document.getElementById("saveMethod").value,
    gatewayUrl: normalizeGatewayUrl(document.getElementById("gatewayUrl").value)
  };

  chrome.storage.local.set(settings, () => {
    const toast = document.getElementById("toast");
    toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), 1800);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  buildProviderCards();
  load();

  document.querySelectorAll('input[name="apiProvider"]').forEach(radio => {
    radio.addEventListener("change", (e) => setActiveProvider(e.target.value));
  });

  // Clicking anywhere on a card selects that provider (except when using its inputs).
  document.querySelectorAll(".provider").forEach(card => {
    if (card.classList.contains("is-unavailable")) return; // not selectable
    card.addEventListener("click", (e) => {
      if (e.target.closest("input, button, select, summary")) return;
      const radio = card.querySelector('input[name="apiProvider"]');
      radio.checked = true;
      setActiveProvider(card.dataset.provider);
    });
  });

  // Expand/collapse a provider's settings via its chevron (doesn't change selection).
  document.querySelectorAll(".spoiler-toggle").forEach(btn => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".provider");
      const opening = !card.classList.contains("open");
      card.classList.toggle("open", opening);
      btn.setAttribute("aria-expanded", String(opening));
    });
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
