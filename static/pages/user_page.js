// 用户页 (开发者公开面): 快捷使用 — 工具是入口无安装, 卡片内一控件
// 选文件即执行 (默认参数), 与工具页/首页同一 process-flow 单源。
(function () {
  "use strict";

  const tools = window.__OWNER_TOOLS__ || [];
  const esc = window.escapeHtml || (s => String(s || "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])));
  const t = k => window.t(k);

  tools.forEach(tool => {
    const card = document.querySelector(`.userpage-card[data-tool-id="${CSS.escape(tool.id)}"]`);
    if (!card) return;
    const actions = card.querySelector(".userpage-card-actions");
    if (!actions) return;

    const wrap = document.createElement("span");
    wrap.className = "userpage-quick";
    const inputId = `uq_${tool.id.replace(/[^a-zA-Z0-9_]/g, "_")}`;
    wrap.innerHTML =
      `<input type="file" id="${inputId}" class="visually-hidden"` +
      (tool.accept ? ` accept="${tool.accept.map(a => esc(a)).join(",")}"` : "") +
      (tool.multi ? " multiple" : "") + `>` +
      `<label class="userpage-quick-btn" for="${inputId}">` +
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 17v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2"/></svg>` +
      `<span>${esc(t("tool_run_pick"))}</span></label>` +
      `<span class="toolpage-file-name uq-name"></span>`;
    actions.appendChild(wrap);

    const input = wrap.querySelector("input");
    const btn = wrap.querySelector(".userpage-quick-btn");
    const nameEl = wrap.querySelector(".uq-name");
    input.addEventListener("change", async () => {
      const files = input.files;
      if (!files || !files.length) return;
      if (!window.__USER__) { location.href = "/auth/login"; return; }
      nameEl.textContent = files.length > 1 ? `${files.length} files` : files[0].name;
      btn.classList.add("busy");
      // 快捷使用 = 默认参数 (preset 默认项/params 默认值); 深度调整走工具页
      let preset = null;
      if (Array.isArray(tool.presets) && tool.presets.length) {
        preset = (tool.presets.find(p => p.default) || tool.presets[0]).id;
      }
      const params = {};
      if (Array.isArray(tool.params)) {
        tool.params.forEach(p => {
          if (p.default !== undefined) params[p.key] = p.default;
        });
      }
      try {
        const r = await window.runToolFlow(tool, files, { preset, params });
        if (r) nameEl.textContent = "";
      } catch (e) {
        window.showResult && window.showResult(esc(e.message), true);
        nameEl.textContent = "";
      } finally {
        btn.classList.remove("busy");
        input.value = "";
      }
    });
  });
})();
