// ── 工具页 (能力叙事 + 一控件即执行) ──────────────────────────
// 机制 (0905 终稿): 单工具上下文 = 参数平铺默认预选 + 选文件即提交,
// 无确认步无大 dropzone。执行走全局 process-flow (runToolFlow 单源)。
// app.js 提供共享底座 (t/showResult/showToolProcessing/showShareResult/
// resolveLabel/extLabel) — 此文件只做本页专属逻辑。
(function () {
  "use strict";

  const tool = (window.__TOOLS__ || [])[0];
  if (!tool) return;

  const paramsBox = document.getElementById("toolParams");
  const fileInput = document.getElementById("toolFile");
  const fileLabel = document.getElementById("toolFileLabel");
  const fileName = document.getElementById("toolFileName");

  const esc = window.escapeHtml || (s => String(s || "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])));
  const t = k => window.t(k);
  const resolveLabel = l => window.resolveLabel(l);

  // ── 参数平铺 (默认预选, localStorage 持久 — 与首页浮层同存储键) ──
  function renderParams() {
    if (!paramsBox) return;
    let html = "";
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem("flashshare_tool_params") || "{}")[tool.id] || {}; } catch (e) { saved = {}; }

    if (Array.isArray(tool.presets) && tool.presets.length) {
      const def = (tool.presets.find(p => p.default) || tool.presets[0]).id;
      html += `<div class="toolpage-param-group"><div class="tool-overlay-presets">`;
      tool.presets.forEach(p => {
        const sel = (saved.__preset || def) === p.id ? " active" : "";
        html += `<button type="button" class="tool-overlay-preset tp-preset${sel}" data-preset="${esc(p.id)}">${esc(resolveLabel(p.label) || p.id)}</button>`;
      });
      html += `</div></div>`;
    }
    if (Array.isArray(tool.params) && tool.params.length) {
      // B1: 单参数工具不显示通用 heading — 与首页浮层同规则。
      const lblHtml = tool.params.length > 1
        ? `<div class="tool-param-label">${esc(t("tool_params") !== "tool_params" ? t("tool_params") : "Options")}</div>`
        : "";
      html += `<div class="toolpage-param-group">${lblHtml}<div class="toolpage-params-grid">`;
      tool.params.forEach(p => {
        const pl = resolveLabel(p.label) || p.key;
        const val = saved[p.key] !== undefined ? saved[p.key] : p.default;
        const pid = `tp_${p.key}`;
        if (p.type === "enum") {
          html += `<div class="toolpage-param"><div class="tool-param-sub">${esc(pl)}</div><div class="tool-overlay-presets">`;
          (p.options || []).forEach(o => {
            const sel = (val === o.value) ? " active" : "";
            html += `<button type="button" class="tool-overlay-preset tp-option${sel}" data-param="${esc(p.key)}" data-value="${esc(o.value)}">${esc(resolveLabel(o.label) || o.value)}</button>`;
          });
          html += `</div></div>`;
        } else if (p.type === "boolean") {
          const on = val === true || val === "true";
          html += `<div class="toolpage-param tool-param-row"><label class="tool-param-sub" for="${pid}">${esc(pl)}</label><input type="checkbox" id="${pid}" class="tp-bool" data-param="${esc(p.key)}"${on ? " checked" : ""}></div>`;
        } else if (p.type === "number") {
          const mn = p.min !== undefined ? ` min="${p.min}"` : "";
          const mx = p.max !== undefined ? ` max="${p.max}"` : "";
          html += `<div class="toolpage-param tool-param-row"><label class="tool-param-sub" for="${pid}">${esc(pl)}</label><input type="number" id="${pid}" class="tp-number" data-param="${esc(p.key)}"${mn}${mx} value="${val !== undefined && val !== null ? esc(String(val)) : ""}"></div>`;
        } else {
          const ph = p.placeholder ? esc(resolveLabel(p.placeholder)) : "";
          html += `<div class="toolpage-param tool-param-row"><label class="tool-param-sub" for="${pid}">${esc(pl)}</label><input type="text" id="${pid}" class="tp-text" data-param="${esc(p.key)}" placeholder="${ph}" value="${val !== undefined && val !== null ? esc(String(val)) : ""}"></div>`;
        }
      });
      html += `</div></div>`;
    }
    paramsBox.innerHTML = html;

    paramsBox.querySelectorAll(".tp-preset").forEach(b => b.addEventListener("click", () => {
      paramsBox.querySelectorAll(".tp-preset").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      persist();
    }));
    paramsBox.querySelectorAll(".tp-option").forEach(b => b.addEventListener("click", () => {
      paramsBox.querySelectorAll(`.tp-option[data-param="${b.dataset.param}"]`).forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      persist();
    }));
    paramsBox.addEventListener("change", () => persist());
  }

  function collect() {
    const out = {};
    if (!paramsBox) return out;
    if (Array.isArray(tool.params)) {
      tool.params.forEach(p => {
        if (p.type === "enum") {
          const on = paramsBox.querySelector(`.tp-option[data-param="${p.key}"].active`);
          if (on) out[p.key] = on.dataset.value;
        } else if (p.type === "boolean") {
          const el = paramsBox.querySelector(`.tp-bool[data-param="${p.key}"]`);
          if (el) out[p.key] = el.checked;
        } else {
          const el = paramsBox.querySelector(`.tp-${p.type === "number" ? "number" : "text"}[data-param="${p.key}"]`);
          if (el && el.value !== "") out[p.key] = p.type === "number" ? Number(el.value) : el.value;
        }
      });
    }
    return out;
  }

  function currentPreset() {
    const on = paramsBox && paramsBox.querySelector(".tp-preset.active");
    return on ? on.dataset.preset : null;
  }

  function persist() {
    try {
      const all = JSON.parse(localStorage.getItem("flashshare_tool_params") || "{}");
      all[tool.id] = Object.assign(collect(), currentPreset() ? { __preset: currentPreset() } : {});
      localStorage.setItem("flashshare_tool_params", JSON.stringify(all));
    } catch (e) { /* non-fatal */ }
  }

  // ── 一控件即执行: 选文件 = 提交 ──
  async function run(files) {
    if (!files || !files.length) return;
    if (!window.__USER__) { location.href = "/auth/login"; return; }
    if (fileName) {
      fileName.textContent = files.length > 1
        ? `${files.length} ${t("err_too_many_files") !== "err_too_many_files" ? "" : ""}`.trim() || `${files.length} files`
        : files[0].name;
    }
    fileLabel.classList.add("busy");
    try {
      const r = await window.runToolFlow(tool, files, {
        preset: currentPreset(),
        params: collect(),
        onProgress: (pct, bps) => {
          const speed = bps >= 1048576 ? (bps / 1048576).toFixed(1) + " MB/s"
                      : bps >= 1024 ? (bps / 1024).toFixed(0) + " KB/s"
                      : Math.round(bps) + " B/s";
          if (fileName) fileName.textContent = `${t("uploading")} ${pct}% · ${speed}`;
        },
      });
      if (r && fileName) fileName.textContent = "";
    } catch (e) {
      window.showResult && window.showResult(esc(e.message), true);
    } finally {
      fileLabel.classList.remove("busy");
      if (fileInput) fileInput.value = "";
    }
  }

  if (fileInput) {
    fileInput.addEventListener("change", () => run(fileInput.files));
  }
  // 键盘可达 (label tabindex)
  if (fileLabel) {
    fileLabel.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput && fileInput.click(); }
    });
  }
  // 整页可拖放 — 页面即 drop 目标, 但视觉重量只是一行控件
  document.addEventListener("dragover", e => e.preventDefault());
  document.addEventListener("drop", e => {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files.length) run(e.dataTransfer.files);
  });

  renderParams();

  // app.js 自检要求的核心 id 在本页不存在 — 通知其静默跳过多工具绑定
  window.__TOOLPAGE__ = true;
})();
