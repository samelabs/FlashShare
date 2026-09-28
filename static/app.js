// ── app.js: 首页/工具页共享底座 (IO-2) ──────────────────────────
// 职责: 结果呈现 (renderResultItems — 与 /r 结果页同一结构)、错误卡
// (showToolError/showError, 与服务端错误码同一边界)、处理中状态、
// operation-ref 唯一生成器。页面输入区 = pages/tool-input.js (共用组件),
// 首页选择逻辑 = pages/home.js, 工具页 = pages/tool_page.js。
//
// i18n: t()/applyI18n() 来自 base.html 全局引擎。顶层禁止 function t /
// function applyI18n 声明 (会遮蔽引擎导致无限递归) — 别名一律 const 箭头。

// ── Shared web affordability preflight (advisory, read-only) ──
// tool == null → no preflight. Executable tool → ask
// /api/tools/preflight BEFORE any upload XHR starts. An explicit
// 402 err_insufficient_credits stops the flow (no FormData upload);
// network/5xx stays advisory — the authoritative gate still decides.
window.preflightToolAffordability = async function (tool) {
  if (!tool) return { ok: true };
  try {
    const r = await fetch("/api/tools/preflight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_id: tool.id }),
    });
    if (r.status === 402) {
      const d = await r.json().catch(() => null);
      if (d && d.error === "err_insufficient_credits") {
        return { ok: false, error: "err_insufficient_credits" };
      }
    }
    return { ok: true }; // advisory: any other outcome defers to the gate
  } catch (e) {
    return { ok: true }; // network failure — never a new authority
  }
};

// ── Load self-check: version-skew (stale cached JS vs fresh HTML) must
// fail LOUD, not silently half-bind events. Both surfaces loading app.js
// (home + tool page) always render into #result. ──
(function () {
  const missing = ["result"].filter(id => !document.getElementById(id));
  if (missing.length) {
    console.error("[518] DOM missing:", missing.join(", "), "— stale JS? hard-refresh (Cmd/Ctrl+Shift+R)");
    const b = document.createElement("div");
    b.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#f4212e;color:#fff;padding:10px 16px;font:600 14px system-ui;text-align:center";
    b.textContent = window.t ? window.t("stale_banner") : "Page resources are out of date — please hard-refresh (Ctrl/Cmd+Shift+R)";
    document.body && document.body.appendChild(b);
    throw new Error("[518] abort: DOM missing " + missing.join(", "));
  }
})();

const result = document.getElementById("result");

const _IC = window.__ICONS__ || {};
function svgIcon(name) {
  const path = _IC[name] || _IC.info || "";
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}
// Canonical icon renderer — site-wide single source.
window.iconSvg = svgIcon;

// i18n: t(), applyI18n(), lang data — all provided by base.html global engine
// Local alias — MUST stay arrow/const: a top-level `function t` declaration
// would shadow window.t (engine) and recurse infinitely.
const t = (key, vars = {}) => window.t(key, vars);
function currentLang() { return window.__LANG || "en"; }

