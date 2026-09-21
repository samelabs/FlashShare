const form = document.getElementById("uploadForm");

// ── Shared web affordability preflight (advisory, read-only) ──
// tool == null / pass_through → no preflight. Executable tool → ask
// /api/tools/preflight BEFORE any upload XHR starts. An explicit
// 402 err_insufficient_credits stops the flow (no FormData upload);
// network/5xx stays advisory — the authoritative gate still decides.
window.preflightToolAffordability = async function (tool) {
  if (!tool || tool.pass_through) return { ok: true };
  try {
    const r = await fetch("/api/tools/preflight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_id: tool.id }),
    });
    if (r.status === 402) {
      const d = await r.json().catch(() => null);
      if (d && d.error === "err_insufficient_credits") {
        return { ok: false, error: "err_insufficient_credits" };
      }
    }
    return { ok: true }; // advisory: any other outcome defers to the gate
  } catch (e) {
    return { ok: true }; // network failure — never a new authority
  }
};
// ── Load self-check: version-skew (stale cached JS vs fresh HTML) must
// fail LOUD, not silently half-bind events. ──
// 单工具页 (tool_page) 没有 uploadForm/dropZone — 那是首页多工具入口的
// 形态; 该页只复用本文件的共享底座 (t/showResult/showShareResult/…),
// 自检跳过表单 id, 但 toolPage 标记必须在 (缺 = 真版本错位)。
(function(){
  // 单工具上下文页 (工具页 toolRunForm / 用户页 userPage) 无 uploadForm/
  // dropZone — 那是首页多工具入口的形态; 这些页只复用本文件的共享底座
  // (t/showResult/showShareResult/…), 自检跳过表单 id。
  const singleToolPage = !!(document.getElementById("toolRunForm") || document.getElementById("userPage"));
  const need = singleToolPage
    ? ["result"]
    : ["uploadForm","file","fileLabel","dropZone","submitBtn"];
  if (!singleToolPage) window.__TOOLPAGE__ = false;
  const missing = need.filter(id => !document.getElementById(id));
  if (missing.length) {
    console.error("[518] DOM missing:", missing.join(", "), "— stale JS? hard-refresh (Cmd/Ctrl+Shift+R)");
    const b = document.createElement("div");
    b.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#f4212e;color:#fff;padding:10px 16px;font:600 14px system-ui;text-align:center";
    b.textContent = "页面资源版本不一致 — 请强制刷新 (Ctrl/Cmd+Shift+R)";
    document.body && document.body.appendChild(b);
    throw new Error("[518] abort: DOM missing " + missing.join(", "));
  }
  if (singleToolPage) {
    window.__TOOLPAGE__ = true;
    return;
  }
  // 首页形态: 绑定表单事件 (下方代码使用这些引用)
})();

const file = document.getElementById("file");
const fileLabel = document.getElementById("fileLabel");
const result = document.getElementById("result");
const submitBtn = document.getElementById("submitBtn");
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
const limits = window.__LIMITS__ || {};
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
const _IC = window.__ICONS__ || {};
function svgIcon(name) {
  const path = _IC[name] || _IC.info || "";
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}
// Canonical icon renderer — site-wide single source (task 8: no raw
// `__PAGE__.icons.check` / `✓` literals in page scripts).
window.iconSvg = svgIcon;
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

// i18n: t(), applyI18n(), lang data — all provided by base.html global engine
// Local alias — MUST stay arrow/const: a top-level `function t` declaration
// would shadow window.t (engine) and recurse infinitely (call-stack overflow).
const t = (key, vars = {}) => window.t(key, vars);
function currentLang() { return window.__LANG || "en"; }

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
  const lm = window.__LOCALE_MAP || {};
  return new Date(ts * 1000).toLocaleString(lm[currentLang()] || "en", { hour12:false });
}
// 直选/透传判定单点 (tool modes v3): 无输入声明 = 透传工具。
// A8 配套: 交互行为一律经此判定, 禁止工具名比较。
function isPassThrough(tool) {
  if (!tool) return true;
  // 直传判定 = 工具声明 (platform_tools.pass_through, 仅直传语义工具为 true),
  // 不再从 presets/params/multi 推断 — 无参执行型工具 (crop_white/doc2pdf)
  // 和无参 http 工具必须走 execute, 推断会把它们误判成直传。
  return !!tool.pass_through;
}

