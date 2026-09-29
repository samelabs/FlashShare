// ── 首页 (v3): composer 默认挂载分享, 底栏工具选择器 (顶部过滤输入) ──
// 选择器复用 window.__TOOLS__; 选中后 ToolInput.setTool 原地切换 (不跳
// 转); 执行链与 tool 页同一 runToolFlow 单源。卡片/市场搜索负责发现面,
// 首页不再有快捷胶囊。
(function () {
  "use strict";

  const mountEl = document.getElementById("toolInput");
  if (!mountEl || !window.ToolInput) return;
  const tools = window.__TOOLS__ || [];
  if (!tools.length) return;

  let ctrl = null;
  let activeTool = null;

  function mountInput(tool) {
    if (!ctrl) {
      ctrl = window.ToolInput.mount(mountEl, { tool: tool });
      window.__toolInputRefresh = () => ctrl && ctrl.rerender();
    } else {
      ctrl.setTool(tool);
    }
    activeTool = tool;
    document.body.dataset.tool = tool.id;
    const res = document.getElementById("result");
    if (res) { res.classList.add("hidden"); res.innerHTML = ""; res.classList.remove("has-result"); }
    window.invalidateToolOperation && window.invalidateToolOperation();
  }

  function selectTool(tool, opts) {
    opts = opts || {};
    mountInput(tool);
    ensurePicker();
    if (!opts.skipPersist) {
      try { localStorage.setItem("flashshare_tool", tool.id); } catch (e) {}
    }
    updatePickerLabel();
  }

  function toolName(tool) { return window.resolveLabel(tool.label) || tool.id; }

  // ── 底栏工具选择器: 由 ToolInput 渲染完成后在 c-bar 里注入胶囊 ──
  function ensurePicker() {
    const bar = mountEl.querySelector(".c-bar");
    if (!bar) return;
    if (bar.querySelector(".tsel")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tsel ti-picker";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML = '<span class="tic" aria-hidden="true"></span><span class="tsel-name"></span>'
      + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
    const pop = document.createElement("div");
    pop.className = "tsel-pop hidden";
    pop.innerHTML = '<div class="tsel-filter">'
      + '<input type="search" autocomplete="off" role="combobox" aria-controls="tselList" aria-expanded="false"'
      + ' placeholder="' + escAttr(window.t("tool_search_ph")) + '" aria-label="' + escAttr(window.t("tool_search_ph")) + '"></div>'
      + '<div class="tsel-list" id="tselList" role="listbox" aria-label="' + escAttr(window.t("tool_search_ph")) + '"></div>';
    bar.insertBefore(btn, bar.firstChild);
    bar.insertBefore(pop, btn.nextSibling);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = pop.classList.contains("hidden");
      pop.classList.toggle("hidden", !open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        renderPickerList();
        const q = pop.querySelector(".tsel-filter input");
        if (q) { q.value = ""; if (window.matchMedia && window.matchMedia("(min-width: 768px)").matches) q.focus(); }
      }
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    const q = pop.querySelector(".tsel-filter input");
    if (q) {
      q.addEventListener("input", renderPickerList);
      q.addEventListener("keydown", (e) => { if (e.key === "Escape") { pop.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); } });
    }
    document.addEventListener("click", () => { pop.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); });
  }

  function escAttr(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  // 过滤谓词与市场搜索同一规则: 分词 AND, 命中 名称/说明/id — 名称与
  // 说明索引全部语言值 (同后端 label_json/desc_json::text LIKE)。
  function pickerHaystack(tool) {
    return [tool.id, tool.label, tool.desc]
      .map((v) => (v && typeof v === "object") ? JSON.stringify(v).toLowerCase()
                                                  : String(v || "").toLowerCase())
      .join("\n");
  }

  function renderPickerList() {
    const pop = mountEl.querySelector(".tsel-pop");
    if (!pop) return;
    const list = pop.querySelector(".tsel-list");
    const qEl = pop.querySelector(".tsel-filter input");
    const tokens = (qEl ? qEl.value : "").toLowerCase().split(/\s+/).filter(Boolean);
    const esc = window.escapeHtml || String;
    const matches = tools.filter((tool) => {
      const hay = pickerHaystack(tool);
      return tokens.every((tok) => hay.indexOf(tok) >= 0);
    });
    list.innerHTML = matches.length ? matches.map((tool) => {
      const on = activeTool && activeTool.id === tool.id;
      return '<button type="button" role="option" aria-selected="' + on + '" class="tsel-item' + (on ? " on" : "") + '" data-tool="' + esc(tool.id) + '">'
        + (tool.icon ? window.iconSvg(tool.icon) : "") + "<span>" + esc(toolName(tool)) + "</span></button>";
    }).join("") : '<div class="tsel-empty" role="status">' + esc(window.t("tool_search_none")) + "</div>";
    list.querySelectorAll(".tsel-item").forEach((b) => {
      b.addEventListener("click", () => {
        const tool = tools.find((t) => t.id === b.dataset.tool);
        if (tool) selectTool(tool);
        pop.classList.add("hidden");
      });
    });
  }

  function updatePickerLabel() {
    const tic = mountEl.querySelector(".tsel .tic");
    const name = mountEl.querySelector(".tsel-name");
    if (tic) tic.innerHTML = activeTool && activeTool.icon ? window.iconSvg(activeTool.icon) : "";
    if (name) name.textContent = activeTool ? toolName(activeTool) : "";
  }

  // ── init: 默认分享 (显式保存的选择优先) ──
  const saved = (() => { try { return localStorage.getItem("flashshare_tool") || ""; } catch (e) { return ""; } })();
  const savedTool = tools.find((t) => t.id === saved);
  const share = tools.find((t) => t.id === "share");
  selectTool(savedTool || share || tools[0], { skipPersist: !savedTool });
})();
