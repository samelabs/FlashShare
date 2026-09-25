// ── 首页: 工具选择 × 共用输入组件 (tool-input.js) ────────────────
// 机制: 工具 tab/搜索直选 (无参数浮层 — 参数就在输入区里, 随声明呈现);
// 选中即 ToolInput.setTool 重渲染输入区。默认工具 = share。
// 选择持久化 localStorage("flashshare_tool"), 参数持久化在组件内。
(function () {
  "use strict";

  const mountEl = document.getElementById("toolInput");
  if (!mountEl || !window.ToolInput) return;
  const tools = window.__TOOLS__ || [];
  if (!tools.length) return;

  let activeTool = null;
  const ctrl = window.ToolInput.mount(mountEl, {});
  window.__toolInputRefresh = () => ctrl.rerender();

  function selectTool(tool, opts) {
    opts = opts || {};
    activeTool = tool;
    if (!opts.restore) {
      try { localStorage.setItem("flashshare_tool", tool.id); } catch (e) {}
    }
    document.body.dataset.tool = tool.id;
    ctrl.setTool(tool);
    // Tab active state + description line follow the selection
    document.querySelectorAll(".tool-tab").forEach(b => {
      b.classList.toggle("active", b.dataset.toolId === tool.id);
    });
    const descEl = document.getElementById("toolDesc");
    if (descEl) {
      const desc = window.resolveLabel(tool.desc);
      if (desc) { descEl.textContent = desc; descEl.classList.remove("hidden"); }
      else descEl.classList.add("hidden");
    }
    window.invalidateToolOperation && window.invalidateToolOperation();
  }
  window.__homeSelectTool = selectTool;

  function bindTabs() {
    const homeTools = document.getElementById("homeTools");
    if (!homeTools) return;
    homeTools.querySelectorAll(".tool-tab").forEach(tab => {
      tab.addEventListener("click", () => {
        const tool = tools.find(t => t.id === tab.dataset.toolId);
        if (tool) selectTool(tool);
      });
    });
  }

  // ── Tool search (search-as-select) ──
  let _searchTimer = null;
  let _searchIdx = -1;
  let _lastSearchResults = [];

  function initToolSearch() {
    const input = document.getElementById("toolSearch");
    const pop = document.getElementById("toolSearchPop");
    if (!input || !pop) return;
    const t = (k) => window.t(k);
    const esc = window.escapeHtml || String;

    const closePop = () => { pop.classList.add("hidden"); _searchIdx = -1; };

    const renderResults = (items) => {
      if (!items.length) {
        pop.innerHTML = `<div class="tool-search-empty">${esc(t("tool_search_none"))}</div>`;
        pop.classList.remove("hidden");
        return;
      }
      pop.innerHTML = items.map((x, i) => {
        const name = window.resolveLabel(x.label) || x.id;
        const cost = x.cost > 0 ? `<span class="tool-cost">${x.cost}</span>` : "";
        return `<button type="button" class="tool-search-item" role="option" data-idx="${i}" data-tool="${esc(x.id)}">
          ${x.icon ? window.iconSvg(x.icon) : ''}<span class="tool-tab-name">${esc(name)}</span>${cost}
        </button>`;
      }).join("");
      pop.classList.remove("hidden");
      _searchIdx = -1;
      pop.querySelectorAll(".tool-search-item").forEach(btn => {
        btn.addEventListener("click", () => {
          const tool = tools.find(t => t.id === btn.dataset.tool)
            || _lastSearchResults.find(t => t.id === btn.dataset.tool);
          if (tool) selectTool(tool);
          closePop();
        });
      });
    };

    input.addEventListener("input", () => {
      clearTimeout(_searchTimer);
      const q = input.value.trim();
      if (!q) { closePop(); return; }
      _searchTimer = setTimeout(async () => {
        try {
          const r = await fetch(`/api/tools/search?q=${encodeURIComponent(q)}`);
          const d = await r.json();
          if (d.ok) { _lastSearchResults = d.tools; renderResults(d.tools); }
        } catch (e) { /* offline: keep pop closed */ }
      }, 180);
    });
    input.addEventListener("keydown", (e) => {
      const items = [...pop.querySelectorAll(".tool-search-item")];
      if (e.key === "ArrowDown" && items.length) {
        e.preventDefault();
        _searchIdx = Math.min(_searchIdx + 1, items.length - 1);
      } else if (e.key === "ArrowUp" && items.length) {
        e.preventDefault();
        _searchIdx = Math.max(_searchIdx, 0);
      } else if (e.key === "Enter") {
        if (_searchIdx >= 0 && items[_searchIdx]) { e.preventDefault(); items[_searchIdx].click(); }
        return;
      } else if (e.key === "Escape") {
        closePop();
        return;
      } else return;
      items.forEach((el, i) => el.classList.toggle("active", i === _searchIdx));
    });
    document.addEventListener("click", (e) => {
      if (!pop.contains(e.target) && e.target !== input) closePop();
    });
  }

  // Tool names follow language switches (data-driven labels)
  const _after = window.__afterI18n;
  window.__afterI18n = function () {
    if (typeof _after === "function") _after();
    document.querySelectorAll("#homeTools .tool-tab").forEach(tab => {
      const tool = tools.find(t => t.id === tab.dataset.toolId);
      if (!tool) return;
      const nameEl = tab.querySelector(".tool-tab-name");
      if (nameEl) nameEl.textContent = window.resolveLabel(tool.label) || tool.id;
    });
  };

  bindTabs();
  initToolSearch();

  // Restore last selection (default = share)
  const saved = localStorage.getItem("flashshare_tool") || "share";
  const savedTool = tools.find(t => t.id === saved) || tools.find(t => t.id === "share") || tools[0];
  selectTool(savedTool, { restore: true });
})();
