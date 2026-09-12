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
   * tool: catalog item 形状 {id,label,params,multi,cost,...}
   *       传 null/undefined = 无工具直传 (share 语义)。
   * files: FileList | File[]
   * opts: {params, expiryDays, allowedDownloads,
   *        onProgress(pct, speedStr), signal}
   */
  async function runToolFlow(tool, files, opts) {
    opts = opts || {};
    const fileList = Array.from(files || []);
    if (!fileList.length) return null;
    if (!window.__USER__) { location.href = "/auth/login"; return null; }

    // Shared affordability preflight (advisory): for the tool that WILL
    // execute, ask before any source upload starts. 402 → typed error,
    // no upload. Other outcomes defer to the authoritative gate.
    {
      const execTool = tool && !tool.pass_through ? tool : null;
      if (execTool && window.preflightToolAffordability) {
        const pf = await window.preflightToolAffordability(execTool);
        if (!pf.ok) throw new Error(errText(pf.error));
      }
    }

    const isMulti = !!(tool && tool.multi);
    // 4C-2: multi=false 显式多文件不静默截断/忽略 — typed 错误, 由 UI 呈现
    // (dispatch 对 files[] 越界同样 typed err_too_many_files)。
    if (!isMulti && fileList.length > 1) {
      throw new Error(errText("err_too_many_files"));
    }

    // Step 1: upload (multipart, XHR for progress)
    const formData = new FormData();
    fileList.forEach(f => formData.append("file", f));
    // 工具中间件语义: 带 tool 字段 → 上传不落分享记录 (仅 execute 产物落库)
    if (tool) formData.append("tool", tool.id);
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

    // Pass-through (share 语义): 工具声明 pass_through=true 才直传,
    // 不从 presets/params/multi 推断 (无参执行型工具会被误判)。
    const hasExec = tool && !tool.pass_through;
    if (!hasExec) {
      const f = up.files ? up.files[0] : up.file;
      window.showShareResult && window.showShareResult(f);
      window._loadHistory && window._loadHistory();
      return f;
    }

    // Step 2: execute tool — canonical ToolInvocation (Stage C):
    // source.files[] 唯一 shape (single/multi 只是长度不同);
    // params 只含 tool params; delivery 独立; client_ref=null= fresh。
    window.showToolProcessing &&
      window.showToolProcessing(window.resolveLabel ? window.resolveLabel(tool.label) || tool.id : tool.id);
    const uploaded = up.files || [up.file];
    const params = Object.assign({}, opts.params || {});
    const body = {
      tool_id: tool.id,
      source: { files: uploaded.map(f => ({ public_id: f.publicId, source_token: f.sourceToken || "" })) },
      params: params,
      delivery: { expiry_days: opts.expiryDays || 1, allowed_downloads: opts.allowedDownloads || 50 },
      client_ref: null,
    };
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
