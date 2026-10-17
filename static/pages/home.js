// ── 首页 (v3): composer 默认挂载分享, 底栏工具选择器 (顶部过滤输入) ──
// 选择器列表 = window.__TOOLS__ (工作集: 官方 + 自己的 + 收藏); 输入查询
// 时追加公开搜索命中 (/api/tools/search, 与市场/MCP catalog_search 同一
// 谓词)。选中后 ToolInput.setTool 原地切换 (不跳转); 执行链与 tool 页
// 同一 runToolFlow 单源。
(function () {
  "use strict";

  const mountEl = document.getElementById("toolInput");
  if (!mountEl || !window.ToolInput) return;
  const tools = window.__TOOLS__ || [];
  if (!tools.length) return;

  let ctrl = null;
  let activeTool = null;
  // 公开搜索命中 (仅对应 searchQ 这一查询串有效)
  let searchQ = "";
  let searchHits = [];
  let searchTimer = null;

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
      q.addEventListener("input", () => { renderPickerList(); scheduleSearch(q.value); });
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

  function scheduleSearch(raw) {
    clearTimeout(searchTimer);
    const q = raw.trim();
    if (!q) return;
    searchTimer = setTimeout(() => {
      fetch("/api/tools/search?q=" + encodeURIComponent(q))
        .then((r) => r.json())
        .then((d) => {
          const qEl = mountEl.querySelector(".tsel-filter input");
          if (!qEl || qEl.value.trim() !== q) return;   // 过期查询
          searchQ = q;
          searchHits = (d && d.tools) || [];
          renderPickerList();
        })
        .catch(() => {});
    }, 200);
  }

  function renderPickerList() {
    const pop = mountEl.querySelector(".tsel-pop");
    if (!pop) return;
    const list = pop.querySelector(".tsel-list");
    const qEl = pop.querySelector(".tsel-filter input");
    const raw = qEl ? qEl.value : "";
    const tokens = raw.toLowerCase().split(/\s+/).filter(Boolean);
    const esc = window.escapeHtml || String;
    const matches = tools.filter((tool) => {
      const hay = pickerHaystack(tool);
      return tokens.every((tok) => hay.indexOf(tok) >= 0);
    });
    if (tokens.length && searchQ === raw.trim()) {
      const seen = new Set(matches.map((m) => m.id));
      searchHits.forEach((h) => { if (!seen.has(h.id)) matches.push(h); });
    }
    list.innerHTML = matches.length ? matches.map((tool) => {
      const on = activeTool && activeTool.id === tool.id;
      return '<button type="button" role="option" aria-selected="' + on + '" class="tsel-item' + (on ? " on" : "") + '" data-tool="' + esc(tool.id) + '">'
        + (tool.icon ? window.iconSvg(tool.icon) : "") + "<span>" + esc(toolName(tool)) + "</span></button>";
    }).join("") : '<div class="tsel-empty" role="status">' + esc(window.t("tool_search_none")) + "</div>";
    list.querySelectorAll(".tsel-item").forEach((b) => {
      b.addEventListener("click", () => {
        const tool = tools.find((t) => t.id === b.dataset.tool)
          || searchHits.find((t) => t.id === b.dataset.tool);
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

  // "Continue processing" (/r owner action): ?from=<result_id> brings that
  // result's files into the composer as references (no re-upload).
  const fromResult = new URLSearchParams(location.search).get("from");
  if (fromResult && ctrl && ctrl.addFiles) {
    fetch("/api/results/" + encodeURIComponent(fromResult) + "/files")
      .then((r) => r.json())
      .then((d) => {
        if (!d || !d.ok || !d.files.length) return;
        ctrl.addFiles(d.files.map((f) => ({
          ref: true, publicId: f.public_id, name: f.filename,
          size: f.size, type: f.content_type,
        })));
        history.replaceState(null, "", location.pathname);
      })
      .catch(() => {});
  }
})();

// ── R2: hero 活终端 — 真实调用序列打字机回放 (纯展示, aria-hidden;
//   尊重 prefers-reduced-motion: 直接呈现终态) ──
(function () {
  "use strict";
  const body = document.getElementById("heroTermBody");
  if (!body) return;
  const base = (window.__BASE_URL__ || location.origin);
  const SEQ = [
    { text: "# 一次真实的调用", cls: "c", pause: 500 },
    { text: "claude mcp add --transport http 518 " + base + "/mcp", cls: "k", pause: 900 },
    { text: "→ tools/call  share", cls: "", pause: 1100 },
    { text: "result_url: " + base + "/r/qDNuKPEM", cls: "g", pause: 0 },
  ];
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    body.innerHTML = SEQ.map((l) => `<span class="${l.cls}">${l.text}</span>`).join("<br>");
    return;
  }
  let line = 0, ch = 0;
  let spans = [];
  function tick() {
    if (line >= SEQ.length) {
      setTimeout(() => { body.innerHTML = ""; spans = []; line = 0; ch = 0; tick(); }, 5200);
      return;
    }
    const cur = SEQ[line];
    if (ch === 0) { spans[line] = document.createElement("span"); spans[line].className = cur.cls; body.appendChild(spans[line]); }
    ch += 1;
    spans[line].textContent = cur.text.slice(0, ch);
    if (ch >= cur.text.length) { line += 1; ch = 0; setTimeout(tick, Math.max(cur.pause, 120)); }
    else setTimeout(tick, 24 + Math.random() * 40);
  }
  tick();
})();
