// ── agents page: client-config tabs + copy buttons ───────────────
// Copy wiring serves both /agents and the tool MCP page (every
// button[data-copy-target] copies the text of the element whose id
// it names; feedback text comes from i18n).
function _copyText(btn, text) {
  // the one in-place confirmation (base-engine): busy-guarded, synced
  window.copyFeedback(btn);
  navigator.clipboard.writeText(text).catch(() => {});
}
document.querySelectorAll("button[data-copy-target]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const src = document.getElementById(btn.dataset.copyTarget);
    if (src) _copyText(btn, src.textContent);
  });
});

// client-config tabs
(function () {
  "use strict";
  var tabs = document.querySelectorAll(".agents-tabs [data-tab]");
  if (!tabs.length) return;
  tabs.forEach(function (btn) {
    btn.addEventListener("click", function () {
      tabs.forEach(function (b) { b.classList.toggle("on", b === btn); });
      document.querySelectorAll(".tab-panel").forEach(function (p) {
        p.classList.toggle("hidden", p.dataset.panel !== btn.dataset.tab);
      });
    });
  });
})();
