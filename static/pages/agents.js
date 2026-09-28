// ── agents page: client-config tab switching ────────────────────
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
