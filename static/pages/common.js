// ── shared page helpers (single source; every pages/*.js may use) ──
window._msg = k => { const v = window.t(k); return (v && v !== k) ? v : k; };
// Unified user-visible error boundary (#42):
//   known code → specific localized message
//   unknown err_* / machine code / any non-code diagnostic → the
//   canonical localized generic failure (err_unexpected)
// A raw err_* key, 'error' or exception text is never returned.
window._errm = code => {
  if (code === null || code === undefined || code === "") return window.t("err_unexpected");
  code = String(code);
  // Platform i18n layer (protocol error codes are _msg keys, NOT LocalizedText —
  // user-declared text resolves via resolveLabel, never through this path).
  if (window._msg(code) !== code) return window._msg(code);
  const k = 'mytools_err_' + code;
  if (window._msg(k) !== k) return window._msg(k);
  const k2 = 'err_' + code;
  if (window._msg(k2) !== k2) return window._msg(k2);
  // unknown machine code — canonical localized generic failure, never raw
  return window.t("err_unexpected");
};
// Standalone-PWA download rebinding (single source): inside an installed
// PWA an <a download> navigates the window and traps the user, so every
// result/file surface routes its download links through the engine
// authority (window.pwaDownload — URL share / secondary-context handoff,
// never a body fetch, never a window navigation). Outside standalone this
// is a no-op: the plain SSR anchors keep working without JS.
window.wirePwaDownloadLinks = function (scope) {
  if (!scope || !scope.querySelectorAll) return;
  if (!(typeof window.isStandalonePWA === "function" && window.isStandalonePWA())) return;
  scope.querySelectorAll('a[href*="download=1"]').forEach((a) => {
    const url = a.getAttribute("href") || "";
    const name = (a.textContent || "download").split("·")[0].trim() || "download";
    a.removeAttribute("href");
    a.removeAttribute("download");
    a.setAttribute("role", "button");
    a.addEventListener("click", () => {
      window.pwaDownload && window.pwaDownload(url, name);
    });
  });
};

// Mobile top-bar navigation sheet: visible only at narrow widths (CSS),
// toggled by the menu button; closes on backdrop / outside click / Escape /
// nav. The backdrop is the touch-friendly outside.
window.wireMobileNav = function () {
  const btn = document.getElementById("mobileNavBtn");
  const sheet = document.getElementById("mobileNavSheet");
  const backdrop = document.getElementById("mobileNavBackdrop");
  if (!btn || !sheet || btn.dataset.wired) return;
  btn.dataset.wired = "1";
  const setOpen = (open) => {
    sheet.classList.toggle("hidden", !open);
    if (backdrop) backdrop.classList.toggle("hidden", !open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(sheet.classList.contains("hidden"));
  });
  if (backdrop) backdrop.addEventListener("click", () => setOpen(false));
  document.addEventListener("click", (e) => {
    if (!sheet.contains(e.target) && e.target !== btn) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setOpen(false);
  });
  sheet.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => setOpen(false)));
};
document.addEventListener("DOMContentLoaded", () => window.wireMobileNav());

// ── Image zoom (single site-wide lightbox authority) ──────────────
// Result grids (inline result card + /r result page) open a full-size
// view on click: one overlay element, image untouched (no fetch, no
// re-render), click anywhere / Escape closes. Long-press save on the
// underlying grid cells is untouched (the overlay only covers click).
window.wireImageZoom = function (scope) {
  const root = scope || document;
  if (!root.querySelectorAll) return;
  root.querySelectorAll("[data-zoom]").forEach((btn) => {
    if (btn.dataset.zoomWired) return;
    btn.dataset.zoomWired = "1";
    btn.addEventListener("click", () => {
      let layer = document.getElementById("imgZoom");
      if (!layer) {
        layer = document.createElement("div");
        layer.id = "imgZoom";
        layer.className = "img-zoom hidden";
        layer.setAttribute("role", "dialog");
        layer.setAttribute("aria-modal", "true");
        layer.innerHTML = "<img alt=\"\">";
        layer.addEventListener("click", () => layer.classList.add("hidden"));
        document.addEventListener("keydown", (e) => {
          if (e.key === "Escape") layer.classList.add("hidden");
        });
        document.body.appendChild(layer);
      }
      const src = btn.getAttribute("data-zoom") || "";
      const img = layer.querySelector("img");
      if (img && src) {
        img.src = src;
        img.alt = btn.getAttribute("aria-label") || "";
        layer.classList.remove("hidden");
      }
    });
  });
};

// ── Text result links (tool page result + /r page, one implementation) ──
// Tool-returned text is untrusted: built with DOM nodes only, and only
// https:// URLs become links (new tab, noopener/noreferrer/nofollow).
// A text item that is exactly one https URL becomes a link card (domain
// shown prominently); the <pre> keeps the raw text for copy.
// Returns the number of single-link cards created.
window.linkifyResultText = function (root) {
  const URL_RE = /https:\/\/[^\s<>"'`]+/g;
  const TRAIL = /[.,;:!?)\]}'"。，；：！？）】」』]+$/;
  const safeUrl = (raw) => {
    try {
      const u = new URL(raw);
      return u.protocol === "https:" ? u : null;
    } catch (e) { return null; }
  };
  const linkEl = (u, text) => {
    const a = document.createElement("a");
    a.href = u.href;
    a.target = "_blank";
    a.rel = "noopener noreferrer nofollow";
    a.textContent = text;
    return a;
  };
  let cards = 0;
  (root || document).querySelectorAll("pre.rs-text").forEach((pre) => {
    if (pre.dataset.linked) return;
    pre.dataset.linked = "1";
    const text = pre.textContent;
    const whole = text.trim();
    const single = /^https:\/\/\S+$/.test(whole) ? safeUrl(whole) : null;
    if (single) {
      const item = pre.closest(".rs-item");
      const card = linkEl(single, "");
      card.className = "rs-link-card";
      const host = document.createElement("b");
      host.textContent = single.hostname;
      const full = document.createElement("small");
      full.textContent = whole;
      const go = document.createElement("span");
      go.className = "btn btn-p btn-sm";
      go.textContent = window.t ? window.t("open") : "Open";
      card.append(host, full, go);
      pre.hidden = true;
      pre.parentNode.insertBefore(card, pre);
      if (item) item.classList.add("rs-link");
      cards += 1;
      return;
    }
    const frag = document.createDocumentFragment();
    let last = 0;
    let found = false;
    text.replace(URL_RE, (m, idx) => {
      const trimmed = m.replace(TRAIL, "");
      const u = safeUrl(trimmed);
      if (!u) return m;
      found = true;
      frag.append(text.slice(last, idx), linkEl(u, trimmed));
      last = idx + trimmed.length;
      return m;
    });
    if (!found) return;
    frag.append(text.slice(last));
    pre.textContent = "";
    pre.append(frag);
  });
  return cards;
};
