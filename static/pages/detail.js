window.__ZIP_PID__ = __PAGE__.publicId;

// Copy link
const copyBtn = document.getElementById("copyBtn");
const landingUrl = document.getElementById("landingUrl");
copyBtn.addEventListener("click", async () => {
  await navigator.clipboard.writeText(landingUrl.value);
  const orig = copyBtn.innerHTML;
  copyBtn.innerHTML = '__PAGE__.icons.check';
  copyBtn.style.color = "var(--accent)";
  copyBtn.style.borderColor = "var(--accent)";
  setTimeout(() => { copyBtn.innerHTML = orig; copyBtn.style.color = ""; copyBtn.style.borderColor = ""; }, 1400);
});

const _pwa = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
const dlBtn = document.querySelector('a.action-btn.primary[download]');
if (dlBtn && _pwa) {
  dlBtn.removeAttribute('href');
  dlBtn.addEventListener('click', async () => {
    const url = __PAGE__.dlUrl;
    const fname = __PAGE__.fileName;
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error('fetch failed');
      const blob = await res.blob();
      const file = new File([blob], fname || 'download', { type: blob.type || 'application/octet-stream' });
      if (navigator.share) {
        await navigator.share({ files: [file], title: fname || 'download' });
        return;
      }
    } catch(e) {
      if(e.name === 'AbortError') return;
      console.warn('download failed:', e.message);
    }
    window.location.href = __PAGE__.dlUrl;
  });
}
const shareImgBtn = document.getElementById("shareImgBtn");
shareImgBtn.addEventListener("click", async () => {
  const imgUrl = __PAGE__.dlUrl;
  const fname = __PAGE__.fileName;
  try {
    const res = await fetch(imgUrl);
    if (!res.ok) throw new Error("fetch failed");
    const blob = await res.blob();
    const file = new File([blob], fname || "download", { type: blob.type || "application/octet-stream" });
    if (navigator.share) {
      await navigator.share({ files: [file], title: fname || "download" });
      return;
    }
  } catch(e) {
    if(e.name === 'AbortError') return;
    console.warn('share failed:', e.message);
  }
  window.location.href = __PAGE__.dlUrl;
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

function toolLoading() { return '<div class="tool-loading">window.t("loading")</div>'; }
const _errMap = window.__I18N__ ? (window.__I18N__[window.__LANG] || {}) : {};
function toolError(msg) {
  if (msg && _errMap[msg]) msg = _errMap[msg];
  msg = msg || _errMap.tool_failed || "error";
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
  s.src = "/static/zip-preview.js";
  s.onload = () => window.zipPreview && window.zipPreview.wireRemotePreview(zipPreviewBtn, window.__ZIP_PID__);
  document.head.appendChild(s);
}

function openRename(pid) {
  const h1 = document.getElementById("fileName");
  const oldName = h1.textContent.trim();
  const dotIdx = oldName.lastIndexOf(".");
  const stem = dotIdx > 0 ? oldName.substring(0, dotIdx) : oldName;
  openToolModal('<div class="tool-panel"><h3>window.t("rename")</h3><input type="text" id="renameInput" value="' + stem.replace(/"/g, '&quot;') + '" maxlength="200"><div class="tool-panel-actions"><button class="action-btn primary" id="renameConfirm">✓</button></div></div>');
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
  openToolModal('<div class="tool-panel"><h3>window.t("delete")</h3><p class="tool-confirm-text">window.t("confirmDelete")</p><div class="tool-panel-actions"><button class="action-btn" id="delCancel">✕</button><button class="action-btn primary" id="delConfirm">✓</button></div></div>');
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
