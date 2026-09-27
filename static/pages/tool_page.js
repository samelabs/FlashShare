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

// MCP name copy (the AI-call-name line at the bottom of the page)
(function () {
  const btn = document.getElementById("toolMcpCopy");
  const code = document.getElementById("toolMcpName");
  if (btn && code) {
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        const span = btn.querySelector("span");
        const orig = span.textContent;
        span.textContent = window.t("copied");
        setTimeout(() => { span.textContent = orig; }, 1400);
      } catch (e) { /* clipboard blocked — value stays selectable */ }
    });
  }
})();
