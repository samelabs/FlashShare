// ── tool-input: 任务输入区共用组件 (首页 / 工具页同一组件) ──────
// 按所选工具的声明呈现: 参数控件 (text 参数 = 文字输入区)、文件添加
// (选择/拖放/粘贴, 数量窗口 [min,max], 接受格式)、交付选项 (次级折叠)。
// 校验走与后端同一报错边界 (window.t + 服务端错误码):
//   文件数越窗 → err_file_count {min}{max}; 格式 → err_format_mismatch;
//   必填参数 → param_required。
// 执行走全局单源 window.runToolFlow (process-flow.js); 本组件只管
// 输入呈现、即时校验与忙碌态, 不复制执行链。
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

  // 参数持久化单点 (localStorage 权威, 与旧浮层同键同语义)。
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
   * ({id,label,params,accept,files_min,files_max,outputs,cost,…})
   * controller: {setTool(tool), files(), el}
   */
  function mount(container, opts) {
    opts = opts || {};
    let tool = opts.tool || null;
    let files = [];
    const thumbUrls = [];

    function filesMin() { return tool ? Math.max(0, tool.files_min || 0) : 1; }
    function filesMax() { return tool ? Math.min(20, tool.files_max || 1) : 1; }
    function acceptTokens() {
      return (tool && Array.isArray(tool.accept) && tool.accept.length)
        ? tool.accept.map((a) => String(a).replace(/^\./, "").toLowerCase())
        : null;
    }
    function windowText() {
      const mn = filesMin(), mx = filesMax();
      if (mn === 0 && mx === 1) return t("ti_files_optional");
      return t("ti_files_window", { min: mn, max: mx });
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
      const p = tool ? (Array.isArray(tool.params) ? tool.params : []) : [];
      const accept = acceptTokens();
      const optional = filesMin() === 0;

      let html = '<div class="ti-card' + (optional ? " ti-optional" : "") + '" data-tool="' + esc(tool ? tool.id : "") + '">';

      // 参数区 (声明驱动): text 参数 = 文字输入 (textarea), 其余同原控件族
      if (p.length) {
        html += '<div class="ti-params">';
        const saved = savedParams(tool.id);
        p.forEach((prm) => {
          const label = resolveLabel(prm.label) || prm.key;
          const val = saved[prm.key] !== undefined ? saved[prm.key] : prm.default;
          const pid = "ti_" + prm.key;
          if (prm.type === "enum") {
            html += '<div class="ti-param field"><div class="field-label">' + esc(label) + "</div>"
              + '<div class="ti-presets">';
            (prm.options || []).forEach((o) => {
              const sel = val === o.value ? " active" : "";
              html += '<button type="button" class="ti-preset tp-option' + sel + '" data-param="' + esc(prm.key)
                + '" data-value="' + esc(o.value) + '">' + esc(resolveLabel(o.label) || o.value) + "</button>";
            });
            html += "</div></div>";
          } else if (prm.type === "boolean") {
            html += '<div class="ti-param ti-param-row"><label class="check" for="' + pid + '">'
              + '<input type="checkbox" id="' + pid + '" class="tp-bool" data-param="' + esc(prm.key) + '"'
              + (val === true || val === "true" ? " checked" : "") + "><span>" + esc(label) + "</span></label></div>";
          } else if (prm.type === "number") {
            const mn = prm.min !== undefined ? ' min="' + esc(prm.min) + '"' : "";
            const mx = prm.max !== undefined ? ' max="' + esc(prm.max) + '"' : "";
            html += '<div class="ti-param ti-param-row field"><label class="field-label" for="' + pid + '">'
              + esc(label) + '</label><input type="number" id="' + pid + '" class="input tp-number" data-param="' + esc(prm.key)
              + '"' + mn + mx + ' value="' + (val !== undefined && val !== null ? esc(String(val)) : "") + '"></div>';
          } else {
            // text 参数 = 多行文字输入 (任务的多模态输入之一)
            const ph = prm.placeholder ? esc(resolveLabel(prm.placeholder)) : "";
            const req = prm.required && prm.default == null ? " ti-required" : "";
            html += '<div class="ti-param field' + req + '"><label class="field-label" for="' + pid + '">'
              + esc(label) + (prm.required && prm.default == null ? ' <i class="field-req" aria-hidden="true">*</i>' : "")
              + '</label><textarea id="' + pid + '" class="textarea tp-text" data-param="' + esc(prm.key)
              + '" rows="3" placeholder="' + ph + '">'
              + (val !== undefined && val !== null ? esc(String(val)) : "") + "</textarea></div>";
          }
        });
        html += "</div>";
      }

      // 文件区: 数量窗口 + 接受格式提示; min=0 的工具为紧凑可选行
      const acceptHint = accept
        ? '<span class="chip-row">' + accept.map((a) => '<span class="chip">' + esc(a.toUpperCase()) + "</span>").join("") + "</span>"
        : esc(t("ti_any_format"));
      if (optional) {
        html += '<div class="ti-attach ti-attach-compact">'
          + '<button type="button" class="btn btn-secondary btn-sm ti-add ti-add-quiet">' + window.iconSvg("upload")
          + "<span>" + t("ti_add_optional") + "</span></button>"
          + '<span class="ti-window-note">' + esc(t("ti_files_none_needed")) + "</span>"
          + '<div class="ti-rows ti-rows-quiet"></div></div>';
      } else {
        html += '<div class="ti-attach">'
          + '<div class="ti-attach-head"><span class="ti-window">' + esc(windowText()) + "</span>"
          + '<span class="ti-accept">' + acceptHint + "</span></div>"
          + '<div class="ti-drop">'
          + '<button type="button" class="btn btn-secondary ti-add">' + window.iconSvg("upload") + "<span>" + t("ti_add_files") + "</span></button>"
          + '<span class="ti-drop-hint">' + esc(t("ti_drop_hint")) + "</span></div>"
          + '<div class="ti-rows"></div></div>';
      }
      html += '<input type="file" class="visually-hidden ti-file-input"'
        + (accept ? ' accept="' + esc(accept.map((a) => "." + a).join(",")) + '"' : "")
        + (filesMax() > 1 ? " multiple" : "") + ">";

      // 交付选项 (次级折叠)
      const del = window.__DELIVERY__ || { days: [1], downloads: [50], defaultDays: 1, defaultDownloads: 50 };
      const optsExp = del.days.map((d) => '<option value="' + d + '"' + (d === del.defaultDays ? " selected" : "") + ">"
        + d + " " + t(d === 1 ? "day_unit" : "days_unit") + "</option>").join("");
      const optsDl = del.downloads.map((n) => '<option value="' + n + '"' + (n === del.defaultDownloads ? " selected" : "") + ">"
        + n + " " + t("times_unit") + "</option>").join("");
      html += '<details class="ti-delivery"><summary>' + esc(t("ti_delivery")) + "</summary>"
        + '<div class="ti-delivery-row">'
        + '<label class="field ti-field"><span class="field-label">' + esc(t("expiry")) + '</span><select class="select ti-expiry" data-delivery="expiry_days">' + optsExp + "</select></label>"
        + '<label class="field ti-field"><span class="field-label">' + esc(t("downloads")) + '</span><select class="select ti-downloads" data-delivery="allowed_downloads">' + optsDl + "</select></label>"
        + "</div></details>";

      // 运行行
      html += '<div class="ti-run-row">'
        + '<p class="error-note ti-error" hidden></p>'
        + '<span class="ti-progress tertiary" aria-live="polite"></span>'
        + '<button type="button" class="btn btn-primary btn-lg ti-run">' + window.iconSvg("wand") + "<span>" + t("ti_run") + "</span></button>"
        + "</div></div>";

      container.innerHTML = html;
      bindParams();
      bindFiles();
      bindRun();
      renderRows();
    }

    // ── 参数控件 ──
    function bindParams() {
      container.querySelectorAll(".tp-option").forEach((btn) => {
        btn.addEventListener("click", () => {
          container.querySelectorAll('.tp-option[data-param="' + btn.dataset.param + '"]')
            .forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
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
          const on = container.querySelector('.tp-option[data-param="' + prm.key + '"].active');
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
      const rejected = [];
      for (const f of incoming) {
        const ext = extOf(f.name);
        if (accept ? accept.indexOf(ext) === -1 : !EXT_TO_TYPE[ext]) { rejected.push(f.name); continue; }
        if (files.length >= max) { rejected.push(f.name); continue; }
        files.push(f);
      }
      if (rejected.length) {
        showError(accept ? t("err_format_mismatch") : t("err_file_count", { min: filesMin(), max: max }));
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
      const rows = container.querySelector(".ti-rows");
      if (!rows) return;
      thumbUrls.forEach((u) => URL.revokeObjectURL(u));
      thumbUrls.length = 0;
      if (!files.length) { rows.innerHTML = ""; return; }
      rows.innerHTML = files.map((f, i) => {
        const isImg = /^image\//.test(f.type || "") && (f.type || "").toLowerCase() !== "image/svg+xml";
        let thumb = "";
        if (isImg) {
          const u = URL.createObjectURL(f);
          thumbUrls.push(u);
          thumb = '<img class="file-thumb" src="' + u + '" alt="" loading="lazy">';
        } else {
          thumb = '<span class="file-thumb file-thumb-glyph" aria-hidden="true">' + window.iconSvg("doc") + "</span>";
        }
        return '<div class="file-row ti-row" data-idx="' + i + '">' + thumb
          + '<span class="file-name">' + esc(f.name) + "</span>"
          + '<span class="file-meta">' + esc(fmtSize(f.size)) + "</span>"
          + '<button type="button" class="file-remove ti-row-remove" data-rm="' + i + '" aria-label="' + esc(t("ti_remove")) + '" title="' + esc(t("ti_remove")) + '">'
          + window.iconSvg("close") + "</button></div>";
      }).join("");
      rows.querySelectorAll("[data-rm]").forEach((b) => {
        b.addEventListener("click", () => removeFile(+b.dataset.rm));
      });
    }

    function bindFiles() {
      const addBtns = container.querySelectorAll(".ti-add");
      const input = fileInput();
      addBtns.forEach((b) => b.addEventListener("click", () => input && input.click()));
      if (input) input.addEventListener("change", () => {
        addFiles(input.files);
        input.value = "";
      });
      // 拖放: 整个输入卡即落点
      const card = container.querySelector(".ti-card");
      if (card) {
        ["dragenter", "dragover"].forEach((n) => card.addEventListener(n, (e) => {
          e.preventDefault();
          card.classList.add("dragover");
        }));
        ["dragleave", "drop"].forEach((n) => card.addEventListener(n, (e) => {
          e.preventDefault();
          card.classList.remove("dragover");
        }));
        card.addEventListener("drop", (e) => {
          if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
        });
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

    // ── 运行 ──
    function validateBeforeRun() {
      const p = tool && Array.isArray(tool.params) ? tool.params : [];
      for (const prm of p) {
        if (prm.type !== "text" && prm.type !== "number" && prm.type !== "enum") continue;
        if (!prm.required || prm.default != null) continue;
        const el = container.querySelector('.tp-' + (prm.type === "number" ? "number" : "text") + '[data-param="' + prm.key + '"]')
          || container.querySelector('.tp-option[data-param="' + prm.key + '"]');
        const val = el ? el.value : "";
        if (prm.type === "enum") {
          const on = container.querySelector('.tp-option[data-param="' + prm.key + '"].active');
          if (!on) return t("param_required");
        } else if (!val || !String(val).trim()) {
          return t("param_required");
        }
      }
      if (files.length < filesMin() || files.length > filesMax()) {
        return t("err_file_count", { min: filesMin(), max: filesMax() });
      }
      return null;
    }

    function bindRun() {
      const btn = container.querySelector(".ti-run");
      if (!btn) return;
      btn.addEventListener("click", async () => {
        clearError();
        if (!window.__USER__) { window.location.href = "/auth/login"; return; }
        if (!tool) return;
        if (window.toolOperationBusy && window.toolOperationBusy()) return;
        const err = validateBeforeRun();
        if (err) { showError(err); return; }
        const params = collectParams();
        const expSel = container.querySelector(".ti-expiry");
        const dlSel = container.querySelector(".ti-downloads");
        btn.disabled = true;
        btn.classList.add("busy");
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
          btn.disabled = false;
          btn.classList.remove("busy");
          const runLine = container.querySelector(".ti-progress");
          if (runLine) runLine.textContent = "";
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
