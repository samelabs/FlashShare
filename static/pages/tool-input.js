// ── tool-input: the composer (首页 / 工具页同一组件, v2 视觉) ────
// 按所选工具的声明呈现: 主 text 参数 = 输入框正文, 其余参数进参数区
// (enum→分段按钮 / number→数字输入 / boolean→开关 / text→文本域);
// 文件位 (files_max>0): 拖放 / 回形针选择 / 粘贴, files_min=0 标"可选"。
// 校验走与后端同一报错边界 (统一内联提示, 不用 alert):
//   文件数越窗 → err_file_count {min}{max}; 格式 → err_format_not_supported
//   {formats}; 超单文件上限 → err_file_too_large {mb} (选择时前端预检,
//   超限不发请求);
//   必填参数 → param_required。
// Ctrl/Cmd+Enter 提交。执行走全局单源 window.runToolFlow
// (process-flow.js): files / params / delivery / client_ref 的字段、
// 校验与请求形状完全不变 — 本组件只管输入呈现与即时校验。
(function () {
  "use strict";

  // 平台格式白名单 (与上传入口同词表): accept=None 的工具仍只收
  // 平台白名单格式。ext token (小写、无点) → 场景族。
  const EXT_TO_TYPE = {
    jpg: "image", jpeg: "image", png: "image", gif: "image", webp: "image",
    bmp: "image", ico: "image", heic: "image", heif: "image", tiff: "image",
    tif: "image", avif: "image", svg: "image",
    pdf: "pdf",
    doc: "doc", docx: "doc", rtf: "doc", odt: "doc",
    xls: "sheet", xlsx: "sheet", ods: "sheet",
    ppt: "slides", pptx: "slides", odp: "slides",
    txt: "text", csv: "text", md: "text", json: "text", xml: "text",
    yaml: "text", yml: "text", py: "text", js: "text", mjs: "text",
    ts: "text", html: "text", htm: "text", css: "text", go: "text",
    rs: "text", java: "text", c: "text", cpp: "text", h: "text",
    rb: "text", php: "text", sh: "text", sql: "text",
    zip: "archive", rar: "archive", "7z": "archive", gz: "archive",
    tar: "archive", bz2: "archive", xz: "archive", tgz: "archive",
  };
  window.EXT_TO_TYPE = EXT_TO_TYPE;

  const t = (k, vars) => window.t(k, vars);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtSize = (n) => {
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(i ? 1 : 0) + " " + units[i];
  };
  const extOf = (name) => {
    const m = String(name || "").toLowerCase().match(/\.(\w+)$/);
    return m ? m[1] : "";
  };
  const resolveLabel = (l) => window.resolveLabel(l);
  const icon = (name) => window.iconSvg ? window.iconSvg(name) : "";

  // 参数持久化单点 (localStorage 权威, 与旧实现同键同语义)。
  function savedParams(toolId) {
    try {
      const all = JSON.parse(localStorage.getItem("flashshare_tool_params") || "{}");
      const v = all && typeof all === "object" ? all[toolId] : undefined;
      return (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
    } catch (e) { return {}; }
  }
  function persistParams(toolId, values) {
    try {
      const all = JSON.parse(localStorage.getItem("flashshare_tool_params") || "{}");
      all[toolId] = values;
      localStorage.setItem("flashshare_tool_params", JSON.stringify(all));
    } catch (e) { /* storage blocked — non-fatal */ }
  }

  /**
   * mount(container, opts) → controller
   * opts: {tool, onChange()} — tool = catalog 投影
   * controller: {setTool(tool), files(), el}
   */
  function mount(container, opts) {
    opts = opts || {};
    let tool = opts.tool || null;
    let files = [];
    const thumbUrls = [];

    function filesMin() { return tool ? Math.max(0, tool.files_min || 0) : 1; }
    // files_max=0 是显式"无文件位"(纯文本工具), 不能落进 ||1 缺省
    function filesMax() {
      if (!tool) return 1;
      return tool.files_max === 0 ? 0 : Math.min(20, tool.files_max || 1);
    }
    function acceptTokens() {
      return (tool && Array.isArray(tool.accept) && tool.accept.length)
        ? tool.accept.map((a) => String(a).replace(/^\./, "").toLowerCase())
        : null;
    }
    function hasFilesSlot() { return tool ? filesMax() > 0 : true; }
    // 主 text 参数 = 第一个 text 型参数 (输入框正文); 其余进参数区
    function mainTextParam() {
      const p = tool && Array.isArray(tool.params) ? tool.params : [];
      for (const prm of p) if (prm.type === "text") return prm;
      return null;
    }

    function showError(msg) {
      const line = container.querySelector(".ti-error");
      if (!line) return;
      line.textContent = msg;
      line.hidden = false;
    }
    function clearError() {
      const line = container.querySelector(".ti-error");
      if (line) { line.textContent = ""; line.hidden = true; }
    }

    // ── 渲染 ──
    function render() {
      const all = tool ? (Array.isArray(tool.params) ? tool.params : []) : [];
      const main = mainTextParam();
      const rest = main ? all.filter((p) => p !== main) : all.slice();
      const accept = acceptTokens();
      const saved = savedParams(tool ? tool.id : "");
      const cost = tool ? Math.max(0, parseInt(tool.cost || 0, 10) || 0) : 0;

      let html = '<div class="composer ti-card" data-tool="' + esc(tool ? tool.id : "") + '">'
        + '<p class="error-note ti-error" hidden></p>';

      // 文件位 (files_max>0): 空态 = 整块上传区 (点击/拖入), 有文件 = chip 列表
      if (hasFilesSlot()) {
        const countTxt = (filesMin() === filesMax() && filesMax() === 1)
          ? t("n_files_one")
          : t("files_window_short", { min: filesMin(), max: filesMax() });
        const fmtTxt = accept
          ? accept.map((a) => a.toUpperCase()).join(" · ")
          : t("ti_accept_common");
        const lim = window.__LIMITS__ || {};
        const segs = [countTxt, fmtTxt];
        if (lim.maxFileMb) segs.push(t("ti_zone_file_max", { mb: lim.maxFileMb }));
        if (lim.dailyFiles) segs.push(t("ti_zone_daily", { n: lim.dailyFiles, gb: lim.dailyGb }));
        html += '<div class="c-files"></div>'
          + '<button type="button" class="ti-drop" aria-label="' + esc(t("ti_zone_main")) + '">'
          + '<span class="dz-ic" aria-hidden="true">' + icon("upload") + "</span>"
          + "<b>" + esc(t("ti_zone_main")) + "</b>"
          + "<small>" + esc(segs.join(" · ")) + "</small>"
          + "</button>";
      }

      // 正文: 主 text 参数 (或纯文件工具的拖放提示文案)
      if (main) {
        const ph = main.placeholder ? esc(resolveLabel(main.placeholder))
          : esc(t("ti_composer_ph"));
        const val = saved[main.key] !== undefined ? saved[main.key] : (main.default || "");
        html += '<textarea class="c-text tp-text" data-param="' + esc(main.key)
          + '" rows="3" placeholder="' + ph + '">'
          + esc(val === undefined || val === null ? "" : String(val)) + "</textarea>";
      }

      // 参数区: 其余参数 (enum→seg / number / boolean / text)
      if (rest.length) {
        html += '<div class="params"><div class="divider"></div>';
        rest.forEach((prm) => {
          const label = resolveLabel(prm.label) || prm.key;
          const val = saved[prm.key] !== undefined ? saved[prm.key] : prm.default;
          if (prm.type === "enum") {
            html += '<div class="prow"><span class="prow-label">' + esc(label) + "</span>"
              + '<span class="seg tp-seg" data-param="' + esc(prm.key) + '">';
            (prm.options || []).forEach((o) => {
              html += '<button type="button" class="tp-option' + (val === o.value ? " on" : "")
                + '" data-value="' + esc(o.value) + '">'
                + esc(resolveLabel(o.label) || o.value) + "</button>";
            });
            html += "</span></div>";
          } else if (prm.type === "boolean") {
            html += '<div class="prow"><span class="prow-label">' + esc(label) + "</span>"
              + '<label class="switch"><input type="checkbox" class="tp-bool" data-param="' + esc(prm.key) + '"'
              + (val === true || val === "true" ? " checked" : "")
              + '><span class="switch-track"></span></label></div>';
          } else if (prm.type === "number") {
            const mn = prm.min !== undefined ? ' min="' + esc(prm.min) + '"' : "";
            const mx = prm.max !== undefined ? ' max="' + esc(prm.max) + '"' : "";
            html += '<div class="prow"><span class="prow-label">' + esc(label) + "</span>"
              + '<input type="number" class="input tp-number" data-param="' + esc(prm.key)
              + '"' + mn + mx + ' value="'
              + (val !== undefined && val !== null ? esc(String(val)) : "") + '"></div>';
          } else {
            const ph = prm.placeholder ? esc(resolveLabel(prm.placeholder)) : "";
            html += '<div class="prow" style="align-items:flex-start"><span class="prow-label" style="padding-top:8px">'
              + esc(label) + "</span>"
              + '<textarea class="textarea tp-text" data-param="' + esc(prm.key)
              + '" rows="2" style="flex:1;min-width:220px" placeholder="' + ph + '">'
              + (val !== undefined && val !== null ? esc(String(val)) : "") + "</textarea></div>";
          }
        });
        html += "</div>";
      }

      // 底栏: 回形针 + delivery 胶囊 + 发送 (免费=动作词, 收费=动作词 · N 积分)
      const del = window.__DELIVERY__ || { days: [1], downloads: [50], defaultDays: 1, defaultDownloads: 50 };
      const dLabel = "<b>" + del.defaultDays + " " + t(del.defaultDays === 1 ? "day_unit" : "days_unit")
        + "</b> · " + del.defaultDownloads + " " + t("times_unit");
      const runLabel = cost > 0
        ? esc(t("ti_run")) + " · " + cost + " " + esc(t("credits_short"))
        : esc(t("ti_run"));
      html += '<div class="c-bar">'
        + (hasFilesSlot()
          ? '<button type="button" class="ib ti-add" aria-label="' + esc(t("ti_add_files")) + '" title="' + esc(t("ti_add_files")) + '">'
          + icon("clip") + '<span class="ti-add-label">' + esc(t("ti_add_files")) + "</span></button>"
          : "")
        + '<button type="button" class="opt ti-del" aria-haspopup="true" aria-expanded="false">'
        + icon("expiry") + '<span class="ti-del-label">' + dLabel + "</span></button>"
        + '<div class="c-del-pop hidden">'
        + '<label class="field"><span class="field-label">' + esc(t("expiry")) + '</span>'
        + '<select class="select ti-expiry" data-delivery="expiry_days">'
        + del.days.map((d) => '<option value="' + d + '"' + (d === del.defaultDays ? " selected" : "") + ">"
          + d + " " + t(d === 1 ? "day_unit" : "days_unit") + "</option>").join("")
        + "</select></label>"
        + '<label class="field"><span class="field-label">' + esc(t("downloads")) + '</span>'
        + '<select class="select ti-downloads" data-delivery="allowed_downloads">'
        + del.downloads.map((n) => '<option value="' + n + '"' + (n === del.defaultDownloads ? " selected" : "") + ">"
          + n + " " + t("times_unit") + "</option>").join("")
        + "</select></label>"
        + "</div>"
        + '<span class="ti-progress" aria-live="polite"></span>'
        + '<span class="sp"></span>'
        + '<button type="button" class="send wide ti-run">' + runLabel + " " + icon("send") + "</button>"
        + "</div>";

      // 隐藏文件输入 (测试与无 JS 场景的稳定钩子)
      html += '<input type="file" class="visually-hidden ti-file-input"'
        + (accept ? ' accept="' + esc(accept.map((a) => "." + a).join(",")) + '"' : "")
        + (filesMax() > 1 ? " multiple" : "") + ">";

      html += "</div>";
      container.innerHTML = html;
      bindParams();
      bindFiles();
      bindDelivery();
      bindRun();
      renderRows();
    }

    // ── 参数控件 ──
    function bindParams() {
      container.querySelectorAll(".tp-option").forEach((btn) => {
        btn.addEventListener("click", () => {
          const seg = btn.closest(".seg");
          if (seg) seg.querySelectorAll(".tp-option").forEach((b) => b.classList.remove("on"));
          btn.classList.add("on");
          collectAndPersist();
          clearError();
          window.invalidateToolOperation && window.invalidateToolOperation();
        });
      });
      container.addEventListener("change", (e) => {
        if (e.target.matches(".tp-bool, .tp-number")) {
          collectAndPersist();
          clearError();
          window.invalidateToolOperation && window.invalidateToolOperation();
        }
      });
      container.addEventListener("input", (e) => {
        if (e.target.matches(".tp-text")) {
          collectAndPersist();
          clearError();
          window.invalidateToolOperation && window.invalidateToolOperation();
        }
      });
    }
    function collectParams() {
      const out = {};
      const p = tool && Array.isArray(tool.params) ? tool.params : [];
      p.forEach((prm) => {
        if (prm.type === "enum") {
          const seg = container.querySelector('.seg[data-param="' + prm.key + '"]');
          const on = seg && seg.querySelector(".tp-option.on");
          if (on) out[prm.key] = on.dataset.value;
        } else if (prm.type === "boolean") {
          const el = container.querySelector('.tp-bool[data-param="' + prm.key + '"]');
          if (el) out[prm.key] = el.checked;
        } else {
          const el = container.querySelector('.tp-' + (prm.type === "number" ? "number" : "text") + '[data-param="' + prm.key + '"]');
          if (el && el.value !== "") out[prm.key] = prm.type === "number" ? Number(el.value) : el.value;
        }
      });
      return out;
    }
    function collectAndPersist() {
      if (tool) persistParams(tool.id, collectParams());
    }

    // ── 文件 ──
    function fileInput() { return container.querySelector(".ti-file-input"); }

    function addFiles(list) {
      const incoming = Array.from(list || []);
      if (!incoming.length) return;
      const accept = acceptTokens();
      const max = filesMax();
      const maxBytes = ((window.__LIMITS__ || {}).maxFileMb || 50) * 1024 * 1024;
      let rejectedFormat = null, rejectedCount = false, rejectedSize = false;
      for (const f of incoming) {
        const ext = extOf(f.name);
        if (accept ? accept.indexOf(ext) === -1 : !EXT_TO_TYPE[ext]) {
          rejectedFormat = accept ? accept.map((a) => a.toUpperCase()).join(" · ") : t("ti_accept_common");
          continue;
        }
        if (f.size > maxBytes) { rejectedSize = true; continue; }
        if (files.length >= max) { rejectedCount = true; continue; }
        files.push(f);
      }
      if (rejectedFormat) {
        showError(t("err_format_not_supported", { formats: rejectedFormat }));
      } else if (rejectedSize) {
        showError(t("err_file_too_large", { mb: Math.round(maxBytes / 1024 / 1024) }));
      } else if (rejectedCount) {
        showError(t("err_file_count", { min: filesMin(), max: max }));
      } else {
        clearError();
      }
      renderRows();
      window.invalidateToolOperation && window.invalidateToolOperation();
      if (opts.onChange) opts.onChange(files.slice());
    }

    function removeFile(i) {
      files.splice(i, 1);
      renderRows();
      clearError();
      window.invalidateToolOperation && window.invalidateToolOperation();
      if (opts.onChange) opts.onChange(files.slice());
    }

    function renderRows() {
      const rows = container.querySelector(".c-files");
      if (!rows) return;
      thumbUrls.forEach((u) => URL.revokeObjectURL(u));
      thumbUrls.length = 0;
      const zone = container.querySelector(".ti-drop");
      if (zone) zone.hidden = files.length > 0;
      if (!files.length) { rows.innerHTML = ""; return; }
      rows.innerHTML = files.map((f, i) => {
        const isImg = /^image\//.test(f.type || "") && (f.type || "").toLowerCase() !== "image/svg+xml";
        let thumb;
        if (isImg) {
          const u = URL.createObjectURL(f);
          thumbUrls.push(u);
          thumb = '<img class="th" src="' + u + '" alt="" loading="lazy">';
        } else {
          const kind = EXT_TO_TYPE[extOf(f.name)] || "doc";
          thumb = '<span class="th d" aria-hidden="true">' + icon(kind === "text" ? "text" : kind) + "</span>";
        }
        return '<span class="chipf">' + thumb
          + "<span><b>" + esc(f.name) + "</b><small>"
          + (filesMin() === 0 ? esc(t("ti_files_optional")) + " · " : "")
          + esc(fmtSize(f.size)) + "</small></span>"
          + '<button type="button" class="x ti-row-remove" data-rm="' + i
          + '" aria-label="' + esc(t("ti_remove")) + '" title="' + esc(t("ti_remove")) + '">'
          + icon("close") + "</button></span>";
      }).join("");
      rows.querySelectorAll("[data-rm]").forEach((b) => {
        b.addEventListener("click", () => removeFile(+b.dataset.rm));
      });
    }

    function bindFiles() {
      const addBtns = container.querySelectorAll(".ti-add");
      const input = fileInput();
      addBtns.forEach((b) => b.addEventListener("click", () => input && input.click()));
      const zone = container.querySelector(".ti-drop");
      if (zone) zone.addEventListener("click", () => input && input.click());
      if (input) input.addEventListener("change", () => {
        addFiles(input.files);
        input.value = "";
      });
      // 拖放: 整个输入卡即落点; 空态上传区单独高亮
      const card = container.querySelector(".composer");
      if (card) {
        ["dragenter", "dragover"].forEach((n) => card.addEventListener(n, (e) => {
          e.preventDefault();
          card.classList.add("dragover", "focus");
          if (zone && !zone.hidden) zone.classList.add("dragover");
        }));
        ["dragleave", "drop"].forEach((n) => card.addEventListener(n, (e) => {
          e.preventDefault();
          card.classList.remove("dragover");
          if (zone) zone.classList.remove("dragover");
        }));
        card.addEventListener("drop", (e) => {
          if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
        });
        card.addEventListener("focusin", () => card.classList.add("focus"));
        card.addEventListener("focusout", () => card.classList.remove("focus"));
      }
      // 粘贴: 剪贴板里的文件 (截图等) 直接进附件
      document.addEventListener("paste", (e) => {
        if (!container.isConnected) return;
        const items = e.clipboardData && e.clipboardData.files;
        if (items && items.length) {
          const focused = document.activeElement;
          if (focused && focused.matches && focused.matches("input, textarea")) return; // 输入中不劫持
          addFiles(items);
        }
      });
    }

    // ── delivery 胶囊与浮层 ──
    function bindDelivery() {
      const btn = container.querySelector(".ti-del");
      const pop = container.querySelector(".c-del-pop");
      if (!btn || !pop) return;
      const close = () => { pop.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); };
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const open = pop.classList.contains("hidden");
        if (open) { pop.classList.remove("hidden"); btn.setAttribute("aria-expanded", "true"); }
        else close();
      });
      pop.addEventListener("click", (e) => e.stopPropagation());
      document.addEventListener("click", close);
      const upd = () => {
        const d = parseInt((container.querySelector(".ti-expiry") || {}).value, 10) || 1;
        const n = parseInt((container.querySelector(".ti-downloads") || {}).value, 10) || 50;
        const label = container.querySelector(".ti-del-label");
        if (label) label.innerHTML = "<b>" + d + " " + t(d === 1 ? "day_unit" : "days_unit")
          + "</b> · " + n + " " + t("times_unit");
      };
      container.querySelector(".ti-expiry") && container.querySelector(".ti-expiry").addEventListener("change", upd);
      container.querySelector(".ti-downloads") && container.querySelector(".ti-downloads").addEventListener("change", upd);
    }

    // ── 运行 ──
    function validateBeforeRun() {
      const p = tool && Array.isArray(tool.params) ? tool.params : [];
      for (const prm of p) {
        if (prm.type !== "text" && prm.type !== "number" && prm.type !== "enum") continue;
        if (!prm.required || prm.default != null) continue;
        const el = container.querySelector('.tp-' + (prm.type === "number" ? "number" : "text") + '[data-param="' + prm.key + '"]');
        if (prm.type === "enum") {
          const seg = container.querySelector('.seg[data-param="' + prm.key + '"]');
          const on = seg && seg.querySelector(".tp-option.on");
          if (!on) return t("param_required");
        } else if (!el || !el.value || !String(el.value).trim()) {
          return t("param_required");
        }
      }
      if (hasFilesSlot() && (files.length < filesMin() || files.length > filesMax())) {
        return t("err_file_count", { min: filesMin(), max: filesMax() });
      }
      return null;
    }

    async function doRun() {
      clearError();
      if (!window.__USER__) {
        const ok = await new Promise((res) => {
          if (window.openAuthModal) window.openAuthModal({ mode: "login", onSuccess: () => res(true), onClose: () => res(false) });
          else { window.location.href = "/auth/login"; res(false); }
        });
        if (!ok || !window.__USER__) return;
      }
      if (!tool) return;
      if (window.toolOperationBusy && window.toolOperationBusy()) return;
      const err = validateBeforeRun();
      if (err) { showError(err); return; }
      const params = collectParams();
      const expSel = container.querySelector(".ti-expiry");
      const dlSel = container.querySelector(".ti-downloads");
      // 体积对比信号 (仅呈现层): 本次输入文件大小, app.js 结果区消费
      window.__lastInputSizes = files.slice().map((f) => f.size);
      const btn = container.querySelector(".ti-run");
      if (btn) { btn.disabled = true; }
      try {
        await window.runToolFlow(tool, files.slice(), {
          params: params,
          expiryDays: expSel ? parseInt(expSel.value, 10) : 1,
          allowedDownloads: dlSel ? parseInt(dlSel.value, 10) : 50,
          onProgress: (pct) => {
            const runLine = container.querySelector(".ti-progress");
            if (runLine) runLine.textContent = t("uploading") + " · " + pct + "%";
          },
          onError: (msg) => {
            window.showToolError && window.showToolError(msg, null, () => {
              const p = window.retryLastToolOperation && window.retryLastToolOperation();
              if (p && typeof p.catch === "function") p.catch(() => {});
            });
          },
        });
      } catch (e) {
        const cb = window.canRetryToolOperation && window.canRetryToolOperation()
          ? () => {
              const p = window.retryLastToolOperation && window.retryLastToolOperation();
              if (p && typeof p.catch === "function") p.catch(() => {});
            }
          : null;
        if (window.showToolError) window.showToolError(e.message, null, cb);
        else showError(e.message);
      } finally {
        if (btn) btn.disabled = false;
        const runLine = container.querySelector(".ti-progress");
        if (runLine) runLine.textContent = "";
      }
    }

    function bindRun() {
      const btn = container.querySelector(".ti-run");
      if (btn) btn.addEventListener("click", doRun);
      // Ctrl/Cmd+Enter 全局提交 (composer 内任意输入焦点)
      container.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          doRun();
        }
      });
    }

    function setTool(next) {
      tool = next;
      files = [];
      render();
      clearError();
      if (opts.onChange) opts.onChange([]);
    }

    // Language switch: re-render the controls for the CURRENT tool,
    // keeping the chosen files (params survive via the persisted values).
    function rerender() {
      if (!tool) return;
      render();
    }

    render();
    return {
      el: container,
      setTool: setTool,
      rerender: rerender,
      files: () => files.slice(),
    };
  }

  window.ToolInput = { mount: mount, savedParams: savedParams };
})();