// 标签解析单点 (tool modes v3): str 原样 / dict[lang]→dict.en→首值。勿在他处重写此链。

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}
function actionLabel(icon, label) {
  return `<span class="action-mark">${ICONS[icon]}</span><span class="action-text">${escapeHtml(label)}</span>`;
}
function visibleAction(icon, label) {
  return `<span class="action-mark">${ICONS[icon]}</span><span>${escapeHtml(label)}</span>`;
}
function renderModalActions() {
  if (!modalOpen) return; // 无结果浮层 dom 的页 (用户页等) — 结果操作按钮由 showShareResult 动态渲染
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
// Home page i18n callback — called by global applyI18n() after data-i18n sweep
window.__afterI18n = function() {
  document.title = t("title");
  if (modalCopy) modalCopy.textContent = t("copy");
  // Upload-card param values follow language
  const expSel = document.getElementById("expiryDays");
  const dlSel = document.getElementById("allowedDownloads");
  const expLab = document.getElementById("expiryLabel");
  const dlLab = document.getElementById("downloadsLabel");
  if (expSel && expLab) {
    const d = expSel.value;
    expLab.textContent = d === "1" ? "1 " + t("day_unit") : d + " " + t("days_unit");
    // Dynamic options (value + generic unit) follow language switches too
    Array.from(expSel.options).forEach(o => {
      const n = parseInt(o.value, 10);
      o.textContent = n + " " + t(n === 1 ? "day_unit" : "days_unit");
    });
  }
  if (dlSel && dlLab) {
    dlLab.textContent = dlSel.value + " " + t("times_unit");
    Array.from(dlSel.options).forEach(o => {
      o.textContent = parseInt(o.value, 10) + " " + t("times_unit");
    });
  }
  // Tool names: data-driven (tool modes v3) — resolveLabel 同链 (dict[lang]→en→首值)
  document.querySelectorAll("#homeTools .tool-tab").forEach(tab => {
    const tool = homeToolsData.find(t => t.id === tab.dataset.toolId);
    if (!tool) return;
    const nameEl = tab.querySelector(".tool-tab-name");
    if (nameEl) nameEl.textContent = resolveLabel(tool.label) || tool.id;
  });
  renderModalActions();
  if (file && fileLabel && !file.files[0]) fileLabel.textContent = t("dropEmpty");
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
  if (window.__TOOLPAGE__) return; // 工具页无表单 — 登录态由 run() 引导
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
// ── ZIP local preview (before upload) ──
let zipPreviewBtnEl = null;
function ensureZipPreviewBtn() {
  if (zipPreviewBtnEl) return zipPreviewBtnEl;
  const zone = document.getElementById("dropZone");
  if (!zone || !zone.parentElement) return null;
  zipPreviewBtnEl = document.createElement("button");
  zipPreviewBtnEl.type = "button";
  zipPreviewBtnEl.id = "zipPreviewLocalBtn";
  zipPreviewBtnEl.className = "action-btn zip-preview-trigger hidden";
  zone.parentElement.insertBefore(zipPreviewBtnEl, zone.nextSibling);
  zipPreviewBtnEl.addEventListener("click", () => {
    const f = file.files && file.files[0];
    if (!f || !window.zipPreview || !window.zipPreview.isZipName(f.name, f.type)) return;
    window.zipPreview.parseLocalZip(f).then(
      (entries) => window.zipPreview.showZipModal(entries, { truncated: entries.length >= 500 }),
      (reason) => window.zipPreview.showZipError(reason)
    );
  });
  return zipPreviewBtnEl;
}
function isZipSelection() {
  const f = file.files && file.files[0];
  if (!f) return false;
  return /\.zip$/i.test(f.name) || f.type === "application/zip" || f.type === "application/x-zip-compressed";
}
function updateZipPreviewBtn() {
  const btn = ensureZipPreviewBtn();
  if (!btn) return;
  if (isZipSelection() && !window.zipPreview) { ensureZipPreview(); return; }
  const isZip = isZipSelection() && !!window.zipPreview;
  btn.classList.toggle("hidden", !isZip);
  if (isZip) btn.innerHTML = svgIcon("eye") + '<span data-i18n="zip_preview_btn">' + t("zip_preview_btn") + "</span>";
}
// True lazy-load: module injected only when a zip is actually selected/uploaded.
let zipPreviewLoading = false;
function ensureZipPreview(cb) {
  if (window.zipPreview) { if (cb) cb(); return; }
  if (zipPreviewLoading) return;
  zipPreviewLoading = true;
  const s = document.createElement("script");
  s.src = "/static/zip-preview.js";
  s.onload = () => { updateZipPreviewBtn(); if (cb) cb(); };
  document.head.appendChild(s);
}

if (!window.__TOOLPAGE__) file.addEventListener("change", () => {
  pendingOperationRef = null; // operation input changed → new identity next submit
  window.invalidateToolOperation && window.invalidateToolOperation();
  if (!window.__USER__) { location.href = "/auth/login"; return; }
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
      // VIS-003 fix: multi-file count uses the canonical i18n key — no
      // hard-coded English ("2 files"), count + word + total size.
      fileLabel.textContent = file.files.length + " " + t("tool_multi_files") + " · " + fmtSize(Array.from(file.files).reduce((s,f) => s+f.size, 0));
    }
  } else {
    fileLabel.textContent = t("dropEmpty");
  }
  updateZipPreviewBtn();
});
if (!window.__TOOLPAGE__) dropZone.addEventListener("click", (e) => {
  if (!window.__USER__) { e.preventDefault(); location.href = "/auth/login"; }
});
["dragenter", "dragover"].forEach(name => { if (dropZone) dropZone.addEventListener(name, event => {
  event.preventDefault();
  dropZone.classList.add("dragover");
}); });
["dragleave", "drop"].forEach(name => { if (dropZone) dropZone.addEventListener(name, event => {
  event.preventDefault();
  dropZone.classList.remove("dragover");
}); });
if (!window.__TOOLPAGE__) dropZone.addEventListener("drop", event => {
  if (!window.__USER__) { location.href = "/auth/login"; return; }
  if (event.dataTransfer.files.length) {
    const dt = new DataTransfer();
    for (const f of event.dataTransfer.files) dt.items.add(f);
    file.files = dt.files;
    file.dispatchEvent(new Event("change"));
  }
});
// delivery selects are operation input too — changing them invalidates
// the pending operation identity
if (!window.__TOOLPAGE__) ["expiryDays", "allowedDownloads"].forEach(name => {
  const el = form.elements.namedItem(name);
  if (el) el.addEventListener("change", () => {
    pendingOperationRef = null;
    window.invalidateToolOperation && window.invalidateToolOperation();
  });
});
if (!window.__TOOLPAGE__) form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!window.__USER__) return location.href = "/auth/login";
  if (!file.files[0]) return;
  if (window.toolOperationBusy && window.toolOperationBusy()) return; // one operation per surface
  const isMulti = activeTool && activeTool.multi;
  submitBtn.disabled = true; // busy state = disabled dim; progress lives in the result area only
  showResult(`<div class="upload-progress"><div class="upload-progress-bar"><div class="upload-progress-fill" id="progressFill"></div></div><div class="upload-progress-text" id="progressText">0%</div></div>`);
  // Executable tools ride the shared process-flow authority (ONE upload→
  // execute→retry chain, ONE client_ref per logical operation, frozen
  // invocation for retries). app.js keeps only the surfaces: progress
  // paint, busy state, and the pass-through result card.
  const files = Array.from(file.files);
  const activeId = activeTool ? activeTool.id : null;
  try {
    if (activeTool && activeTool.id !== "share" && !activeTool.pass_through) {
      const result = await window.runToolFlow(activeTool, files, {
        params: Object.assign({}, activeToolParams || {}),
        expiryDays: (form.elements.namedItem("expiryDays") || {}).value ? parseInt(form.elements.namedItem("expiryDays").value) : 1,
        allowedDownloads: (form.elements.namedItem("allowedDownloads") || {}).value ? parseInt(form.elements.namedItem("allowedDownloads").value) : 50,
        onProgress: (pct, bps) => {
          const fill = document.getElementById("progressFill");
          const text = document.getElementById("progressText");
          const elapsed = ((Date.now() - (form.__t0 || Date.now())) / 1000).toFixed(1);
          const speed = bps > 0 && elapsed > 0 ? formatSpeed(pct > 0 ? pct * 1024 : 0, parseFloat(elapsed)) : "";
          if (fill) fill.style.width = pct + "%";
          if (text) text.textContent = t("uploading") + " · " + pct + "%" + (speed ? " · " + speed : "");
        },
        onError: (msg) => {
          window.showToolError && window.showToolError(msg, null, activeTool ? () =>
            window.retryLastToolOperation && window.retryLastToolOperation() : null);
        },
      });
      if (result && result.passthrough) {
        // pass-through handled by process-flow's share branch
      }
      form.reset();
      fileLabel.textContent = t("dropEmpty");
      pendingOperationRef = null;
      return;
    }
    // Pass-through share direct upload (NOT the tool operation identity —
    // share ingress has its own one-shot identity below).
    if (!pendingOperationRef) pendingOperationRef = newOperationRef();
    const formData = new FormData(form);
    formData.append("client_ref", pendingOperationRef);
    files.forEach((f) => formData.append("file", f));
    const startTime = Date.now();
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.timeout = 900000;
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
    const data = await new Promise((resolve, reject) => {
      xhr.onload = () => {
        try {
          const d = JSON.parse(xhr.responseText);
          if (xhr.status >= 400 || !d.ok) { const ek = d.error || "failed"; reject(new Error(t(ek) !== ek ? t(ek) : t("failed"))); return; }
          resolve(d);
        } catch (err) { reject(new Error(t("failed"))); }
      };
      xhr.onerror = () => reject(new Error(t("failed")));
      xhr.ontimeout = () => reject(new Error(t("failed")));
      xhr.send(formData);
    });
    if (data.files) {
      data.files.forEach((f, i) => setTimeout(() => showShareResult(f), i * 100));
    } else {
      showShareResult(data.file);
    }
    pendingOperationRef = null; // direct share complete — next submit is new
    form.reset();
    fileLabel.textContent = t("dropEmpty");
    window._loadHistory && window._loadHistory();
  } catch (err) {
    // upload/execute failure — process-flow retains the operation ONLY
    // for retryable failures (response loss / tool_failed); preflight and
    // validation failures invalidate it. The Retry button exists only
    // when a frozen operation can actually be replayed — never a dead
    // button.
    if (window.showToolError && activeTool && activeTool.id !== "share") {
      const cb = window.canRetryToolOperation && window.canRetryToolOperation()
        ? () => {
            const p = window.retryLastToolOperation && window.retryLastToolOperation();
            if (p && typeof p.catch === "function") p.catch(() => {});
          }
        : null;
      window.showToolError(err.message, null, cb);
    } else {
      showResult(escapeHtml(err.message), true);
    }
  } finally {
    submitBtn.disabled = false;
  }
});
// loadHistory moved to footer.html (shared nav behavior)
function renderModal(upload) {
  modalTitle.textContent = t("qrCode");
  modalMeta.innerHTML = `<span><strong>${escapeHtml(upload.file_name)}</strong></span><span>${t("expiresLabel")}<strong>${fmtDate(upload.expire_at)}</strong></span>`;
  // VIS-007: no 1–2s blank panel — explicit loading state until the SVG
  // has actually loaded; error state if it never does.
  modalQr.classList.add("qr-loading");
  modalQr.removeAttribute("src");
  modalQr.onload = () => modalQr.classList.remove("qr-loading", "qr-error");
  modalQr.onerror = () => { modalQr.classList.remove("qr-loading"); modalQr.classList.add("qr-error"); };
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
if (!window.__TOOLPAGE__) modalClose.addEventListener("click", closeModal);
if (!window.__TOOLPAGE__) uploadModal.addEventListener("click", event => {
  if (event.target === uploadModal) closeModal();
});
if (!window.__TOOLPAGE__) modalCopy.addEventListener("click", async () => {
  if (!activeUpload) return;
  await navigator.clipboard.writeText(activeUpload.url_download);
  modalCopy.textContent = t("copied");
});
async function deleteUpload(upload, triggerButton = null) {
  if (!upload) return;
  // Site-wide confirm authority (VIS-006): modal confirm, no native dialog.
  const ok = await window.confirmAction({
    title: t("delete"),
    message: t("confirmDelete"),
    confirmLabel: t("delete"),
    cancelLabel: t("cancel"),
  });
  if (!ok) return;
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
if (!window.__TOOLPAGE__) modalDelete.addEventListener("click", () => deleteUpload(activeUpload, modalDelete));

// ── Home Tools ──────────────────────────────────────────────────────────────
const homeTools = document.getElementById("homeTools");
const toolOverlay = document.getElementById("toolOverlay");
const toolOverlayBody = document.getElementById("toolOverlayBody");
const toolOverlayClose = document.getElementById("toolOverlayClose");
// 4C-2: 删除 ACCEPT_TAGS MIME 手写镜像 — Catalog 已输出 canonical extension
// token (".pdf"), 展示统一用 extLabel (".jpeg"→"JPEG"); 偶发 legacy fallback
// token (含 "/") 简单取末段可读, 不再维护 MIME→格式全集。
function extLabel(tok) {
  const t = String(tok || "");
  const tail = t.indexOf("/") >= 0 ? t.split("/").pop() : t;
  return tail.replace(/^\./, "").toUpperCase();
}
let activeTool = null;
let activeToolParams = {};  // spec v3: 确认时收集的 params 值

// ── operation identity (client-generated, page-lifetime only) ──
// One user action = one stable client_ref shared by upload + execute +
// retry. Changed operation input (files/tool/params/delivery) drops the
// pending ref so the next submit generates a fresh one. No localStorage,
// no persistence: this only needs to survive a response-loss retry.
function newOperationRef() {
  const u = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID().replace(/-/g, "")
    : Array.from(crypto.getRandomValues(new Uint8Array(16)))
        .map(b => b.toString(16).padStart(2, "0")).join("");
  return "web:" + u;
}
// Web operation-ref generator — the ONLY one. Exposed for the shared
// process-flow entry so tool pages mint identity through this single
// source (no second UUID implementation anywhere).
window.newOperationRef = newOperationRef;
let pendingOperationRef = null;
let homeToolsData = [];

// 4A-fix3: 单点读取某 tool_id 保存的 canonical params (localStorage 权威源)。
// reload 恢复与浮层渲染共用此 helper — 参数读取逻辑只此一处, 杜绝分叉。
function savedToolParams(toolId) {
  try {
    const all = JSON.parse(localStorage.getItem("flashshare_tool_params") || "{}");
    const v = all && typeof all === "object" ? all[toolId] : undefined;
    return (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
  } catch (e) {
    return {};
  }
}

// 4A-fix3b: 无配置工具的直接选择 — tab 直选 / 搜索 share / 搜索无配置工具
// 共用同一 commit 语义 (一次性提交 + 清 params + 写 selection storage)。
// 有配置工具必须走 openToolOverlay 事务语义: Confirm 才 commit, Cancel/X/
// backdrop 不改当前 committed selection。
function commitToolSelection(tool) {
  activeTool = tool;
  activeToolParams = {};
  pendingOperationRef = null; // operation input changed
  localStorage.setItem("flashshare_tool", tool.id);
  applyToolSelection();
}

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
      // 直选规则: 无 params 声明 → 不弹浮层; 有 params → openToolOverlay。
      // share 由此规则自然直选 — 无工具名特判。
      const noInput = !(Array.isArray(tool.params) && tool.params.length);
      if (noInput) {
        // 4A-fix3b: 无配置工具直接选择 — 与搜索路径共用 commitToolSelection
        commitToolSelection(tool);
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
    // reload 恢复保存的 canonical params — 保存过的参数必须真实
    // 用于下一次 execute。
    activeToolParams = savedToolParams(savedTool.id);
    applyToolSelection(true);
  }

  initToolSearch();
}

// ── Tool search (search-as-select; 518-native, not a directory page) ──
let _searchTimer = null;
let _searchIdx = -1;

function initToolSearch() {
  const input = document.getElementById("toolSearch");
  const pop = document.getElementById("toolSearchPop");
  if (!input || !pop) return;

  const closePop = () => { pop.classList.add("hidden"); _searchIdx = -1; };

  const renderResults = (items) => {
    if (!items.length) {
      pop.innerHTML = `<div class="tool-search-empty">${escapeHtml(t("tool_search_none"))}</div>`;
      pop.classList.remove("hidden");
      return;
    }
    pop.innerHTML = items.map((x, i) => {
      const name = resolveLabel(x.label) || x.id;
      const cost = x.cost > 0 ? `<span class="tool-cost">${x.cost}</span>` : "";
      return `<button type="button" class="tool-search-item" role="option" data-idx="${i}" data-tool="${escapeHtml(x.id)}">
        ${x.icon ? svgIcon(x.icon) : ''}<span class="tool-tab-name">${escapeHtml(name)}</span>${cost}
      </button>`;
    }).join("");
    pop.classList.remove("hidden");
    _searchIdx = -1;
    pop.querySelectorAll(".tool-search-item").forEach(btn => {
      btn.addEventListener("click", () => {
        const tool = homeToolsData.find(t => t.id === btn.dataset.tool)
          || _lastSearchResults.find(t => t.id === btn.dataset.tool);
        if (tool) selectToolFromSearch(tool);
        closePop();
      });
    });
  };

  const selectToolFromSearch = (tool) => {
    // 4A-fix3b: 与 tab 路径同一事务语义 — 有配置工具只打开 overlay, 不改
    // 当前 committed selection; Confirm 才 commit (activeTool/
    // activeToolParams/localStorage 一次性提交)。无配置工具直接选择。
    const noInput = !(Array.isArray(tool.params) && tool.params.length);
    if (noInput) {
      commitToolSelection(tool);
      closePop();
      return;
    }
    openToolOverlay(tool, null);
    input.value = "";
    closePop();
  };

  input.addEventListener("input", () => {
    clearTimeout(_searchTimer);
    const q = input.value.trim();
    if (!q) { closePop(); return; }
    _searchTimer = setTimeout(async () => {
      try {
        const r = await fetch(`/api/tools/search?q=${encodeURIComponent(q)}`);
        const d = await r.json();
        if (d.ok) { _lastSearchResults = d.tools; renderResults(d.tools); }
      } catch (e) { /* offline: keep pop closed */ }
    }, 180);
  });
  input.addEventListener("keydown", (e) => {
    const items = [...pop.querySelectorAll(".tool-search-item")];
    if (e.key === "ArrowDown" && items.length) {
      e.preventDefault();
      _searchIdx = Math.min(_searchIdx + 1, items.length - 1);
    } else if (e.key === "ArrowUp" && items.length) {
      e.preventDefault();
      _searchIdx = Math.max(_searchIdx - 1, 0);
    } else if (e.key === "Enter") {
      if (_searchIdx >= 0 && items[_searchIdx]) { e.preventDefault(); items[_searchIdx].click(); }
      return;
    } else if (e.key === "Escape") {
      closePop();
      return;
    } else return;
    items.forEach((el, i) => el.classList.toggle("active", i === _searchIdx));
  });
  document.addEventListener("click", (e) => {
    if (!pop.contains(e.target) && e.target !== input) closePop();
  });
}

let _lastSearchResults = [];

// ── Tool Overlay (shared component) ─────────────────────────

function openToolOverlay(tool, btn) {
  const toolName = resolveLabel(tool.label) || tool.id;
  const iconSvg = tool.icon ? svgIcon(tool.icon) : '';
  const isActive = activeTool && activeTool.id === tool.id;

  // Build overlay content
  let html = `<div class="tool-overlay-head"><svg viewBox="0 0 24 24" aria-hidden="true">${iconSvg}</svg><h3>${escapeHtml(toolName)}</h3></div>`;

  // Description: 系统工具走 i18n {id}_desc; 工具自述 desc 双通道兼容 (spec v3)
  const desc = resolveLabel(tool.desc);
  if (desc) {
    html += `<p class="tool-overlay-desc">${escapeHtml(desc)}</p>`;
  }

  // Metered cost badge (single catalog source: window.__TOOLS__ carries cost)
  if (typeof tool.cost === "number" && tool.cost > 0) {
    html += `<p class="tool-overlay-cost"><span class="tool-cost">${tool.cost}</span> ${t("credits_per_call")}</p>`;
  }

  // Params controls (tool modes v3: enum 分段/boolean 开关/number 步进/text 输入)
  // 4A-fix3: 渲染值来自单点 savedToolParams — 与 reload 恢复同一数据源
  const savedParams = savedToolParams(tool.id);
  if (Array.isArray(tool.params) && tool.params.length > 0) {
    // Params are presented by their own labels only — no generic
    // "参数/Options" grouping heading above them (the tool's own label in
    // the overlay head is the heading).
    html += `<div class="tool-overlay-section"><div class="tool-overlay-params">`;
    tool.params.forEach((p) => {
      const plabel = resolveLabel(p.label) || p.key;
      const val = savedParams[p.key] !== undefined ? savedParams[p.key] : p.default;
      const pid = `tp_${p.key}`;
      if (p.type === "enum") {
        html += `<div class="tool-overlay-param"><div class="tool-param-label">${escapeHtml(plabel)}</div><div class="tool-overlay-presets">`;
        (p.options || []).forEach((o) => {
          const olabel = resolveLabel(o.label) || o.value;
          const sel = (val === o.value) ? " active" : "";
          html += `<button type="button" class="tool-overlay-preset tp-option${sel}" data-param="${p.key}" data-value="${escapeHtml(o.value)}">${escapeHtml(olabel)}</button>`;
        });
        html += `</div></div>`;
      } else if (p.type === "boolean") {
        const on = val === true || val === "true";
        html += `<div class="tool-overlay-param tool-param-row"><label class="tool-param-label" for="${pid}">${escapeHtml(plabel)}</label><input type="checkbox" id="${pid}" class="tp-bool" data-param="${p.key}"${on ? " checked" : ""}></div>`;
      } else if (p.type === "number") {
        const mn = p.min !== undefined ? ` min="${p.min}"` : "";
        const mx = p.max !== undefined ? ` max="${p.max}"` : "";
        html += `<div class="tool-overlay-param tool-param-row"><label class="tool-param-label" for="${pid}">${escapeHtml(plabel)}</label><input type="number" id="${pid}" class="tp-number" data-param="${p.key}"${mn}${mx} value="${val !== undefined && val !== null ? escapeHtml(String(val)) : ""}"></div>`;
      } else {
        const ph = p.placeholder ? escapeHtml(resolveLabel(p.placeholder)) : "";
        html += `<div class="tool-overlay-param tool-param-row"><label class="tool-param-label" for="${pid}">${escapeHtml(plabel)}</label><input type="text" id="${pid}" class="tp-text" data-param="${p.key}" placeholder="${ph}" value="${val !== undefined && val !== null ? escapeHtml(String(val)) : ""}"></div>`;
      }
    });
    html += `</div></div>`;
  }

  // Actions — always show confirm/cancel, even when active
  html += `<div class="tool-overlay-actions"><button type="button" class="overlay-btn muted" id="overlayCancel">${isActive ? t("cancelTool") : t("cancel")}</button><button type="button" class="overlay-btn primary" id="overlayConfirm">${t("confirm")}</button></div>`;

  toolOverlayBody.innerHTML = html;
  toolOverlay.classList.remove("hidden");

  // Params enum 分段选择
  const paramOptions = toolOverlayBody.querySelectorAll(".tp-option");
  paramOptions.forEach(obtn => {
    obtn.addEventListener("click", () => {
      paramOptions.forEach(b => { if (b.dataset.param === obtn.dataset.param) b.classList.remove("active"); });
      obtn.classList.add("active");
    });
  });

  // Confirm
  const confirmBtn = document.getElementById("overlayConfirm");
  if (confirmBtn) {
    confirmBtn.addEventListener("click", () => {
      activeTool = tool;
      // 收集 params 控件值 (spec v3: enum 分段/bool/number/text)
      const collected = {};
      if (Array.isArray(tool.params)) {
        tool.params.forEach((p) => {
          if (p.type === "enum") {
            const on = toolOverlayBody.querySelector(`.tp-option[data-param="${p.key}"].active`);
            if (on) collected[p.key] = on.dataset.value;
          } else if (p.type === "boolean") {
            const el = document.getElementById(`tp_${p.key}`);
            if (el) collected[p.key] = el.checked;
          } else {
            const el = document.getElementById(`tp_${p.key}`);
            if (el && el.value !== "") {
              collected[p.key] = p.type === "number" ? Number(el.value) : el.value;
            }
          }
        });
      }
      activeToolParams = collected;
      try {
        const all = JSON.parse(localStorage.getItem("flashshare_tool_params") || "{}");
        all[tool.id] = collected;
        localStorage.setItem("flashshare_tool_params", JSON.stringify(all));
      } catch (e) { /* storage full/blocked — non-fatal */ }
      localStorage.setItem("flashshare_tool", tool.id);
      applyToolSelection(true);
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
        activeToolParams = {};
        if (shareTool) {
          localStorage.setItem("flashshare_tool", "share");
        } else {
          localStorage.removeItem("flashshare_tool");
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

if (!window.__TOOLPAGE__) toolOverlayClose.addEventListener("click", closeToolOverlay);
if (!window.__TOOLPAGE__) toolOverlay.addEventListener("click", (e) => { if (e.target === toolOverlay) closeToolOverlay(); });

// Apply tool selection to the upload zone (首页形态专属 — 工具页无表单)
function applyToolSelection(skipStateReset) {
  if (window.__TOOLPAGE__) return;
  // Full state reset on tool switch (not on params-only re-confirm)
  if (!skipStateReset) {
    file.value = "";
    fileLabel.textContent = t("dropEmpty");
  }
  result.classList.add("hidden");
  lastErrorKey = null;
  window.invalidateToolOperation && window.invalidateToolOperation(); // input changed → fresh ref next run
  document.body.dataset.tool = activeTool ? activeTool.id : "";

  const formatTags = document.querySelector(".drop-formats");

  // Clear all badges + set active state on ALL tabs
  document.querySelectorAll(".tool-tab").forEach(b => {
    const isActive = activeTool && b.dataset.toolId === activeTool.id;
    b.classList.toggle("active", isActive);
    const oldBadge = b.querySelector(".tool-preset-badge");
    if (oldBadge) oldBadge.remove();
  });

  if (isPassThrough(activeTool)) {
    file.accept = "";
    if (formatTags) formatTags.textContent = t("dropFormats");
    updateToolDescription();
    return;
  }

  // Bind accept — 4C-2: canonical exact extension token list 直绑,
  // 不再把同族 MIME 放宽成 image/* (exact contract 不得被 UI 扩大)
  if (activeTool.accept && activeTool.accept.length > 0) {
    file.accept = activeTool.accept.join(",");
  } else {
    file.removeAttribute("accept");
  }

  // Bind multiple (multi-file tools like pdf_merge)
  if (activeTool.multi) {
    file.setAttribute("multiple", "");
  } else {
    file.removeAttribute("multiple");
  }

  // Update format tags — spans with wrapping (flex-wrap chips)
  if (formatTags && activeTool.accept) {
    const tags = [...new Set(activeTool.accept.map(extLabel).filter(Boolean))];
    formatTags.innerHTML = tags.map(tg => `<span class="fmt-chip">${tg}</span>`).join("");
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
  const desc = resolveLabel(activeTool.desc);
  if (desc) {
    descEl.textContent = desc;
    descEl.classList.remove("hidden");
  } else {
    descEl.classList.add("hidden");
  }
}

function showToolProcessing(toolName) {
  showResult(`<div class="tool-processing"><div class="upload-spinner"></div><span>${escapeHtml(toolName)} · ${t("processing")}</span></div>`);
}

// Task-2 error contract: explicit retry callback decides whether a Retry
// button exists at all — no dead buttons. The message is already
// user-facing (process-flow maps unknown codes to t("failed")); machine
// error codes are never rendered raw.
function showToolError(message, originalFile, retryCallback) {
  const origUrl = originalFile && (originalFile.downloadUrl || originalFile.url) ? (originalFile.downloadUrl || originalFile.url) : "";
  const origLink = origUrl ? `<a class="overlay-btn primary" href="${escapeHtml(origUrl)}" target="_blank">${t("useOriginal")}</a>` : "";
  const retryBtn = typeof retryCallback === "function"
    ? `<button class="overlay-btn" id="toolRetry" type="button">${t("retry")}</button>` : "";
  showResult(`<div class="result-card"><div class="result-error">${escapeHtml(message)}</div><div class="result-buttons">${retryBtn}${origLink}</div></div>`, true);
  const btn = document.getElementById("toolRetry");
  if (btn && typeof retryCallback === "function") {
    btn.addEventListener("click", retryCallback);
  }
}

// Stable icon paths via unified registry

async function shareFile(url, filename, btn) {
  // Delegates to the site-wide share authority (base-engine.js).
  // Kept as a thin alias so existing call sites stay unchanged.
  if (window.shareFileViaURL) return window.shareFileViaURL(url, filename, btn);
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
  lastErrorKey = null;
  const url = file.downloadUrl || file.url;
  const dlUrl = url + (url.includes("?") ? "&" : "?") + "download=1";
  const share = "https://x.com/intent/tweet?" + new URLSearchParams({ text: t("shareText"), url });
  const copyId = `copy-${Date.now()}`;
  const qrId = `qr-${Date.now()}`;
  const shareImgId = `shareimg-${Date.now()}`;
  const zipBtnId = `zipbtn-${Date.now()}`;
  const meta = fmtSize(file.size);
  // PWA detection authority is base-engine.js window.isStandalonePWA —
  // app.js never re-declares it (a same-name top-level function here
  // shadowed the engine property and recursed infinitely).
  const pwa = window.isStandalonePWA();

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

  // Same rule as the server inline/detail embeddable check: raster /
  // inlineable images auto-preview; image/svg+xml never embeds via
  // /inline (server rejects it — no client-side second SVG preview).
  const isImg = file.contentType
      && file.contentType.startsWith("image/")
      && file.contentType.toLowerCase() !== "image/svg+xml";
  const inlineUrl = `/f/${file.publicId}/inline`;
  const preview = isImg ? `<div class="result-preview"><img src="${escapeHtml(inlineUrl)}" alt="${escapeHtml(file.filename || file.name || "")}" loading="lazy" decoding="async"></div>` : "";
  const zipCt = file.contentType || "";
  const zipName = file.filename || file.name || "";
  const isZip = !!file.publicId && (/\.zip$/i.test(zipName) || zipCt === "application/zip" || zipCt === "application/x-zip-compressed");
  if (isZip) {
    actions = `<button class="action-btn" id="${zipBtnId}" type="button" aria-label="${t("zip_preview_btn")}">${svgIcon("eye")}<span data-i18n="zip_preview_btn">${t("zip_preview_btn")}</span></button>` + actions;
  }
  const isPdf = !!file.publicId && (zipCt === "application/pdf" || /\.pdf$/i.test(zipName));
  if (isPdf) {
    actions = `<a class="action-btn" href="/f/${file.publicId}/print" target="_blank" rel="noopener" aria-label="${t("print_btn")}">${svgIcon("print")}<span data-i18n="print_btn">${t("print_btn")}</span></a>` + actions;
  }

  const header = isImg ? "" :
    `<div class="result-check">${svgIcon("check")}</div>` +
    `<div class="result-card-title" data-i18n="ready">${t("ready")}</div>`;
  const metaLine = isImg ? "" : `<div class="result-meta">${escapeHtml(meta)}</div>`;

  showResult(
    `<div class="result-card">` +
    header +
    preview +
    metaLine +
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
  const zipBtn = document.getElementById(zipBtnId);
  if (zipBtn) ensureZipPreview(() => window.zipPreview && window.zipPreview.wireRemotePreview(zipBtn, file.publicId));
  // PWA standalone: download goes through the site-wide authority
  // (base-engine.js pwaDownload — URL share / secondary context; the
  // current PWA window is never navigated).
  if (pwa) {
    const dlBtn = document.querySelector('.result-card .action-btn.primary');
    if (dlBtn) dlBtn.addEventListener("click", () => {
      window.pwaDownload && window.pwaDownload(dlUrl, file.filename || file.name || "download");
    });
  }
  const shareImgBtn = document.getElementById(shareImgId);
  if (shareImgBtn) shareImgBtn.addEventListener("click", async () => {
    const ok = await shareFile(dlUrl, file.filename || file.name || "download", shareImgBtn);
    if (!ok) window.open(dlUrl, "_blank", "noopener");
  });
}

// Load tools on page load
if (!window.__TOOLPAGE__) loadHomeTools();
window.applyI18n();
