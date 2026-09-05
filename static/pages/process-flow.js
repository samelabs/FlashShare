// ── process-flow: 全局处理流 (单源) ──────────────────────────────
// 上传 → (工具执行) → 结果呈现。任何入口 (首页表单 / 工具页一控件 /
// 用户页快捷使用) 都调用同一函数; 首页表单绑定仍在 app.js, 此处只提供
// 编程式入口 + 结果/进度呈现 (依赖 app.js 已加载: t/showResult/
// showToolProcessing/showToolError/showShareResult 均为 app.js 单源)。
(function () {
  "use strict";

  function errText(code) {
    const m = window._errm ? window._errm(code) : code;
    return m || "failed";
  }

  /**
   * runToolFlow(tool, files, opts) → Promise<result|null>
   * tool: catalog item 形状 {id,label,params,presets,multi,cost,...}
   *       传 null/undefined = 无工具直传 (share 语义)。
   * files: FileList | File[]
   * opts: {preset, params, expiryDays, allowedDownloads,
   *        onProgress(pct, speedStr), signal}
   */
  async function runToolFlow(tool, files, opts) {
    opts = opts || {};
    const fileList = Array.from(files || []);
    if (!fileList.length) return null;
    if (!window.__USER__) { location.href = "/auth/login"; return null; }

    const isMulti = !!(tool && tool.multi);
    if (!isMulti && fileList.length > 1) fileList.length = 1;

    // Step 1: upload (multipart, XHR for progress)
    const formData = new FormData();
    fileList.forEach(f => formData.append("file", f));
    const up = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/uploads");
      xhr.timeout = 900000;
      const t0 = Date.now();
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && opts.onProgress) {
          const pct = Math.min(99, Math.round(e.loaded / e.total * 100));
          const elapsed = (Date.now() - t0) / 1000;
          const bps = elapsed > 0 ? e.loaded / elapsed : 0;
          opts.onProgress(pct, bps);
        }
      };
      xhr.onload = () => {
        try {
          const data = JSON.parse(xhr.responseText);
          if (xhr.status >= 400 || !data.ok) { reject(new Error(errText(data.error))); return; }
          resolve(data);
        } catch (e) { reject(new Error(errText("failed"))); }
      };
      xhr.onerror = () => reject(new Error(errText("failed")));
      xhr.ontimeout = () => reject(new Error(errText("failed")));
      xhr.send(formData);
    });

    // Pass-through (share 语义): 无声明工具 → 结果即分享链接
    const hasDecl = tool && (Array.isArray(tool.presets) && tool.presets.length
                             || Array.isArray(tool.params) && tool.params.length);
    if (!hasDecl) {
      const f = up.files ? up.files[0] : up.file;
      window.showShareResult && window.showShareResult(f);
      window._loadHistory && window._loadHistory();
      return f;
    }

    // Step 2: execute tool
    window.showToolProcessing &&
      window.showToolProcessing(window.resolveLabel ? window.resolveLabel(tool.label) || tool.id : tool.id);
    const body = {
      tool_id: tool.id,
      params: Object.assign({
        preset: opts.preset || null,
        expiry_days: opts.expiryDays || 1,
        allowed_downloads: opts.allowedDownloads || 50,
      }, opts.params || {}),
    };
    if (isMulti) {
      const fl = up.files || [up.file];
      body.public_ids = fl.map(f => f.publicId);
      body.source_tokens = fl.map(f => f.sourceToken || "");
    } else {
      body.public_id = up.file.publicId;
      body.source_token = up.file.sourceToken || "";
    }
    const res = await fetch("/api/tools/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      const ref = up.files ? up.files[0] : up.file;
      window.showToolError && window.showToolError(errText(data.error), ref);
      return null;
    }
    if (window.__syncCredits && typeof data.balance === "number") {
      window.__syncCredits(data.balance);
    }
    window.showShareResult && window.showShareResult(data.result);
    window._loadHistory && window._loadHistory();
    return data.result;
  }

  // app.js 函数是非模块脚本顶层声明 = window 属性, 编程式入口可直接用。
  window.runToolFlow = runToolFlow;
})();
