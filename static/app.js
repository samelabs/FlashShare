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
let currentUploads = []; // kept for modal deleteUpload compatibility
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

// i18n: t(), applyI18n(), detectLang(), lang data — all provided by footer.html global engine
const lang = window.__LANG || "en";
// Helper: lang-aware t() wrapper (delegates to global)
function t(key, vars = {}) { return window.t(key, vars); }

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
  const shareUrl = "https://x.com/intent/tweet?" + new URLSearchParams({ text:t("homeShareText"), url: location.origin });
  homeShare.href = shareUrl;
  homeShare.setAttribute("aria-label", t("shareX"));
  homeShare.setAttribute("title", t("shareX"));
}
// Home page i18n callback — called by global applyI18n() after data-i18n sweep
window.__afterI18n = function() {
  document.title = t("title");
  renderHomeShare();
  modalCopy.textContent = t("copy");
  renderModalActions();
  if (!file.files[0]) fileLabel.textContent = t("dropEmpty");
  applyToolSelection();
  renderAccount();
  if (activeUpload) renderModal(activeUpload);
  if (lastErrorKey && result && result.classList.contains("error")) {
    showError(lastErrorKey);
  }
};
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
let lastErrorKey = null;
function showResult(html, isError=false) {
  result.className = "result" + (isError ? " error" : "");
  result.innerHTML = html;
}
function showError(errorKey) {
  lastErrorKey = errorKey;
  showResult(`<div class="result-card"><div class="result-error">${escapeHtml(t(errorKey))}</div></div>`, true);
}
file.addEventListener("change", () => {
  if (!window.__USER__) { location.href = "/auth/x/login"; return; }
  if (file.files.length > 0) {
    for (let i = 0; i < file.files.length; i++) {
      const ext = file.files[i].name.toLowerCase().match(/\.(\w+)$/);
      if (ext && !EXT_TO_TYPE[ext[1]]) {
        showError("err_format_unsupported");
        file.value = "";
        fileLabel.textContent = t("dropEmpty");
        return;
      }
    }
    if (file.files.length === 1) {
      fileLabel.textContent = t("dropSelected", { name: file.files[0].name, size: fmtSize(file.files[0].size) });
    } else {
      fileLabel.textContent = file.files.length + " files · " + fmtSize(Array.from(file.files).reduce((s,f) => s+f.size, 0));
    }
  } else {
    fileLabel.textContent = t("dropEmpty");
  }
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
  const isMulti = activeTool && activeTool.multi;
  const numFiles = file.files.length;
  submitBtn.disabled = true;
  submitBtn.innerHTML = `<div class="btn-spinner"></div>`;
  showResult(`<div class="upload-progress"><div class="upload-progress-bar"><div class="upload-progress-fill" id="progressFill"></div></div><div class="upload-progress-text" id="progressText">0%</div></div>`);
  const formData = new FormData(form);
  if (activeTool && activeTool.id !== "share") {
    formData.append("tool", activeTool.id);
    if (activePreset) formData.append("preset", activePreset);
  }
  const fileSize = Array.from(file.files).reduce((s,f) => s+f.size, 0);
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
      if (xhr.status >= 400 || !data.ok) { const ek = data.error || "failed"; throw new Error(t(ek) !== ek ? t(ek) : t("failed")); }
      // No tool (or default share tool) → show result directly
      if (!activeTool || activeTool.id === "share") {
        if (data.files) {
          data.files.forEach((f, i) => setTimeout(() => showShareResult(f), i * 100));
        } else {
          showShareResult(data.file);
        }
        form.reset();
        fileLabel.textContent = t("dropEmpty");
        window._loadHistory && window._loadHistory();
        return;
      }
      // Tool active → Step 2: execute tool
      showToolProcessing(t(activeTool.id));
      const expirySelect = form.querySelector("[name=\"expiryDays\"]");
      const dlSelect = form.querySelector("[name=\"allowedDownloads\"]");
      const toolBody = {
        tool_id: activeTool.id,
        params: {
          preset: activePreset,
          expiry_days: expirySelect ? parseInt(expirySelect.value) : 1,
          allowed_downloads: dlSelect ? parseInt(dlSelect.value) : 50,
        },
      };
      if (isMulti && data.files) {
        toolBody.public_ids = data.files.map(f => f.publicId);
      } else {
        toolBody.public_id = data.file.publicId;
      }
      const toolRes = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(toolBody),
      });
      const toolData = await toolRes.json();
      if (!toolData.ok) {
        const ref = (data.files ? data.files[0] : data.file);
        showToolError(toolData.error === "auth_required" ? t("signIn") : toolData.error, ref);
        return;
      }
      lastUploadResult = toolData.result;
      showShareResult(toolData.result);
      form.reset();
      fileLabel.textContent = t("dropEmpty");
      window._loadHistory && window._loadHistory();
    } catch (err) {
      showResult(escapeHtml(err.message), true);
    } finally {
      submitBtn.disabled = false;
      submitBtn.innerHTML = `${ICONS.upload}<span data-i18n="create">${t("create")}</span>`;
    }
  };
  xhr.onerror = () => {
    showResult(escapeHtml(t("failed")), true);
    submitBtn.disabled = false;
    submitBtn.innerHTML = `${ICONS.upload}<span data-i18n="create">${t("create")}</span>`;
  };
  xhr.send(formData);
});
// loadHistory moved to footer.html (shared nav behavior)
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
    if (!res.ok || !data.ok) { const ek = data.error || "failed"; throw new Error(t(ek) !== ek ? t(ek) : t("failed")); }
    if (activeUpload && Number(activeUpload.id) === Number(upload.id)) closeModal();
    showResult(escapeHtml(t("deleted")));
    window._loadHistory && window._loadHistory();
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

