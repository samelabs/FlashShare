// ── 工具页: 单工具上下文 × 共用输入组件 (tool-input.js) ─────────
// 机制 (IO-2): 页面自述 (费用/次数/格式/窗口) 是 SSR 事实; 输入区与
// 首页同一组件, 随本工具声明呈现 (参数/文件窗口/格式/交付)。
// 执行走全局 process-flow (runToolFlow 单源); 结果呈现走 app.js
// 共享底座 (renderResultItems — 与 /r 结果页同构)。
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
// MCP name copy (page-head copyfield)
(function () {
  const btn = document.getElementById("toolMcpCopy");
  const field = document.getElementById("toolMcpName");
  if (btn && field) {
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(field.value);
        const span = btn.querySelector("span");
        const orig = span.textContent;
        span.textContent = window.t("copied");
        setTimeout(() => { span.textContent = orig; }, 1400);
      } catch (e) { /* clipboard blocked — value stays selectable */ }
    });
  }
})();