function fmtSize(n) {
  if (!n) return "0 B";
  const units = ["B","KB","MB","GB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}
function fmtDate(ts) {
  if (!ts) return "-";
  const lm = window.__LOCALE_MAP || {};
  return new Date(ts * 1000).toLocaleString(lm[currentLang()] || "en", { hour12:false });
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}

let lastErrorKey = null;
function showResult(html, isError=false) {
  result.className = "result" + (isError ? " error" : "");
  result.innerHTML = html;
}
function showError(errorKey) {
  lastErrorKey = errorKey;
  showResult(`<div class="card card-pad result-card"><div class="error-note">${escapeHtml(t(errorKey))}</div></div>`, true);
}

// Language switch: dynamic strings follow (JS-rendered input area
// re-renders through the surface hook; a visible error re-renders too).
window.__afterI18n = function() {
  document.title = t("title");
  window.__toolInputRefresh && window.__toolInputRefresh();
  if (lastErrorKey && result.classList.contains("error")) {
    showError(lastErrorKey);
  }
};

function showToolProcessing(toolName) {
  showResult(`<div class="card card-pad result-card"><div class="empty" style="padding:var(--s5)"><div class="upload-spinner"></div><span>${escapeHtml(toolName)} · ${t("processing")}</span></div></div>`);
}

// Task-2 error contract: explicit retry callback decides whether a Retry
// button exists at all — no dead buttons. The message is already
// user-facing (process-flow maps unknown codes to t("failed")); machine
// error codes are never rendered raw.
function showToolError(message, originalFile, retryCallback) {
  const origUrl = originalFile && (originalFile.page_url || originalFile.url) ? (originalFile.page_url || originalFile.url) : "";
  const origLink = origUrl ? `<a class="btn btn-primary" href="${escapeHtml(origUrl)}" target="_blank">${t("useOriginal")}</a>` : "";
  const retryBtn = typeof retryCallback === "function"
    ? `<button class="btn btn-secondary" id="toolRetry" type="button">${t("retry")}</button>` : "";
  showResult(`<div class="card card-pad result-card"><div class="error-note">${escapeHtml(message)}</div><div class="row row-wrap" style="margin-block-start:var(--s3)">${retryBtn}${origLink}</div></div>`, true);
  const btn = document.getElementById("toolRetry");
  if (btn && typeof retryCallback === "function") {
    btn.addEventListener("click", retryCallback);
  }
}

// ── operation identity (client-generated, page-lifetime only) ──
// One user action = one stable client_ref shared by upload + execute +
// retry. Changed operation input drops the pending ref so the next
// submit generates a fresh one. No localStorage, no persistence: this
// only needs to survive a response-loss retry.
function newOperationRef() {
  const u = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID().replace(/-/g, "")
    : Array.from(crypto.getRandomValues(new Uint8Array(16)))
        .map(b => b.toString(16).padStart(2, "0")).join("");
  return "web:" + u;
}
// Web operation-ref generator — the ONLY one. Exposed for the shared
// process-flow entry so both surfaces mint identity through this single
// source (no second UUID implementation anywhere).
window.newOperationRef = newOperationRef;

// ── Result presentation (IO-2): the SAME structure as the /r result
// page renders server-side — link bar first, then ordered items
// (file = preview + one download; text = tinted block + copy; json =
// collapsible code). Single input + single output file adds the
// size-delta line. Structure classes rs-* stay stable (/r page +
// browser suites ride on them). ──
function renderResultItems(items, opts) {
  opts = opts || {};
  const pwa = !!opts.pwa;
  const inputSizes = Array.isArray(opts.inputSizes) ? opts.inputSizes : null;
  const copyBtn = (kind) =>
    '<button class="btn btn-icon btn-sm rs-copy" type="button" data-copy="' + kind +
    '" aria-label="' + t("copy") + '" title="' + t("copy") + '">' + svgIcon("copy") + "</button>";
  const dlControl = (it) => {
    const dlUrl = it.download_url ||
      ("/f/" + encodeURIComponent(it.public_id) + "?download=1");
    return pwa
      ? '<button class="btn btn-secondary btn-sm rs-dl" type="button" data-dl-url="' + escapeHtml(dlUrl) +
        '" data-dl-name="' + escapeHtml(it.filename || "download") + '" aria-label="' + t("downloadFile") +
        '" title="' + t("downloadFile") + '">' + svgIcon("download") + "<span>" + t("downloadFile") + "</span></button>"
      : '<a class="btn btn-secondary btn-sm rs-dl" href="' + escapeHtml(dlUrl) +
        '" download aria-label="' + t("downloadFile") + '" title="' + t("downloadFile") + '">' +
        svgIcon("download") + "<span>" + t("downloadFile") + "</span></a>";
  };
  const fileItems = (items || []).filter((it) => it.kind === "file");
  const showDelta = inputSizes && inputSizes.length === 1 && fileItems.length === 1
    && fileItems[0].size;
  const fileRow = (it) => {
    const ct = it.content_type || "";
    const isImg = ct.startsWith("image/") && ct.toLowerCase() !== "image/svg+xml";
    const sizeTxt = showDelta
      ? fmtSize(inputSizes[0]) + " → " + fmtSize(it.size || 0)
      : fmtSize(it.size || 0);
    let delta = "";
    if (showDelta && inputSizes[0] > 0) {
      const pct = Math.round((1 - (it.size || 0) / inputSizes[0]) * 100);
      if (pct !== 0) {
        const down = pct > 0;
        delta = '<span class="delta' + (down ? "" : " up") + '">' + (down ? "↓ " + pct + "%" : "↑ " + (-pct) + "%") + "</span>";
      }
    }
    const pv = isImg
      ? '<img class="pv img" src="/f/' + encodeURIComponent(it.public_id) + '/inline" alt="" loading="lazy" decoding="async">'
      : '<span class="pv doc" aria-hidden="true">' + svgIcon("doc") + "</span>";
    return '<div class="item-file rs-item rs-file">' + pv
      + '<div class="if-body"><b>' + escapeHtml(it.filename || "file") + "</b>"
      + "<small>" + escapeHtml(sizeTxt) + "</small>" + delta + "</div>"
      + '<div class="a">' + dlControl(it) + "</div></div>";
  };
  const rows = (items || []).map(function (it) {
    if (it.kind === "text") {
      return '<div class="item-text rs-item rs-text">'
        + '<div class="lbl"><span>' + escapeHtml(t("r_text_out")) + "</span>" + copyBtn("text") + "</div>"
        + escapeHtml(it.text || "") + "</div>";
    }
    if (it.kind === "json") {
      const rendered = JSON.stringify(it.data, null, 2);
      return '<div class="rs-item rs-json">'
        + '<details class="rs-json-details" open>'
        + "<summary>" + escapeHtml(t("r_json_out")) + "</summary>"
        + '<div class="rs-body"><pre class="rs-json">' + escapeHtml(rendered) + "</pre></div>"
        + "</details>"
        + '<div class="rs-item-head rs-json-head">' + copyBtn("json") + "</div></div>";
    }
    return fileRow(it);
  }).join("");
  return rows;
}

function wireResultItemActions() {
  if (!result) return;
  result.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      let text = "";
      if (btn.dataset.copy === "link") {
        const bar = btn.closest(".linkbar");
        const span = bar && bar.querySelector("span");
        text = span ? span.textContent : "";
      } else {
        const item = btn.closest(".rs-item");
        const pre = item ? item.querySelector(".rs-text, .rs-json") : null;
        text = pre ? pre.textContent : "";
      }
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        const orig = btn.innerHTML;
        btn.innerHTML = svgIcon("check");
        btn.style.color = "var(--ok)";
        setTimeout(() => { btn.innerHTML = orig; btn.style.color = ""; }, 1400);
      } catch (e) { /* clipboard blocked — text stays selectable */ }
    });
  });
  result.querySelectorAll("[data-dl-url]").forEach((btn) => {
    btn.addEventListener("click", () => {
      window.pwaDownload && window.pwaDownload(btn.dataset.dlUrl, btn.dataset.dlName);
    });
  });
  // Standalone PWA: download anchors route through the engine authority
  // (common.js wirePwaDownloadLinks — no in-window navigation).
  window.wirePwaDownloadLinks && window.wirePwaDownloadLinks(result);
}

