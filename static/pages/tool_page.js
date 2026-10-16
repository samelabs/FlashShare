// ── 工具页: 单工具上下文 × 共用输入组件 (tool-input.js) ─────────
// 机制 (IO-2): 页面头部自述 (作者/价格/输入/运行次数) 是 SSR 事实;
// 输入区与首页同一 composer, 随本工具声明呈现。执行走全局
// process-flow (runToolFlow 单源); 结果呈现走 app.js 共享底座
// (renderResultItems — 与 /r 结果页同构)。
(function () {
  "use strict";

  const tool = (window.__TOOLS__ || [])[0];
  if (!tool) return;

  const mountEl = document.getElementById("toolInput");
  if (!mountEl || !window.ToolInput) return;

  const ctrl = window.ToolInput.mount(mountEl, { tool: tool });
  window.__toolInputRefresh = () => ctrl.rerender();

  window.__TOOLPAGE__ = true;
})();

// Page URL copy (the visible address row under the header)
(function () {
  const btn = document.getElementById("tpUrlCopy");
  const code = document.querySelector(".tp-url code");
  if (btn && code) {
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        const ic = btn.querySelector("svg");
        if (!ic) return;
        const orig = ic.outerHTML;
        ic.outerHTML = window.iconSvg ? window.iconSvg("check") : "✓";
        setTimeout(() => {
          const now = btn.querySelector("svg") || btn.firstChild;
          if (now) now.outerHTML = orig;
        }, 1400);
      } catch (e) { /* clipboard blocked — the URL stays selectable */ }
    });
  }
})();
