// ── Result page (/r/{id}) enhancements ─────────────────────────
// The page is server-rendered readable (text/JSON shown, files linked
// — no JS required). This file wires the progressive actions only:
// copy text / copy JSON / download all.
// Result expiry is enforced server-side (expired → 404), so every
// element this script touches only exists on a live result.
(function () {
  "use strict";

  const t = (k) => window.t(k);

  function flash(btn, icon) {
    const orig = btn.innerHTML;
    btn.innerHTML = window.iconSvg(icon);
    btn.style.color = "var(--success)";
    setTimeout(() => {
      btn.innerHTML = orig;
      btn.style.color = "";
    }, 1400);
  }

  async function copyText(value, btn) {
    try {
      await navigator.clipboard.writeText(value);
      flash(btn, "check");
    } catch (e) { /* clipboard blocked — SSR content stays selectable */ }
  }

  window.linkifyResultText && window.linkifyResultText(document);

  // Per-item copy: text items copy their text, JSON items copy the
  // pretty-printed JSON exactly as rendered.
  document.querySelectorAll("[data-copy-text], [data-copy-json]").forEach((btn) => {
    const item = btn.closest(".rs-item");
    const pre = item ? item.querySelector(".rs-text, .rs-json") : null;
    btn.addEventListener("click", () => copyText(pre ? pre.textContent : "", btn));
  });

  // Copy link / system share / X live in the share bar (base-engine.js).

  // Download all: sequential per-file downloads. Each file already has
  // its own download=1 link (browser-native download, own expiry and
  // counter); no server-side packaging is minted for a bundle.
  const allBtn = document.getElementById("rDownloadAll");
  if (allBtn) {
    // Installed-PWA mode hides the bundle action: opening one secondary
    // context per file gets popup-blocked; per-file downloads remain.
    if (window.isStandalonePWA && window.isStandalonePWA()) {
      allBtn.style.display = "none";
    }
    allBtn.addEventListener("click", () => {
      // Both download shapes: plain anchors AND the PWA-rebound
      // buttons (wirePwaDownloadLinks strips their href — an
      // anchor-only selector would find nothing in standalone mode).
      const links = Array.from(document.querySelectorAll('.rs-item .rs-dl'));
      links.forEach((a, i) => {
        setTimeout(() => a.click(), i * 400);
      });
    });
  }

  // Standalone PWA: download anchors route through the engine authority
  // (common.js wirePwaDownloadLinks — no in-window navigation).
  window.wirePwaDownloadLinks && window.wirePwaDownloadLinks(document);
  // Image grid cells open the full-size view (same lightbox as the
  // inline result card — common.js single authority).
  window.wireImageZoom && window.wireImageZoom(document);
})();
