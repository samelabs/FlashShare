window.__ZIP_PID__ = __PAGE__.publicId;

// Copy link
const copyBtn = document.getElementById("copyBtn");
const landingUrl = document.getElementById("landingUrl");
copyBtn.addEventListener("click", async () => {
  await navigator.clipboard.writeText(landingUrl.value);
  const orig = copyBtn.innerHTML;
  copyBtn.innerHTML = window.iconSvg("check");
  copyBtn.style.color = "var(--accent)";
  copyBtn.style.borderColor = "var(--accent)";
  setTimeout(() => { copyBtn.innerHTML = orig; copyBtn.style.color = ""; copyBtn.style.borderColor = ""; }, 1400);
});

const _pwa = window.isStandalonePWA ? window.isStandalonePWA()
  : (window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true);
const dlBtn = document.querySelector('a.btn.btn-primary[download]');
if (dlBtn && _pwa) {
  dlBtn.removeAttribute('href');
  dlBtn.addEventListener('click', () => {
    // Site-wide PWA download authority (base-engine.js): URL share /
    // secondary-context handoff — never fetches the body and never
    // navigates the current PWA window.
    window.pwaDownload && window.pwaDownload(__PAGE__.dlUrl, __PAGE__.fileName);
  });
}
const shareImgBtn = document.getElementById("shareImgBtn");
shareImgBtn.addEventListener("click", () => {
  // Explicit share-file action: site-wide authority (canShare gate; a
  // standalone-PWA failure never navigates the current window).
  window.shareFileViaURL && window.shareFileViaURL(__PAGE__.dlUrl, __PAGE__.fileName, shareImgBtn);
});

const qrBtn = document.getElementById("qrBtn");
const qrModal = document.getElementById("qrModal");
const qrModalClose = document.getElementById("qrModalClose");
qrBtn.addEventListener("click", () => qrModal.classList.remove("hidden"));
qrModalClose.addEventListener("click", () => qrModal.classList.add("hidden"));
qrModal.addEventListener("click", (e) => { if (e.target === qrModal) qrModal.classList.add("hidden"); });

const wsContainer = document.getElementById("workspaceTools");
const pid = wsContainer ? wsContainer.dataset.pid : "";
const toolModal = document.getElementById("toolModal");
const toolModalBody = document.getElementById("toolModalBody");
function openToolModal(html) { toolModalBody.innerHTML = html; toolModal.classList.remove("hidden"); }
function closeToolModal() { toolModal.classList.add("hidden"); toolModalBody.innerHTML = ""; }
document.getElementById("toolModalClose").addEventListener("click", closeToolModal);
toolModal.addEventListener("click", (e) => { if (e.target === toolModal) closeToolModal(); });
// Keyboard contract: Escape closes the visible toolModal (close button
// and backdrop already close it — this is the one missing cancel path).
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !toolModal.classList.contains("hidden")) closeToolModal();
});

function toolLoading() { return '<div class="tool-loading">' + window.t("loading") + '</div>'; }
function toolError(msg) {
  // unified error boundary (common.js _errm): known code → localized
  // message, anything else (incl. exception text) → generic failure
  msg = _errm(msg);
  msg = String(msg).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  return '<p class="tool-error">' + msg + '</p>';
}

const renameBtn = document.getElementById("renameBtn");
if (renameBtn) renameBtn.addEventListener("click", () => openRename(pid));
const deleteBtn = document.getElementById("deleteBtn");
if (deleteBtn) deleteBtn.addEventListener("click", () => openDelete(pid));

const zipPreviewBtn = document.getElementById("zipPreviewBtn");
if (zipPreviewBtn) {
  const s = document.createElement("script");
  // versioned like every other static script (same template stamp)
  s.src = "/static/zip-preview.js?v=" + encodeURIComponent(__PAGE__.pagesVersion || "");
  s.onload = () => window.zipPreview && window.zipPreview.wireRemotePreview(zipPreviewBtn, window.__ZIP_PID__);
  document.head.appendChild(s);
}

function openRename(pid) {
  const h1 = document.getElementById("fileName");
  const oldName = h1.textContent.trim();
  const dotIdx = oldName.lastIndexOf(".");
  const stem = dotIdx > 0 ? oldName.substring(0, dotIdx) : oldName;
  openToolModal('<div class="tool-panel"><h3>' + window.t("rename") + '</h3><input type="text" id="renameInput" value="' + stem.replace(/"/g, '&quot;') + '" maxlength="200"><div class="tool-panel-actions"><button class="btn btn-primary btn-sm" id="renameConfirm">' + (window.iconSvg ? window.iconSvg("check") : "✓") + '</button></div></div>');
  const input = document.getElementById("renameInput");
  input.focus(); input.select();
  document.getElementById("renameConfirm").addEventListener("click", async () => {
    const v = input.value.trim();
    if (!v || v === stem) { closeToolModal(); return; }
    openToolModal(toolLoading());
    try {
      const r = await fetch(`/api/uploads/${pid}`, { method:"PATCH", headers:{"Content-Type":"application/json"}, body: JSON.stringify({name:v}) });
      const data = await r.json();
      if (data.ok) { h1.textContent = data.name; document.title = data.name; closeToolModal(); }
      else openToolModal(toolError(data.error));
    } catch(e) { openToolModal(toolError(e.message)); }
  });
}

function openDelete(pid) {
  openToolModal('<div class="tool-panel"><h3>' + window.t("delete") + '</h3><p class="tool-confirm-text">' + window.t("confirmDelete") + '</p><div class="tool-panel-actions"><button class="btn btn-secondary btn-sm" id="delCancel">' + (window.iconSvg ? window.iconSvg("x") : "✕") + '</button><button class="btn btn-primary btn-sm" id="delConfirm">' + (window.iconSvg ? window.iconSvg("check") : "✓") + '</button></div></div>');
  document.getElementById("delCancel").addEventListener("click", closeToolModal);
  document.getElementById("delConfirm").addEventListener("click", async () => {
    openToolModal(toolLoading());
    try {
      const r = await fetch(`/api/uploads/${pid}`, { method:"DELETE" });
      const data = await r.json();
      if (data.ok) window.location.href = "/";
      else openToolModal(toolError(data.error));
    } catch(e) { openToolModal(toolError(e.message)); }
  });
}
