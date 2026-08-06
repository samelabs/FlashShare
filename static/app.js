const account = document.getElementById("account");
const form = document.getElementById("uploadForm");
const file = document.getElementById("file");
const fileLabel = document.getElementById("fileLabel");
const result = document.getElementById("result");
const submitBtn = document.getElementById("submitBtn");
const historyList = document.getElementById("historyList");
const dropZone = document.getElementById("dropZone");
const uploadModal = document.getElementById("uploadModal");
const modalClose = document.getElementById("modalClose");
const modalTitle = document.getElementById("modalTitle");
const modalMeta = document.getElementById("modalMeta");
const modalUrl = document.getElementById("modalUrl");
const modalCopy = document.getElementById("modalCopy");
const modalOpen = document.getElementById("modalOpen");
const modalShare = document.getElementById("modalShare");
const modalQr = document.getElementById("modalQr");
const modalSaveQr = document.getElementById("modalSaveQr");
const modalDelete = document.getElementById("modalDelete");
const langButton = document.getElementById("langButton");
const langMenu = document.getElementById("langMenu");
const homeShare = document.getElementById("homeShare");
const limits = window.__LIMITS__ || {};
let currentUploads = [];
let activeUpload = null;

const EXT_TO_TYPE = {
  // image
  jpg:"image",jpeg:"image",png:"image",gif:"image",webp:"image",bmp:"image",ico:"image",
  heic:"image",heif:"image",tiff:"image",tif:"image",avif:"image",svg:"image",
  // pdf
  pdf:"pdf",
  // document
  doc:"doc",docx:"doc",rtf:"doc",odt:"doc",
  // spreadsheet
  xls:"sheet",xlsx:"sheet",ods:"sheet",
  // presentation
  ppt:"slides",pptx:"slides",odp:"slides",
  // text/code
  txt:"text",csv:"text",md:"text",json:"text",xml:"text",yaml:"text",yml:"text",
  py:"text",js:"text",mjs:"text",ts:"text",html:"text",htm:"text",css:"text",
  go:"text",rs:"text",java:"text",c:"text",cpp:"text",h:"text",rb:"text",php:"text",sh:"text",sql:"text",
  // archive
  zip:"archive",rar:"archive","7z":"archive",gz:"archive",tar:"archive",bz2:"archive",xz:"archive",tgz:"archive",
};
const FORMAT_SVG = {};
function fmtIcon(name) {
  const ext = (name||"").toLowerCase().match(/\.(\w+)$/);
  if (!ext) return "";
  const type = EXT_TO_TYPE[ext[1]];
  const svgPath = (type && window.__ICONS__[type]) || window.__ICONS__.doc || window.__ICONS__.text || "";
  const badge = type || "file";
  return '<span class="fmt-badge fmt-'+badge+'" aria-hidden="true"><svg viewBox="0 0 24 24">'+svgPath+'</svg></span>';
}

const _IC = window.__ICONS__ || {};
function svgIcon(name) {
  const path = _IC[name] || _IC.info || "";
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}
const ICONS = {
  upload: `<span class="button-icon" aria-hidden="true">${svgIcon("upload")}</span>`,
  open: svgIcon("open"),
  x: `<span class="x-glyph" aria-hidden="true">𝕏</span>`,
  copy: svgIcon("copy"),
  download: svgIcon("download"),
  qr: svgIcon("qr"),
  logout: svgIcon("logout"),
  link: svgIcon("link"),
  check: svgIcon("check"),
  share: svgIcon("share"),
  trash: svgIcon("trash"),
  settings: svgIcon("settings"),
};

