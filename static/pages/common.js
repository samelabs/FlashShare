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

// Mobile top-bar navigation sheet: visible only at phone widths (CSS),
// toggled by the menu button; closes on outside click / Escape / nav.
window.wireMobileNav = function () {
  const btn = document.getElementById("mobileNavBtn");
  const sheet = document.getElementById("mobileNavSheet");
  if (!btn || !sheet || btn.dataset.wired) return;
  btn.dataset.wired = "1";
  const setOpen = (open) => {
    sheet.classList.toggle("hidden", !open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(sheet.classList.contains("hidden"));
  });
  document.addEventListener("click", (e) => {
    if (!sheet.contains(e.target) && e.target !== btn) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setOpen(false);
  });
  sheet.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => setOpen(false)));
};
document.addEventListener("DOMContentLoaded", () => window.wireMobileNav());