// ── Home Tools ──────────────────────────────────────────────────────────────
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
  html += `<div class="tool-overlay-actions"><button type="button" class="overlay-btn muted" id="overlayCancel">${isActive ? t("cancelTool") : t("cancel")}</button><button type="button" class="overlay-btn primary" id="overlayConfirm">${t("confirm")}</button></div>`;

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

  // Bind multiple (multi-file tools like pdf_merge)
  if (activeTool.multi) {
    file.setAttribute("multiple", "");
  } else {
    file.removeAttribute("multiple");
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
  const origLink = origUrl ? `<a class="overlay-btn primary" href="${escapeHtml(origUrl)}" target="_blank">${t("useOriginal")}</a>` : "";
  showResult(`<div class="result-card"><div class="result-error">${escapeHtml(message)}</div><div class="result-buttons"><button class="overlay-btn" id="toolRetry" type="button">${t("retry")}</button>${origLink}</div></div>`, true);
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
    window._loadHistory && window._loadHistory();
  } catch (e) { showToolError(e.message, lastUploadResult); }
}

// Stable icon paths via unified registry

async function shareFile(url, filename) {
  try {
    const res = await fetch(url);
    if (!res.ok) return false;
    const blob = await res.blob();
    const file = new File([blob], filename || "download", { type: blob.type || "application/octet-stream" });
    // Try navigator.share directly — iOS Safari supports share({files}) but not canShare.
    if (navigator.share) {
      await navigator.share({ files: [file], title: filename || "download" });
      return true;
    }
  } catch (e) {
    if (e.name === "AbortError") return true; // user cancelled
    console.warn("share failed:", e.message);
  }
  return false;
}

function isStandalonePWA() {
  return window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone === true;
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
  lastErrorKey = null;
  const url = file.downloadUrl || file.url;
  const dlUrl = url + (url.includes("?") ? "&" : "?") + "download=1";
  const share = "https://x.com/intent/tweet?" + new URLSearchParams({ text: t("shareText"), url });
  const copyId = `copy-${Date.now()}`;
  const qrId = `qr-${Date.now()}`;
  const shareImgId = `shareimg-${Date.now()}`;
  const meta = fmtSize(file.size);
  const pwa = isStandalonePWA();

  let actions;
  if (pwa) {
    // PWA standalone: <a download> navigates inside PWA and traps user.
    // Use navigator.share so the system sheet handles file transfer.
    const dlBtnId = `dlbtn-${Date.now()}`;
    actions = `<button class="action-btn primary" id="${dlBtnId}" type="button" aria-label="${t("downloadFile")}">${svgIcon("download")}<span data-i18n="downloadFile">${t("downloadFile")}</span></button>`;
  } else {
    actions = `<a class="action-btn primary" href="${escapeHtml(dlUrl)}" download aria-label="${t("downloadFile")}">${svgIcon("download")}<span data-i18n="downloadFile">${t("downloadFile")}</span></a>`;
  }
  actions += `<button class="action-btn" id="${shareImgId}" type="button" aria-label="${t("shareImg")}">${svgIcon("share")}</button>`;
  actions += `<span class="action-sep"></span>`;
  actions += `<button class="action-btn" id="${copyId}" type="button" aria-label="${t("copy")}">${svgIcon("copy")}</button>`;
  actions += `<button class="action-btn" id="${qrId}" type="button" aria-label="QR">${svgIcon("qr")}</button>`;
  actions += `<a class="action-btn" href="${escapeHtml(share)}" target="_blank" rel="noreferrer" aria-label="${t("shareX")}"><span class="x-glyph" aria-hidden="true">𝕏</span></a>`;

  showResult(
    `<div class="result-card">` +
    `<div class="result-check">${svgIcon("check")}</div>` +
    `<div class="result-card-title" data-i18n="ready">${t("ready")}</div>` +
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
  // PWA standalone: download button uses navigator.share instead of <a download>
  if (pwa) {
    const dlBtn = document.querySelector('.result-card .action-btn.primary');
    if (dlBtn) dlBtn.addEventListener("click", async () => {
      const ok = await shareFile(dlUrl, file.filename || file.name || "download");
      if (!ok) window.location.href = dlUrl;
    });
  }
  const shareImgBtn = document.getElementById(shareImgId);
  if (shareImgBtn) shareImgBtn.addEventListener("click", async () => {
    const ok = await shareFile(dlUrl, file.filename || file.name || "download");
    if (!ok) window.location.href = dlUrl;
  });
}

// Load tools on page load
loadHomeTools();
window.applyI18n();
