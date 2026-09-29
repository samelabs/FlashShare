// ── process-flow: 全局处理流 (单源) ──────────────────────────────
// executable tool 的唯一浏览器执行链: 首页表单与 /tool/... 一控件都走
// runToolFlow。share 也走完整执行 (上传 → 执行 → file items), app.js
// 不再维护第二套 upload→execute→retry。
//
// Operation identity (硬 invariant):
//   同一次用户动作 (从第一次 upload 到成功或用户明确换输入) 只有一个
//   client_ref。Retry 复用既有 ref + frozen invocation (files/tool/
//   params/delivery) — upload response loss 重传同一 files+ref (ingress
//   receipt replay), execute response loss / tool_failed 只重放 execute
//   (绝不再 upload)。成功清 pending; 换输入 (新文件/新 tool/参数/delivery)
//   invalidate 旧 operation, 下一动作才 mint 新 ref。
//   ref 唯一生成器 = app.js newOperationRef (window.newOperationRef)。
(function () {
  "use strict";

  function errText(code) {
    const m = window._errm ? window._errm(code) : code;
    return m || "failed";
  }

  // Sniff rejections carry structured ingress detail (filename +
  // detected + suggested_name): render the actionable rename sentence
  // instead of the bare code message. Falls back to the unified
  // error boundary for every other payload (or when i18n is absent).
  function payloadErrText(data) {
    const code = data && typeof data === "object" ? data.error : null;
    if (code && typeof data.filename === "string" && window.t
        && String(code).indexOf("err_content_mismatch") === 0) {
      if (data.suggested_name) {
        return window.t("err_content_mismatch_named", {
          name: data.filename, ext: data.detected || "",
          suggest: data.suggested_name,
        });
      }
      return window.t("err_content_mismatch_named_unknown", { name: data.filename });
    }
    return errText(code);
  }

  // ── Logical operation state (module-private) ──
  // {ref, files(File[]), tool, params, delivery, sources(uploaded handles), phase}
  // phase: "idle" | "running" | "upload-retry" | "execute-retry"
  let op = null;

  function invalidateOperation() {
    // A RUNNING operation is never invalidated from outside: a change event
    // racing the submit (input change fired while the upload/execute legs are
    // already in flight) must not null the live op — that orphans the
    // operation state (progress handlers crash on null op, the retry
    // affordance is lost). Input changes while running are simply ignored;
    // the surface's own submit path already refuses concurrent runs.
    if (op && op.phase === "running") return;
    op = null;
  }

  function sameFiles(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }

  function sameDelivery(x, y) {
    return !!x && !!y && x.expiry_days === y.expiry_days
        && x.allowed_downloads === y.allowed_downloads;
  }

  // Input change → invalidate the retained operation. A changed input set
  // is a NEW user action; the next run mints a fresh ref.
  function fingerprintChanged(tool, files, params, delivery) {
    if (!op) return false;
    if ((tool ? tool.id : null) !== (op.tool ? op.tool.id : null)) return true;
    if (!sameFiles(files, op.files)) return true;
    if (JSON.stringify(params || {}) !== JSON.stringify(op.params || {})) return true;
    if (!sameDelivery(delivery, op.delivery)) return true;
    return false;
  }

  // Concurrent-run guard: while an operation is running on this surface,
  // mouse / keyboard / drag-drop entries all land here and are refused.
  // Returns true when a second operation was blocked.
  function blockConcurrentRun() {
    return !!op && op.phase === "running";
  }

  function uploadLeg() {
    // (Re)upload the FROZEN files with the FROZEN ref — same ref on the
    // first attempt and on upload-response-loss retry, so the server's
    // ingress receipt replays instead of minting a second logical op.
    const formData = new FormData();
    op.files.forEach(f => formData.append("file", f));
    if (op.tool) formData.append("tool", op.tool.id);
    formData.append("client_ref", op.ref);
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/uploads");
      xhr.timeout = 900000;
      const t0 = Date.now();
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && op.onProgress) {
          const pct = Math.min(99, Math.round(e.loaded / e.total * 100));
          const elapsed = (Date.now() - t0) / 1000;
          const bps = elapsed > 0 ? e.loaded / elapsed : 0;
          op.onProgress(pct, bps);
        }
      };
      xhr.onload = () => {
        try {
          const data = JSON.parse(xhr.responseText);
          if (xhr.status >= 400 || !data.ok) { reject(new Error(payloadErrText(data))); return; }
          resolve(data);
        } catch (e) { reject(new Error(errText("failed"))); }
      };
      xhr.onerror = () => reject(new Error(errText("failed")));
      xhr.ontimeout = () => reject(new Error(errText("failed")));
      xhr.send(formData);
    });
  }

  function executeLeg() {
    // Execute the FROZEN invocation (handles from the first successful
    // upload) with the SAME ref. Retry after execute response loss or
    // tool_failed replays this leg — zero second upload.
    const body = {
      tool_id: op.tool.id,
      source: { files: op.sources.map(f => ({ public_id: f.stagingId, source_token: f.sourceToken || "" })) },
      params: op.params,
      delivery: op.delivery,
      client_ref: op.ref,
    };
    return fetch("/api/tools/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(res => res.json());
  }

  /**
   * runToolFlow(tool, files, opts) → Promise<result|null>
   * tool: catalog item 形状 {id,label,params,files_min,files_max,outputs,cost,…}
   * files: FileList | File[] (空数组 = 0-file 工具的纯文字/参数调用)
   * opts: {params, expiryDays, allowedDownloads,
   *        onProgress(pct, bps), onError(message, ref)}
   */
  async function runToolFlow(tool, files, opts) {
    opts = opts || {};
    // Concurrent-run guard (hard invariant): while an operation is in
    // flight on this surface, ANY second entry — mouse, keyboard or
    // drag-drop — is refused here (not just CSS pointer-events).
    if (blockConcurrentRun()) return null;
    const fileList = Array.from(files || []);
    if (!tool) return null;
    // declared window (catalog item contract; files_max 0 = text-only)
    const filesMin = tool.files_min;
    const filesMax = tool.files_max;
    // 0-file tools (files_min === 0) run without any file; every other
    // tool needs at least one input file.
    if (!fileList.length && filesMin > 0) return null;
    if (!window.__USER__) {
      // login modal instead of a page jump: the picked files/params stay
      // in this invocation's closure and the run resumes on success
      const ok = await new Promise((res) => {
        if (window.openAuthModal) window.openAuthModal({ mode: "login", onSuccess: () => res(true), onClose: () => res(false) });
        else { location.href = "/auth/login"; res(false); }
      });
      if (!ok || !window.__USER__) return null;
    }
    if (typeof window.newOperationRef !== "function") {
      throw new Error(errText("failed"));
    }

    let params = Object.assign({}, opts.params || {});
    let delivery = {
      expiry_days: opts.expiryDays || 1,
      allowed_downloads: opts.allowedDownloads || 50,
    };

    // Frozen operation: an input change invalidates the old operation and
    // a new ref is minted for the next action. An unchanged input keeps
    // the retained ref (response-loss retry path). On a retry re-entry
    // with the frozen inputs, keep the retained ref and delivery verbatim.
    const isRetry = !!op && op.phase !== "running"
        && (op.phase === "upload-retry" || op.phase === "execute-retry")
        && !fingerprintChanged(tool, fileList, params, delivery);
    if (isRetry) {
      // retry carries the FROZEN params/delivery, not the re-entry opts
      params = op.params;
      delivery = op.delivery;
    } else if (fingerprintChanged(tool, fileList, params, delivery)) {
      invalidateOperation();
    }

    // Files window (declaration authority): over-max is a typed error —
    // never silently truncated (dispatch rejects the same window).
    if (fileList.length > filesMax) {
      invalidateOperation();
      throw new Error(errText("err_too_many_files"));
    }

    if (isRetry) {
      // retry re-entry: keep the retained ref (frozen operation) — do NOT
      // mint. upload-retry (sources null) re-uploads below; execute-retry
      // replays execute with the retained handles.
      op.phase = "running";
    } else {
      // Fresh operation: ONE ref minted per user action (single generator).
      op = {
        ref: window.newOperationRef(),
        files: fileList,
        tool: tool || null,
        params: params,
        delivery: delivery,
        sources: null,
        onProgress: opts.onProgress || null,
        phase: "running",
      };
    }

    try {
      // Shared affordability preflight (advisory): for the tool that WILL
      // execute, ask before any source upload starts. 402 → typed error,
      // no upload. Other outcomes defer to the authoritative gate.
      const execTool = tool || null;
      if (execTool && !isRetry && window.preflightToolAffordability) {
        const pf = await window.preflightToolAffordability(execTool);
        if (!pf.ok) {
          invalidateOperation();
          throw new Error(errText(pf.error));
        }
      }

      // Step 1: upload — skipped for 0-file calls and for retries with
      // retained handles.
      if (fileList.length && (!isRetry || !op.sources)) {
        const up = await uploadLeg();
        const uploaded = up.files || [up.file];
        op.sources = uploaded.map(f => ({ stagingId: f.stagingId, sourceToken: f.sourceToken || "" }));
      } else if (!fileList.length) {
        op.sources = [];
      }

      // share (IO-1) 是常规执行工具: 上传后照常执行。
      const hasExec = !!tool;
      if (!hasExec) {
        op = null;
        return null;
      }

      // Step 2: execute tool — canonical ToolInvocation (the canonical-invocation unification):
      // source.files[] 唯一 shape (single/multi 只是长度不同);
      // params 只含 tool params; delivery 独立; client_ref = the operation
      // identity minted for THIS run (identical to the upload leg's ref).
      window.showToolProcessing &&
        window.showToolProcessing(window.resolveLabel ? window.resolveLabel(tool.label) || tool.id : tool.id);

      let data;
      try {
        data = await executeLeg();
      } catch (e) {
        // execute response loss — keep ref + sources for retry
        op.phase = "execute-retry";
        throw e;
      }
      if (!data.ok) {
        // tool_failed → explicit retry continues the SAME ref + fingerprint
        op.phase = "execute-retry";
        const ref = null;
        if (opts.onError) opts.onError(payloadErrText(data), ref);
        else window.showToolError && window.showToolError(payloadErrText(data), null);
        return null;
      }
      if (window.__syncCredits && typeof data.balance === "number") {
        window.__syncCredits(data.balance);
      }
      window.showShareResult && window.showShareResult(data.result);
      window._loadHistory && window._loadHistory();
      op = null; // success — pending operation cleared
      return data.result;
    } catch (e) {
      // Upload-leg response loss (network error / timeout / parse) lands
      // here: the ref and any sources are KEPT — the next retry re-enters
      // with the frozen operation. Typed business errors (preflight,
      // too-many-files) already invalidated the operation before throwing.
      if (op && op.phase === "running") op.phase = "upload-retry";
      throw e;
    }
  }

  // Explicit retry entry (wired by the UI layer to the error card's Retry
  // button). Re-enters runToolFlow with the FROZEN operation — same ref,
  // same files/tool/params/delivery; upload-leg response loss re-uploads
  // the same files for receipt replay, execute loss / tool_failed replays
  // execute only.
  window.retryLastToolOperation = function () {
    if (!op) return null;
    if (op.phase === "running") return null;
    const frozen = op;
    op.phase = frozen.sources ? "execute-retry" : "upload-retry";
    return runToolFlow(frozen.tool, frozen.files, {
      params: frozen.params,
      expiryDays: frozen.delivery.expiry_days,
      allowedDownloads: frozen.delivery.allowed_downloads,
      onProgress: frozen.onProgress,
    });
  };

  // True while an operation is in flight — surfaces use this to refuse
  // mouse / keyboard / drag-drop second entries.
  window.toolOperationBusy = function () {
    return blockConcurrentRun();
  };

  // True when a frozen operation is retained and can be retried (upload
  // response loss / execute response loss / tool_failed). Preflight and
  // validation failures invalidate the operation → false → the UI must
  // NOT render a Retry button.
  window.canRetryToolOperation = function () {
    return !!op && op.phase !== "running"
        && (op.phase === "upload-retry" || op.phase === "execute-retry");
  };

  // Input-change hook: surfaces call this when files/tool/params/delivery
  // change so the next action mints a fresh operation.
  window.invalidateToolOperation = invalidateOperation;

  window.runToolFlow = runToolFlow;
})();
