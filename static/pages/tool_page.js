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

// MCP card copy: one click copies a ready-to-paste sentence carrying the
// MCP server URL and this tool's call name (prompt words + URL)
(function () {
  const btn = document.getElementById("toolMcpCopy");
  const url = document.querySelector(".mcpline .mcp-url");
  const name = document.getElementById("toolMcpName");
  if (btn && url && name) {
    btn.addEventListener("click", async () => {
      try {
        const payload = window.t("mcp_copy_text",
          { url: url.textContent.trim(), name: name.textContent.trim() });
        window.copyFeedback(btn);
        await navigator.clipboard.writeText(payload);
      } catch (e) { /* clipboard blocked — the card text stays selectable */ }
    });
  }
})();

// Page URL copy (the visible address row under the header)
(function () {
  const btn = document.getElementById("tpUrlCopy");
  const code = document.querySelector(".tp-url code");
  if (btn && code) {
    btn.addEventListener("click", async () => {
      try {
        window.copyFeedback(btn);
        await navigator.clipboard.writeText(code.textContent);
      } catch (e) { /* clipboard blocked — the URL stays selectable */ }
    });
  }
})();
