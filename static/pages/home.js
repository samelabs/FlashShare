// ── 首页 (v2): composer 默认挂载分享, 底栏工具选择器 + 快捷胶囊 ──
// 选择器与快捷胶囊都复用 window.__TOOLS__; 选中后 ToolInput.setTool
// 原地切换 (不跳转); 执行链与 tool 页同一 runToolFlow 单源。
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
    renderQuick();
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
    pop.setAttribute("role", "listbox");
    bar.insertBefore(btn, bar.firstChild);
    bar.insertBefore(pop, btn.nextSibling);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = pop.classList.contains("hidden");
      pop.classList.toggle("hidden", !open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      renderPickerList();
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => { pop.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); });
  }

  function renderPickerList() {
    const pop = mountEl.querySelector(".tsel-pop");
    if (!pop) return;
    const esc = window.escapeHtml || String;
    pop.innerHTML = tools.map((tool) => {
      const on = activeTool && activeTool.id === tool.id;
      return '<button type="button" role="option" aria-selected="' + on + '" class="tsel-item' + (on ? " on" : "") + '" data-tool="' + esc(tool.id) + '">'
        + (tool.icon ? window.iconSvg(tool.icon) : "") + "<span>" + esc(toolName(tool)) + "</span></button>";
    }).join("");
    pop.querySelectorAll(".tsel-item").forEach((b) => {
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

  // ── 快捷胶囊: 排序最前的 5 个非分享工具 ──
  function renderQuick() {
    const wrap = document.getElementById("quickTools");
    if (!wrap) return;
    const esc = window.escapeHtml || String;
    const picks = tools.filter((t) => t.id !== "share").slice(0, 5);
    wrap.innerHTML = picks.map((tool) =>
      '<a class="quick-item" href="#" role="button" data-tool="' + esc(tool.id) + '">'
      + (tool.icon ? window.iconSvg(tool.icon) : "") + esc(toolName(tool)) + "</a>").join("");
    wrap.querySelectorAll(".quick-item").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        const tool = tools.find((t) => t.id === a.dataset.tool);
        if (tool) {
          selectTool(tool);
          mountEl.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
    });
  }

  // ── init: 默认分享 (显式保存的选择优先) ──
  const saved = (() => { try { return localStorage.getItem("flashshare_tool") || ""; } catch (e) { return ""; } })();
  const savedTool = tools.find((t) => t.id === saved);
  const share = tools.find((t) => t.id === "share");
  selectTool(savedTool || share || tools[0], { skipPersist: !savedTool });
})();