const I18N = window.__I18N__ || { en: {} };
const SUPPORTED_LANGS = ["en", "zh", "ja", "ko", "es", "ar", "de", "ru", "pt", "fr"];
const LOCALE_MAP = { en:"en", zh:"zh-CN", ja:"ja-JP", ko:"ko-KR", es:"es-ES", ar:"ar", de:"de-DE", ru:"ru-RU", pt:"pt-BR", fr:"fr-FR" };
const LANG_LABELS = { en:"English", zh:"简体中文", ja:"日本語", ko:"한국어", es:"Español", ar:"العربية", de:"Deutsch", ru:"Русский", pt:"Português", fr:"Français" };
const CLIENT_META = {
  en: { title: "Flash Share — Free File Tools & Sharing", desc: "Compress, clean, beautify, and share files instantly." },
  zh: { title: "Flash Share — 免费文件工具与分享", desc: "压缩、净化、美化、分享，一步到位。" },
  ja: { title: "Flash Share — 無料ファイルツール＆共有", desc: "圧縮・浄化・美化・共有を一瞬で。" },
  ko: { title: "Flash Share — 무료 파일 도구 & 공유", desc: "압축, 정화, 미화, 공유를 한 번에." },
  es: { title: "Flash Share — Herramientas y compartir gratis", desc: "Comprime, limpia, embellece y comparte al instante." },
  ar: { title: "Flash Share — أدوات ومشاركة مجانية", desc: "ضغط، تنقية، تحسين، ومشاركة فورية." },
  de: { title: "Flash Share — Kostenlose Tools & Sharing", desc: "Komprimieren, bereinigen, verschönern und sofort teilen." },
  ru: { title: "Flash Share — Бесплатные инструменты", desc: "Сжимай, очищай, улучшай и делись мгновенно." },
  pt: { title: "Flash Share — Ferramentas e compartilhar grátis", desc: "Comprima, limpe, embeleze e compartilhe instantaneamente." },
  fr: { title: "Flash Share — Outils gratuits & partage", desc: "Compressez, nettoyez, embellissez et partagez instantanément." },
};
function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]+)"));
  return m ? m[1] : "";
}
function detectLang() {
  const params = new URLSearchParams(location.search);
  const requested = params.get("lang") || params.get("x");
  const requestedCode = String(requested || "").toLowerCase().split("-")[0];
  if (SUPPORTED_LANGS.includes(requestedCode)) return requestedCode;
  const cookieLang = getCookie("flashshare_lang");
  if (SUPPORTED_LANGS.includes(cookieLang)) return cookieLang;
  const saved = localStorage.getItem("flashshare_lang");
  if (SUPPORTED_LANGS.includes(saved)) return saved;
  const languages = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || "en"];
  for (const item of languages) {
    const code = String(item || "").toLowerCase().split("-")[0];
    if (SUPPORTED_LANGS.includes(code)) return code;
  }
  return "en";
}
let lang = detectLang();