function showItemsResult(res) {
  // items result record ({result_id, items, expires_at, …}) → the shared
  // result card (link bar first + items; same structure the /r page
  // renders); the link exposes the durable shareable view.
  const el = result || document.getElementById("result");
  if (!el) return;
  const pwa = !!(typeof window.isStandalonePWA === "function"
    && window.isStandalonePWA());
  const resultId = res.result_id ||
    (res.result_url ? String(res.result_url).split("/").pop() : "");
  const linkUrl = res.result_url ||
    (resultId ? location.origin + "/r/" + encodeURIComponent(resultId) : "");
  const files = (res.items || []).filter(it => it.kind === "file");
  const dlAll = files.length > 1
    ? '<button class="btn btn-secondary btn-sm" id="resDownloadAll" type="button">' + svgIcon("download") +
      "<span>" + t("r_download_all") + " (" + files.length + ")</span></button>"
    : "";
  const page = resultId
    ? '<a class="btn btn-ghost btn-sm" href="/r/' + encodeURIComponent(resultId) + '">' +
      svgIcon("eye") + "<span>" + t("resultPage") + "</span></a>"
    : "";
  const linkbar = linkUrl
    ? '<div class="linkbar">' + svgIcon("link")
      + "<span>" + escapeHtml(linkUrl) + "</span>"
      + '<button class="btn btn-p rs-copy" type="button" data-copy="link">' + svgIcon("copy")
      + "<span>" + t("copy_link") + "</span></button></div>"
    : "";
  const rows = renderResultItems(res.items, { pwa: pwa, inputSizes: window.__lastInputSizes || null });
  window.__lastInputSizes = null;
  el.innerHTML =
    '<div class="out result-card">'
    + '<div class="out-h"><span class="ok" aria-hidden="true">' + svgIcon("check") + "</span>"
    + "<b>" + t("resultReady") + '</b><span class="t">' + dlAll + page + "</span></div>"
    + '<div class="out-b">'
    + linkbar
    + (rows || '<p class="empty">' + t("resultEmpty") + "</p>")
    + "</div></div>";
  el.classList.add("has-result");
  wireResultItemActions();
  const allBtn = el.querySelector("#resDownloadAll");
  if (allBtn) allBtn.addEventListener("click", () => {
    el.querySelectorAll(".rs-dl").forEach((a, i) => {
      setTimeout(() => a.click(), i * 400);
    });
  });
}

function showShareResult(res) {
  lastErrorKey = null;
  if (res && Array.isArray(res.items)) {
    showItemsResult(res);
    return;
  }
  // Not an items record — a plain failure of shape (defensive; every
  // execution result since IO-1 is an items record).
  showItemsResult({ items: [] });
}

window.showResult = showResult;
window.showError = showError;
window.showToolError = showToolError;
window.showToolProcessing = showToolProcessing;
window.showShareResult = showShareResult;
window.showItemsResult = showItemsResult;
window.renderResultItems = renderResultItems;
window.fmtSize = fmtSize;
window.fmtDate = fmtDate;
window.escapeHtml = escapeHtml;

window.applyI18n();
