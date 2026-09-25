// ── Result page (/r/{id}) enhancements ─────────────────────────
// The page is server-rendered readable (text/JSON shown, files linked
// — no JS required). This file wires the progressive actions only:
// copy text / copy JSON / copy link / QR / print / download all.
// Result expiry is enforced server-side (expired → 404), so every
// element this script touches only exists on a live result.
(function () {
  "use strict";

  const t = (k) => window.t(k);

  function flash(btn, icon) {
    const orig = btn.innerHTML;
    btn.innerHTML = window.iconSvg(icon);
    btn.style.color = "var(--accent)";
    btn.style.borderColor = "var(--accent)";
    setTimeout(() => {
      btn.innerHTML = orig;
      btn.style.color = "";
      btn.style.borderColor = "";
    }, 1400);
  }

  async function copyText(value, btn) {
    try {
      await navigator.clipboard.writeText(value);
      flash(btn, "check");
    } catch (e) { /* clipboard blocked — SSR content stays selectable */ }
  }

  // Per-item copy: text items copy their text, JSON items copy the
  // pretty-printed JSON exactly as rendered.
  document.querySelectorAll("[data-copy-text]").forEach((btn) => {
    const pre = btn.closest(".r-item").querySelector(".r-text");
    btn.addEventListener("click", () => copyText(pre ? pre.textContent : "", btn));
  });
  document.querySelectorAll("[data-copy-json]").forEach((btn) => {
    const pre = btn.closest(".r-item").querySelector(".r-json");
    btn.addEventListener("click", () => copyText(pre ? pre.textContent : "", btn));
  });

  // Copy result link
  const copyLink = document.getElementById("rCopyLink");
  if (copyLink) {
    copyLink.addEventListener("click", () => copyText(window.location.href, copyLink));
  }

  // QR modal
  const qrBtn = document.getElementById("rQrBtn");
  const qrModal = document.getElementById("rQrModal");
  if (qrBtn && qrModal) {
    qrBtn.addEventListener("click", () => qrModal.classList.remove("hidden"));
    const close = document.getElementById("rQrModalClose");
    if (close) close.addEventListener("click", () => qrModal.classList.add("hidden"));
    qrModal.addEventListener("click", (e) => {
      if (e.target === qrModal) qrModal.classList.add("hidden");
    });
  }

  // Print
  const printBtn = document.getElementById("rPrintBtn");
  if (printBtn) printBtn.addEventListener("click", () => window.print());

  // Download all: sequential per-file downloads. Each file already has
  // its own download=1 link (browser-native download, own expiry and
  // counter); no server-side packaging is minted for a bundle.
  const allBtn = document.getElementById("rDownloadAll");
  if (allBtn) {
    allBtn.addEventListener("click", () => {
      const links = Array.from(document.querySelectorAll(
        '.r-file-row a[href$="?download=1"]'));
      links.forEach((a, i) => {
        setTimeout(() => a.click(), i * 400);
      });
    });
  }

  // Standalone PWA: download anchors route through the engine authority
  // (common.js wirePwaDownloadLinks — no in-window navigation).
  window.wirePwaDownloadLinks && window.wirePwaDownloadLinks(document);
})();