function t(key, vars = {}) {
  let value = (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
  for (const [name, replacement] of Object.entries(vars)) value = value.replaceAll(`{${name}}`, replacement);
  return value;
}

function fmtSize(n) {
  if (!n) return "0 B";
  const units = ["B","KB","MB","GB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}
function formatSpeed(bytes, seconds) {
  if (!bytes || !seconds) return "";
  const bps = bytes / seconds;
  if (bps >= 1048576) return (bps / 1048576).toFixed(1) + " MB/s";
  if (bps >= 1024) return (bps / 1024).toFixed(0) + " KB/s";
  return Math.round(bps) + " B/s";
}
function fmtDate(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString(LOCALE_MAP[lang] || "en", { hour12:false });
}
function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}
function avatarInitial(user) {
  const source = (user.displayName || user.username || "V").trim();
  return escapeHtml(source.slice(0, 1).toUpperCase());
}
function actionLabel(icon, label) {
  return `<span class="action-mark">${ICONS[icon]}</span><span class="action-text">${escapeHtml(label)}</span>`;
}
function visibleAction(icon, label) {
  return `<span class="action-mark">${ICONS[icon]}</span><span>${escapeHtml(label)}</span>`;
}
function renderModalActions() {
  modalOpen.innerHTML = actionLabel("open", t("open"));
  modalOpen.setAttribute("aria-label", t("open"));
  modalOpen.setAttribute("title", t("open"));
  modalShare.innerHTML = actionLabel("x", t("shareX"));
  modalShare.setAttribute("aria-label", t("shareX"));
  modalShare.setAttribute("title", t("shareX"));
  modalSaveQr.innerHTML = actionLabel("qr", t("saveQr"));
  modalSaveQr.setAttribute("aria-label", t("saveQr"));
  modalSaveQr.setAttribute("title", t("saveQr"));
  modalDelete.innerHTML = actionLabel("trash", t("delete"));
  modalDelete.setAttribute("aria-label", t("delete"));
  modalDelete.setAttribute("title", t("delete"));
}
function renderHomeShare() {
  if (!homeShare) return;
  const shareUrl = "https://x.com/intent/tweet?" + new URLSearchParams({ text:t("homeShareText"), url: `${location.origin}/?x=${encodeURIComponent(lang)}` });
  homeShare.innerHTML = ICONS.share;
  homeShare.href = shareUrl;
  homeShare.setAttribute("aria-label", t("shareX"));
  homeShare.setAttribute("title", t("shareX"));
}
function applyI18n() {
  document.documentElement.lang = LOCALE_MAP[lang] || "en";
  document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
  const meta = CLIENT_META[lang] || CLIENT_META.en;
  document.title = t("title");
  setMeta("description", meta.desc);
  setMetaProperty("og:title", meta.title);
  setMetaProperty("og:description", meta.desc);
  setMeta("twitter:title", meta.title);
  setMeta("twitter:description", meta.desc);
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const key = el.getAttribute("data-i18n");
    el.textContent = t(key);
  });
  renderLanguageMenu();
  renderHomeShare();
  document.getElementById("refreshBtn").setAttribute("aria-label", t("refresh"));
  modalCopy.textContent = t("copy");
  renderModalActions();
  if (!file.files[0]) fileLabel.textContent = t("dropEmpty");
  // Refresh tool selection visual state (badge + format tags + description)
  applyToolSelection();
  renderAccount();
  loadHistory();
  if (activeUpload) renderModal(activeUpload);
  // Re-render result card if visible (uses t() for all labels)
  if (lastUploadResult && result && !result.classList.contains("hidden")) {
    showShareResult(lastUploadResult);
  }
}
function renderLanguageMenu() {
  langButton.textContent = LANG_LABELS[lang] || "English";
  langButton.setAttribute("aria-expanded", "false");
  langMenu.innerHTML = SUPPORTED_LANGS.map(code => `<button class="lang-option${code === lang ? " active" : ""}" type="button" role="option" aria-selected="${code === lang}" data-lang="${code}">${LANG_LABELS[code]}</button>`).join("");
}
function setMeta(name, value) {
  const el = document.querySelector(`meta[name="${name}"]`);
  if (el) el.setAttribute("content", value);
}
function setMetaProperty(property, value) {
  const el = document.querySelector(`meta[property="${property}"]`);
  if (el) el.setAttribute("content", value);
}
function renderAccount() {
  if (!window.__USER__) {
    form.classList.add("disabled");
    submitBtn.disabled = true;
    return;
  }
  submitBtn.disabled = false;
}
function showResult(html, isError=false) {
  result.className = "result" + (isError ? " error" : "");
  result.innerHTML = html;
}
file.addEventListener("change", () => {
  if (!window.__USER__) { location.href = "/auth/x/login"; return; }
  fileLabel.textContent = file.files[0] ? t("dropSelected", { name: file.files[0].name, size: fmtSize(file.files[0].size) }) : t("dropEmpty");
});
["dragenter", "dragover"].forEach(name => dropZone.addEventListener(name, event => {
  event.preventDefault();
  dropZone.classList.add("dragover");
}));
["dragleave", "drop"].forEach(name => dropZone.addEventListener(name, event => {
  event.preventDefault();
  dropZone.classList.remove("dragover");
}));
dropZone.addEventListener("drop", event => {
  if (!window.__USER__) { location.href = "/auth/x/login"; return; }
  if (event.dataTransfer.files.length) {
    const dt = new DataTransfer();
    dt.items.add(event.dataTransfer.files[0]);
    file.files = dt.files;
    file.dispatchEvent(new Event("change"));
  }
});
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!window.__USER__) return location.href = "/auth/x/login";
  if (!file.files[0]) return;
  submitBtn.disabled = true;
  submitBtn.innerHTML = `<div class="btn-spinner"></div>`;
  showResult(`<div class="upload-progress"><div class="upload-progress-bar"><div class="upload-progress-fill" id="progressFill"></div></div><div class="upload-progress-text" id="progressText">0%</div></div>`);
  const formData = new FormData(form);
  if (activeTool && activeTool.id !== "share") {
    formData.append("tool", activeTool.id);
    if (activePreset) formData.append("preset", activePreset);
  }
  const fileSize = file.files[0].size;
  const startTime = Date.now();
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/uploads");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.min(99, Math.round(e.loaded / e.total * 100));
      const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
      const speed = e.loaded > 0 && elapsed > 0 ? formatSpeed(e.loaded, parseFloat(elapsed)) : "";
      const fill = document.getElementById("progressFill");
      const text = document.getElementById("progressText");
      if (fill) fill.style.width = pct + "%";
      if (text) text.textContent = (activeTool ? t("uploading") + " · " : "") + pct + "%" + (speed ? " · " + speed : "");
    }
  };
  xhr.onload = async () => {
    const fill = document.getElementById("progressFill");
    if (fill) fill.style.width = "100%";
    try {
      const data = JSON.parse(xhr.responseText);
      if (xhr.status >= 400 || !data.ok) throw new Error(data.error || t("failed"));
      // No tool (or default share tool) → show result directly
      if (!activeTool || activeTool.id === "share") {
        showShareResult(data.file);
        form.reset();
        fileLabel.textContent = t("dropEmpty");
        loadHistory();
        return;
      }
      // Tool active → Step 2: execute tool
      lastUploadResult = data.file;
      const toolName = t(activeTool.id);
      showToolProcessing(toolName);
      const expirySelect = form.querySelector("[name=\"expiryDays\"]");
      const dlSelect = form.querySelector("[name=\"allowedDownloads\"]");
      const toolRes = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tool_id: activeTool.id,
          public_id: data.file.publicId,
          params: {
            preset: activePreset,
            expiry_days: expirySelect ? parseInt(expirySelect.value) : 1,
            allowed_downloads: dlSelect ? parseInt(dlSelect.value) : 50,
          },
        }),
      });
      const toolData = await toolRes.json();
      if (!toolData.ok) {
        showToolError(toolData.error === "auth_required" ? t("signIn") : toolData.error, data.file);
        return;
      }
      lastUploadResult = toolData.result;
      showShareResult(toolData.result);
      form.reset();
      fileLabel.textContent = t("dropEmpty");
      loadHistory();
    } catch (err) {
      showResult(escapeHtml(err.message), true);
    } finally {
      submitBtn.disabled = false;
      submitBtn.innerHTML = `${ICONS.upload}<span>${t("create")}</span>`;
    }
  };
  xhr.onerror = () => {
    showResult(escapeHtml(t("failed")), true);
    submitBtn.disabled = false;
    submitBtn.innerHTML = `${ICONS.upload}<span>${t("create")}</span>`;
  };
  xhr.send(formData);
});
async function loadHistory() {
  if (!window.__USER__) {
    currentUploads = [];
    historyList.innerHTML = "";
    historyList.setAttribute("data-empty", t("noAuthHistory"));
    const hc3 = document.getElementById("historyCount"); if (hc3) hc3.textContent = "";
    return;
  }
  try {
    const res = await fetch("/api/uploads");
    const data = await res.json();
    if (!data.ok || !data.uploads.length) {
      currentUploads = [];
      historyList.innerHTML = "";
      historyList.setAttribute("data-empty", t("noHistory"));
      const hc2 = document.getElementById("historyCount"); if (hc2) hc2.textContent = "";
      return;
    }
    currentUploads = data.uploads;
    const hc = document.getElementById("historyCount");
    if (hc) hc.textContent = data.uploads.length;
    historyList.setAttribute("data-empty", "");
    historyList.innerHTML = data.uploads.map((x, index) => {
      const share = "https://x.com/intent/tweet?" + new URLSearchParams({ text:t("shareText"), url: x.url_download });
      return `<div class="item"><a class="item-link" href="${escapeHtml(x.url_download)}"><span class="item-main"><span class="item-title">${fmtIcon(x.file_name)}<span class="item-name">${escapeHtml(x.file_name)}</span></span><span class="item-sub">${t("itemMeta", { size: fmtSize(x.size_bytes), days: x.expiry_days, date: fmtDate(x.expire_at) })} · ${downloadText(x)}</span></span></a><div class="item-actions"><a class="item-tool" href="${escapeHtml(x.url_download)}?download=1" download="${escapeHtml(x.file_name)}" aria-label="${t("downloadFile")}" title="${t("downloadFile")}">${ICONS.download}</a><button class="item-tool" type="button" data-copy-index="${index}" aria-label="${t("copy")}" title="${t("copy")}">${ICONS.copy}</button><a class="item-tool accent-action" href="${escapeHtml(share)}" target="_blank" rel="noreferrer" aria-label="${t("shareX")}" title="${t("shareX")}">${ICONS.x}</a><button class="item-tool" type="button" data-qr-index="${index}" aria-label="${t("qrCode")}" title="${t("qrCode")}">${ICONS.qr}</button><button class="item-tool danger-action" type="button" data-delete-index="${index}" aria-label="${t("delete")}" title="${t("delete")}">${ICONS.trash}</button></div></div>`;
    }).join("");
    historyList.querySelectorAll("[data-copy-index]").forEach(button => {
      button.addEventListener("click", async () => {
        const upload = currentUploads[Number(button.dataset.copyIndex)];
        await navigator.clipboard.writeText(upload.url_download);
        const orig = button.innerHTML;
        button.innerHTML = ICONS.check;
        button.style.color = "var(--accent)";
        setTimeout(() => { button.innerHTML = orig; button.style.color = ""; }, 1400);
      });
    });
    historyList.querySelectorAll("[data-qr-index]").forEach(button => {
      button.addEventListener("click", () => {
        const upload = currentUploads[Number(button.dataset.qrIndex)];
        showQrModal(upload.url_download + "/qr.svg");
      });
    });
    historyList.querySelectorAll("[data-delete-index]").forEach(button => {
      button.addEventListener("click", () => deleteUpload(currentUploads[Number(button.dataset.deleteIndex)], button));
    });
  } catch (e) {
    historyList.setAttribute("data-empty", t("noHistory"));
  }
}
function downloadText(upload) {
  const total = upload.allowed_downloads;
  if (Number.isFinite(upload.download_count)) return t("downloadsDetail", { used: upload.download_count, total });
  if (Number.isFinite(upload.downloads_remaining)) return t("downloadsDetail", { used: Math.max(0, total - upload.downloads_remaining), total });
  return t("downloadsUnknown", { total });
}
function renderModal(upload) {
  modalTitle.textContent = t("qrCode");
  modalMeta.innerHTML = `<span><strong>${escapeHtml(upload.file_name)}</strong></span><span>${t("expiresLabel")}<strong>${fmtDate(upload.expire_at)}</strong></span>`;
  modalQr.src = `${upload.url_download}/qr.svg`;
  modalUrl.value = upload.url_download;
  modalOpen.href = upload.url_download;
  modalShare.href = "https://x.com/intent/tweet?" + new URLSearchParams({ text:t("shareText"), url: upload.url_download });
  modalSaveQr.href = `${upload.url_download}/qr.svg`;
  modalSaveQr.setAttribute("download", `flashshare-${upload.public_id || upload.id}.svg`);
  modalCopy.textContent = t("copy");
  renderModalActions();
}
function openModal(upload) {
  activeUpload = upload;
  renderModal(upload);
  uploadModal.classList.remove("hidden");
}
function closeModal() {
  activeUpload = null;
  uploadModal.classList.add("hidden");
}
modalClose.addEventListener("click", closeModal);
uploadModal.addEventListener("click", event => {
  if (event.target === uploadModal) closeModal();
});
modalCopy.addEventListener("click", async () => {
  if (!activeUpload) return;
  await navigator.clipboard.writeText(activeUpload.url_download);
  modalCopy.textContent = t("copied");
});
async function deleteUpload(upload, triggerButton = null) {
  if (!upload || !confirm(t("confirmDelete"))) return;
  const originalHtml = triggerButton ? triggerButton.innerHTML : "";
  if (triggerButton) {
    triggerButton.disabled = true;
    if (triggerButton === modalDelete) triggerButton.innerHTML = actionLabel("trash", t("deleting"));
  }
  try {
    const res = await fetch(`/api/uploads/${upload.public_id}`, {
      method: "DELETE",
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || t("failed"));
    if (activeUpload && Number(activeUpload.id) === Number(upload.id)) closeModal();
    showResult(escapeHtml(t("deleted")));
    await loadHistory();
  } catch (err) {
    if (activeUpload && Number(activeUpload.id) === Number(upload.id)) {
      modalMeta.textContent = err.message;
    } else {
      showResult(escapeHtml(err.message), true);
    }
  } finally {
    if (triggerButton) {
      triggerButton.disabled = false;
      if (triggerButton === modalDelete) renderModalActions();
      else triggerButton.innerHTML = originalHtml;
    }
  }
}
modalDelete.addEventListener("click", () => deleteUpload(activeUpload, modalDelete));
document.getElementById("refreshBtn").addEventListener("click", async () => {
  const btn = document.getElementById("refreshBtn");
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<div class="btn-spinner"></div>`;
  try {
    await loadHistory();
  } catch (e) {} finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
});
langButton.addEventListener("click", () => {
  const hidden = langMenu.classList.toggle("hidden");
  langButton.setAttribute("aria-expanded", hidden ? "false" : "true");
});
langMenu.addEventListener("click", event => {
  const option = event.target.closest("[data-lang]");
  if (!option) return;
  lang = SUPPORTED_LANGS.includes(option.dataset.lang) ? option.dataset.lang : "en";
  localStorage.setItem("flashshare_lang", lang);
  document.cookie = "flashshare_lang=" + lang + ";Path=/;Max-Age=31536000;SameSite=Lax";
  langMenu.classList.add("hidden");
  applyI18n();
});
document.addEventListener("click", event => {
  if (event.target === langButton || langMenu.contains(event.target)) return;
  langMenu.classList.add("hidden");
  langButton.setAttribute("aria-expanded", "false");
});
// User dropdown menu
const userMenuBtn = document.getElementById("userMenuBtn");
const userDropdown = document.getElementById("userDropdown");
if (userMenuBtn && userDropdown) {
  userMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const hidden = userDropdown.classList.toggle("hidden");
    userMenuBtn.setAttribute("aria-expanded", hidden ? "false" : "true");
  });
  document.addEventListener("click", (e) => {
    if (e.target === userMenuBtn || userDropdown.contains(e.target)) return;
    userDropdown.classList.add("hidden");
    userMenuBtn.setAttribute("aria-expanded", "false");
  });
}

// ── Home Tools ──────────────────────────────────────────────
const homeTools = document.getElementById("homeTools");
const toolOverlay = document.getElementById("toolOverlay");
const toolOverlayBody = document.getElementById("toolOverlayBody");
const toolOverlayClose = document.getElementById("toolOverlayClose");
const ACCEPT_TAGS = {
  "image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WebP", "image/gif": "GIF",
  "image/bmp": "BMP", "image/tiff": "TIFF", "image/svg+xml": "SVG", "image/avif": "AVIF",
  "image/x-icon": "ICO", "image/vnd.microsoft.icon": "ICO",
  "application/pdf": "PDF", "application/zip": "ZIP", "text/plain": "TXT",
};
let activeTool = null;
let activePreset = null;
let lastUploadResult = null;
let homeToolsData = [];

function loadHomeTools() {
  if (!homeTools) return;
  const tools = window.__TOOLS__;
  if (!tools || !tools.length) return;
  // DOM already rendered server-side — just bind click handlers
  homeToolsData = tools;
  homeTools.querySelectorAll(".tool-tab").forEach(tab => {
    const toolId = tab.dataset.toolId;
    const tool = tools.find(t => t.id === toolId);
    if (!tool) return;
    tab.addEventListener("click", () => {
      // "share" is the default fallback — select directly, no overlay
      if (toolId === "share") {
        activeTool = tool;
        activePreset = null;
        localStorage.setItem("flashshare_tool", "share");
        localStorage.removeItem("flashshare_tool_preset");
        applyToolSelection();
        return;
      }
      openToolOverlay(tool, tab);
    });
  });

  // Restore last selection from localStorage, default to "share"
  const saved = localStorage.getItem("flashshare_tool") || "share";
  const savedTool = tools.find(t => t.id === saved);
  if (savedTool) {
    activeTool = savedTool;
    activePreset = savedTool.id === "share" ? null : (localStorage.getItem("flashshare_tool_preset") || null);
    applyToolSelection();
  }
}

// ── Tool Overlay (shared component) ─────────────────────────

function openToolOverlay(tool, btn) {
  const toolName = t(tool.id);
  const iconSvg = svgIcon(tool.icon);
  const isActive = activeTool && activeTool.id === tool.id;

  // Build overlay content
  let html = `<div class="tool-overlay-head"><svg viewBox="0 0 24 24" aria-hidden="true">${iconSvg}</svg><h3>${escapeHtml(toolName)}</h3></div>`;

  // Description
  const descKey = tool.id + "_desc";
  const desc = t(descKey);
  if (desc && desc !== descKey) {
    html += `<p class="tool-overlay-desc">${escapeHtml(desc)}</p>`;
  }

  // Presets section
  let defaultPreset = null;
  if (tool.presets && tool.presets.length > 0) {
    // Use user's last selection if this tool is active, otherwise use registered default
    if (isActive && activePreset) {
      defaultPreset = activePreset;
    } else {
      const dp = tool.presets.find(p => p.default);
      defaultPreset = dp ? dp.id : tool.presets[0].id;
    }
    html += `<div class="tool-overlay-section"><div class="tool-overlay-section-label">${t("intensity")}</div><div class="tool-overlay-presets">`;
    tool.presets.forEach((p) => {
      const pname = t(p.id) !== p.id ? t(p.id) : (p.label && p.label[lang] ? p.label[lang] : (p.label && p.label.en ? p.label.en : p.id));
      const sel = (p.id === defaultPreset) ? " active" : "";
      html += `<button type="button" class="tool-overlay-preset${sel}" data-preset="${p.id}">${escapeHtml(pname)}</button>`;
    });
    html += `</div></div>`;
  }

  // Actions — always show confirm/cancel, even when active (user can change preset)
  html += `<div class="tool-overlay-actions"><button type="button" class="tool-btn muted" id="overlayCancel">${isActive ? t("cancelTool") : t("cancel")}</button><button type="button" class="tool-btn primary" id="overlayConfirm">${t("confirm")}</button></div>`;

  toolOverlayBody.innerHTML = html;
  toolOverlay.classList.remove("hidden");

  // Preset selection
  const presetButtons = toolOverlayBody.querySelectorAll(".tool-overlay-preset");
  let selectedPreset = defaultPreset;
  presetButtons.forEach(pbtn => {
    if (pbtn.classList.contains("active")) selectedPreset = pbtn.dataset.preset;
    pbtn.addEventListener("click", () => {
      presetButtons.forEach(b => b.classList.remove("active"));
      pbtn.classList.add("active");
      selectedPreset = pbtn.dataset.preset;
    });
  });

  // Confirm
  const confirmBtn = document.getElementById("overlayConfirm");
  if (confirmBtn) {
    confirmBtn.addEventListener("click", () => {
      activeTool = tool;
      activePreset = selectedPreset;
      // Save to localStorage
      localStorage.setItem("flashshare_tool", tool.id);
      if (selectedPreset) localStorage.setItem("flashshare_tool_preset", selectedPreset);
      applyToolSelection();
      closeToolOverlay();
    });
  }

  // Cancel / Deselect
  const cancelBtn = document.getElementById("overlayCancel");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => {
      if (isActive) {
        // Cancel falls back to "share" (default), not null
        const shareTool = homeToolsData.find(t => t.id === "share");
        activeTool = shareTool || null;
        activePreset = null;
        if (shareTool) {
          localStorage.setItem("flashshare_tool", "share");
          localStorage.removeItem("flashshare_tool_preset");
        } else {
          localStorage.removeItem("flashshare_tool");
          localStorage.removeItem("flashshare_tool_preset");
        }
        applyToolSelection();
      }
      closeToolOverlay();
    });
  }
}

function closeToolOverlay() {
  toolOverlay.classList.add("hidden");
  toolOverlayBody.innerHTML = "";
}

toolOverlayClose.addEventListener("click", closeToolOverlay);
toolOverlay.addEventListener("click", (e) => { if (e.target === toolOverlay) closeToolOverlay(); });

// Apply tool selection to the upload zone
function applyToolSelection() {
  const formatTags = document.querySelector(".drop-formats");

  // Clear all badges + set active state on ALL tabs
  document.querySelectorAll(".tool-tab").forEach(b => {
    const isActive = activeTool && b.dataset.toolId === activeTool.id;
    b.classList.toggle("active", isActive);
    const oldBadge = b.querySelector(".tool-preset-badge");
    if (oldBadge) oldBadge.remove();
  });

  if (!activeTool || activeTool.id === "share") {
    file.accept = "";
    if (formatTags) formatTags.innerHTML = "<span>IMG</span><span>PDF</span><span>DOC</span><span>XLS</span><span>PPT</span><span>TXT</span><span>ZIP</span><span>CODE</span>";
    updateToolDescription();
    return;
  }

  // Add preset badge on active tool tab
  if (activePreset) {
    const activeTab = document.querySelector('.tool-tab.active');
    if (activeTab) {
      let presetLabel = t(activePreset);
      if (presetLabel === activePreset) {
        const pt = (homeToolsData.find(t => t.id === activeTool.id) || {}).presets || [];
        const p = pt.find(p => p.id === activePreset);
        if (p) presetLabel = (p.label && p.label[lang]) || (p.label && p.label.en) || activePreset;
      }
      if (presetLabel && presetLabel !== activePreset) {
        const badge = document.createElement("span");
        badge.className = "tool-preset-badge";
        badge.textContent = " · " + presetLabel;
        activeTab.appendChild(badge);
      }
    }
  }

  // Bind accept
  if (activeTool.accept && activeTool.accept.length > 0) {
    file.accept = activeTool.accept.join(",");
  }

  // Update format tags
  if (formatTags && activeTool.accept) {
    const tags = [...new Set(activeTool.accept.map(m => ACCEPT_TAGS[m]).filter(Boolean))];
    formatTags.innerHTML = tags.map(tg => `<span>${tg}</span>`).join("");
  }

  // Show description below tools
  updateToolDescription();
}

function updateToolDescription() {
  const descEl = document.getElementById("toolDesc");
  if (!descEl) return;
  if (!activeTool) {
    descEl.textContent = "";
    descEl.classList.add("hidden");
    return;
  }
  const descKey = activeTool.id + "_desc";
  const desc = t(descKey);
  if (desc && desc !== descKey) {
    descEl.textContent = desc;
    descEl.classList.remove("hidden");
  } else {
    descEl.classList.add("hidden");
  }
}

function showToolProcessing(toolName) {
  showResult(`<div class="tool-processing"><div class="upload-spinner"></div><span>${escapeHtml(toolName)} · ${t("processing")}</span></div>`);
}

function showToolError(message, originalFile) {
  const origUrl = originalFile && (originalFile.downloadUrl || originalFile.url) ? (originalFile.downloadUrl || originalFile.url) : "";
  const origLink = origUrl ? `<a class="tool-btn primary" href="${escapeHtml(origUrl)}" target="_blank">${t("useOriginal")}</a>` : "";
  showResult(`<div class="result-card"><div class="result-error">${escapeHtml(message)}</div><div class="result-buttons"><button class="tool-btn" id="toolRetry" type="button">${t("retry")}</button>${origLink}</div></div>`, true);
  const retryBtn = document.getElementById("toolRetry");
  if (retryBtn && lastUploadResult) {
    retryBtn.addEventListener("click", () => retryTool());
  }
}

async function retryTool() {
  if (!activeTool || !lastUploadResult) return;
  const toolName = t(activeTool.id);
  showToolProcessing(toolName);
  const expirySelect = form.querySelector("[name=\"expiryDays\"]");
  const dlSelect = form.querySelector("[name=\"allowedDownloads\"]");
  try {
    const toolRes = await fetch("/api/tools/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tool_id: activeTool.id,
        public_id: lastUploadResult.publicId,
        params: {
          preset: activePreset,
          expiry_days: expirySelect ? parseInt(expirySelect.value) : 1,
          allowed_downloads: dlSelect ? parseInt(dlSelect.value) : 50,
        },
      }),
    });
    const toolData = await toolRes.json();
    if (!toolData.ok) { showToolError(toolData.error, lastUploadResult); return; }
    showShareResult(toolData.result);
    loadHistory();
  } catch (e) { showToolError(e.message, lastUploadResult); }
}

// Stable icon paths via unified registry

async function shareImageFile(url, filename) {
  try {
    const res = await fetch(url);
    if (!res.ok) return false;
    const blob = await res.blob();
    const file = new File([blob], filename || "image.png", { type: blob.type || "image/png" });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: filename || "image" });
      return true;
    }
  } catch (e) {
    if (e.name === "AbortError") return true; // user cancelled — NOT a failure
    console.warn("share failed:", e.message);
  }
  return false;
}

function showQrModal(qrUrl) {
  let qrModal = document.getElementById("homeQrModal");
  if (!qrModal) {
    qrModal = document.createElement("div");
    qrModal.id = "homeQrModal";
    qrModal.className = "modal hidden";
    qrModal.setAttribute("role", "dialog");
    qrModal.setAttribute("aria-modal", "true");
    qrModal.innerHTML = '<div class="modal-panel qr-modal-panel"><button class="modal-close" type="button" aria-label="Close">' + svgIcon("close") + '</button><img alt="QR" width="200" height="200"></div>';
    document.body.appendChild(qrModal);
    const closeBtn = qrModal.querySelector(".modal-close");
    closeBtn.addEventListener("click", () => qrModal.classList.add("hidden"));
    qrModal.addEventListener("click", (e) => { if (e.target === qrModal) qrModal.classList.add("hidden"); });
  }
  qrModal.querySelector("img").src = qrUrl;
  qrModal.classList.remove("hidden");
}

function showShareResult(file) {
  const url = file.downloadUrl || file.url;
  const dlUrl = url + (url.includes("?") ? "&" : "?") + "download=1";
  const share = "https://x.com/intent/tweet?" + new URLSearchParams({ text: t("shareText"), url });
  const copyId = `copy-${Date.now()}`;
  const qrId = `qr-${Date.now()}`;
  const shareImgId = `shareimg-${Date.now()}`;
  const meta = fmtSize(file.size);

  let actions = `<a class="action-btn primary" href="${escapeHtml(dlUrl)}" download aria-label="${t("downloadFile")}">${svgIcon("download")}<span>${t("downloadFile")}</span></a>`;
  actions += `<button class="action-btn" id="${shareImgId}" type="button" aria-label="${t("shareImg")}">${svgIcon("share")}</button>`;
  actions += `<span class="action-sep"></span>`;
  actions += `<button class="action-btn" id="${copyId}" type="button" aria-label="${t("copy")}">${svgIcon("copy")}</button>`;
  actions += `<button class="action-btn" id="${qrId}" type="button" aria-label="QR">${svgIcon("qr")}</button>`;
  actions += `<a class="action-btn" href="${escapeHtml(share)}" target="_blank" rel="noreferrer" aria-label="${t("shareX")}"><span class="x-glyph" aria-hidden="true">𝕏</span></a>`;

  showResult(
    `<div class="result-card">` +
    `<div class="result-check">${svgIcon("check")}</div>` +
    `<div class="result-card-title">${t("ready")}</div>` +
    `<div class="result-meta">${escapeHtml(meta)}</div>` +
    `<div class="file-actions">${actions}</div>` +
    `</div>`
  );
  const copyBtn = document.getElementById(copyId);
  if (copyBtn) copyBtn.addEventListener("click", async () => {
    await navigator.clipboard.writeText(url);
    copyBtn.innerHTML = svgIcon("check");
    copyBtn.style.color = "var(--accent)";
    copyBtn.style.borderColor = "var(--accent)";
    setTimeout(() => { copyBtn.innerHTML = svgIcon("copy"); copyBtn.style.color = ""; copyBtn.style.borderColor = ""; }, 2000);
  });
  const qrBtn = document.getElementById(qrId);
  if (qrBtn) qrBtn.addEventListener("click", () => showQrModal(url + "/qr.svg"));
  const shareImgBtn = document.getElementById(shareImgId);
  if (shareImgBtn) shareImgBtn.addEventListener("click", async () => {
    const ok = await shareImageFile(dlUrl, file.filename || file.name || "image.png");
    if (!ok) window.location.href = dlUrl; // fallback to download
  });
}

// Load tools on page load
loadHomeTools();
applyI18n();
