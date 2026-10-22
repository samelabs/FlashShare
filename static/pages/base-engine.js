// ── Error tap: surface client-side crashes to the server log ──
window.__errTap = function(msg, stack) {
  try {
    var body = JSON.stringify({msg: String(msg).slice(0, 300), stack: String(stack || "").slice(0, 1500), lang: window.__LANG, url: location.pathname});
    if (navigator.sendBeacon) navigator.sendBeacon("/api/client-error", body);
  } catch (e) {}
};
window.addEventListener("error", function(e) {
  if (e.error && /Maximum call stack/i.test(e.message)) {
    document.title = "ERR:stack";
    window.__errTap(e.message, e.error.stack);
  } else if (e.error) {
    window.__errTap(e.message, e.error.stack);
  }
});
window.addEventListener("unhandledrejection", function(e) { window.__errTap(e.reason, ""); });

// ── Global i18n engine ──
    window.__SUPPORTED_LANGS=["en","zh","ja","ko","es","ar","de","ru","pt","fr"];
    window.__LOCALE_MAP={en:"en",zh:"zh-CN",ja:"ja-JP",ko:"ko-KR",es:"es-ES",ar:"ar",de:"de-DE",ru:"ru-RU",pt:"pt-BR",fr:"fr-FR"};
    window.__LANG_LABELS={"en":"English","zh":"简体中文","ja":"日本語","ko":"한국어","es":"Español","ar":"العربية","de":"Deutsch","ru":"Русский","pt":"Português","fr":"Français"};
        window.__afterI18n=null;
    window.t=function(k,v){var I=window.__I18N__||{};var T=I[window.__LANG]||I.en||{};var E=I.en||{};var s=T[k]||E[k]||k;if(v){for(var p in v)s=s.split("{"+p+"}").join(v[p])}return s};
    window.applyI18n=function(){
      var L=window.__LANG||"en";
      document.documentElement.lang=window.__LOCALE_MAP[L]||"en";
      document.documentElement.dir=L==="ar"?"rtl":"ltr";
      document.querySelectorAll("[data-i18n]").forEach(function(el){try{el.textContent=window.t(el.getAttribute("data-i18n"))}catch(err){}});
      document.querySelectorAll("[data-i18n-placeholder]").forEach(function(el){try{el.setAttribute("placeholder",window.t(el.getAttribute("data-i18n-placeholder")))}catch(err){}});
      if(typeof window.__afterI18n==="function")window.__afterI18n();
    };
    // Language switcher — header top bar only (the single site-wide
    // entry; visible on every viewport)
    (function(){
      if(window.__LANG_INIT)return;window.__LANG_INIT=true;
      var mounts=Array.prototype.slice.call(document.querySelectorAll(".lang-switch"));
      if(!mounts.length)return;
      function langOptions(){return window.__SUPPORTED_LANGS.map(function(c){return'<button class="lang-option'+(c===window.__LANG?" active":"")+'" type="button" role="option" aria-selected="'+(c===window.__LANG)+'" data-lang="'+c+'">'+window.__LANG_LABELS[c]+"</button>"}).join("")}
      function closeAll(){mounts.forEach(function(m){var b=m.querySelector("button[aria-haspopup]"),u=m.querySelector(".lang-menu");if(b)b.setAttribute("aria-expanded","false");if(u)u.classList.add("hidden")})}
      mounts.forEach(function(m){
        var lb=m.querySelector("button[aria-haspopup]"),lm=m.querySelector(".lang-menu");
        if(!lb||!lm)return;
        lb.title=window.__LANG_LABELS[window.__LANG]||"English";
        lb.addEventListener("click",function(e){e.stopPropagation();var willOpen=lm.classList.contains("hidden");closeAll();if(willOpen){lm.classList.remove("hidden");lb.setAttribute("aria-expanded","true")}});
        lm.innerHTML=langOptions();
        lm.addEventListener("click",function(e){var o=e.target.closest("[data-lang]");if(!o)return;var l=window.__SUPPORTED_LANGS.indexOf(o.dataset.lang)>=0?o.dataset.lang:"en";window.__LANG=l;document.cookie="flashshare_lang="+l+";Path=/;Max-Age=31536000;SameSite=Lax";if(typeof window.__afterI18n==="function"){mounts.forEach(function(mm){var bb=mm.querySelector("button[aria-haspopup]"),uu=mm.querySelector(".lang-menu");if(bb)bb.title=window.__LANG_LABELS[l]||"English";if(uu){uu.classList.add("hidden");uu.querySelectorAll(".lang-option").forEach(function(b){b.classList.toggle("active",b.dataset.lang===l);b.setAttribute("aria-selected",b.dataset.lang===l)})}});lb.setAttribute("aria-expanded","false");window.applyI18n()}else{location.reload()}});
      });
      document.addEventListener("click",closeAll);
    })();

// ── Top-bar interactions (site-wide single source) ────────────────
// Every header control lives HERE because only this file is loaded by
// base.html on every page: the mobile nav drawer, the user dropdown,
// the history modal open/close (history DATA loading stays in
// footer.js) and the language menus (binder above).
// Mobile top-bar navigation sheet: visible only at narrow widths (CSS),
// toggled by the menu button; closes on backdrop / outside click / Escape /
// nav. The backdrop is the touch-friendly outside.
window.wireMobileNav = function () {
  const btn = document.getElementById("mobileNavBtn");
  const sheet = document.getElementById("mobileNavSheet");
  const backdrop = document.getElementById("mobileNavBackdrop");
  if (!btn || !sheet || btn.dataset.wired) return;
  btn.dataset.wired = "1";
  const setOpen = (open) => {
    sheet.classList.toggle("hidden", !open);
    if (backdrop) backdrop.classList.toggle("hidden", !open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(sheet.classList.contains("hidden"));
  });
  if (backdrop) backdrop.addEventListener("click", () => setOpen(false));
  document.addEventListener("click", (e) => {
    if (!sheet.contains(e.target) && e.target !== btn) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setOpen(false);
  });
  sheet.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => setOpen(false)));
};
document.addEventListener("DOMContentLoaded", () => window.wireMobileNav());

// User dropdown menu (site-wide)
(function(){var b=document.getElementById("userMenuBtn"),d=document.getElementById("userDropdown");if(b&&d){b.addEventListener("click",function(e){e.stopPropagation();var h=d.classList.toggle("hidden");b.setAttribute("aria-expanded",h?"false":"true")});document.addEventListener("click",function(e){if(e.target===b||d.contains(e.target))return;d.classList.add("hidden");b.setAttribute("aria-expanded","false")})}})();

// History modal open/close (data loading = footer.js _loadHistory)
(function(){
var hb=document.getElementById("historyBtn"),hm=document.getElementById("historyModal"),hc2=document.getElementById("historyModalClose");
if(hb&&hm){hb.addEventListener("click",function(){hm.classList.remove("hidden");var h2=hm.querySelector("h2");if(h2)h2.textContent=window.t("recent");window._loadHistory&&window._loadHistory()});if(hc2)hc2.addEventListener("click",function(){hm.classList.add("hidden")});hm.addEventListener("click",function(e){if(e.target===hm&&!hm.dataset.locked&&!window._dlLock)hm.classList.add("hidden")})}
})();

/*
 * LocalizedText protocol resolver (JS twin of governance.tool_params.resolve_label).
 * Accepted shapes: string | {locale: string}
 * Canonical fallback order (the ONLY one on this platform):
 *   requested locale -> "en" -> first non-empty value -> ""
 * Callers MAY apply domain fallback after it (e.g. `|| tool.id`), but MUST NOT
 * implement another locale chain. MUST stay semantically identical to the
 * Python resolver — a protocol test asserts the two behave the same.
 * Search indexing is exempt: data-name intentionally indexes all locale values.
 * Lives on base-engine (site-wide) — every page resolves user tool text.
 */
window.resolveLabel = function (label) {
  if (typeof label === "string") return label;
  if (label && typeof label === "object") {
    // __LANG = platform locale code (zh/en/...), same keys as LocalizedText
    // dicts. documentElement.lang is a BCP-47 tag (zh-CN) — NOT a dict key.
    const lang = (window.currentLang && window.currentLang()) ||
                 window.__LANG ||
                 document.documentElement.lang || "en";
    if (label[lang]) return label[lang];
    if (label.en) return label.en;
    for (const v of Object.values(label)) {
      if (typeof v === "string" && v) return v;
    }
  }
  return "";
};

/*
 * ── PWA download authority (site-wide single point) ─────────────────
 * The ONLY standalone-PWA download policy. All download entries
 * (result card, history, detail page) call window.pwaDownload;
 * nobody re-implements the standalone branch.
 *
 * Standalone hard constraints (product decision):
 *   - never fetch the file body, never build Blob/File for downloads
 *   - never navigate the current PWA window to the download URL
 *   - download = open the download URL in a secondary browsing context
 *     (_blank) — Web Share is NOT part of download; system share lives
 *     only on the explicit share actions (result page share button /
 *     share entry on the result card)
 *   - non-standalone browsers keep the plain download behavior
 */
window.isStandalonePWA = function () {
  return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches)
      || window.navigator.standalone === true;
};
window.pwaDownload = function (downloadUrl, viewUrl) {
  // standalone-only path; callers must use plain download elsewhere.
  // Images / PDFs open their inline view (a standalone context cannot
  // save an attachment response — it shows a blank sheet; the view can
  // be long-pressed or shared from the viewer). Other types open the
  // download URL.
  _pwaSecondaryHandoff(viewUrl || downloadUrl);
  return true;
};
function _pwaSecondaryHandoff(downloadUrl) {
  try {
    var a = document.createElement("a");
    a.href = downloadUrl;
    a.target = "_blank";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (e) { /* last resort: open in secondary context */ }
}
// Share a link (site-wide single point for every share action: the
// share bar and the history list). navigator.share is called
// synchronously inside the click so the user activation is still valid
// (an awaited fetch first would expire it and the system sheet never
// opens). Never fetches the file (a fetch would also spend a download).
// No Web Share → copy the link and confirm on the button. User cancel
// (AbortError) is a normal end.
function _sbConfirm(btn) {
  if (!btn) return;
  var xGlyph = btn.querySelector(".x-glyph");
  if (xGlyph) {
    // X 分享钮: 𝕏 字形钮走文本形态
    window.copyFeedback(btn);
    return;
  }
  window.copyFeedback(btn);
}
window.shareLink = function (url, title, btn) {
  if (navigator.share) {
    navigator.share({ title: title || document.title, url: url }).catch(function () {});
    return;
  }
  if (navigator.clipboard) {
    navigator.clipboard.writeText(url).then(function () { _sbConfirm(btn); }).catch(function () {});
  }
};
// Share bar markup — the JS twin of partials/share_bar.html, isomorphic
// down to class names, data-sb actions, data-i18n text keys and the X
// intent format. The /r page renders the partial server-side; dynamic
// surfaces (home result card) build the same bar here. Click behavior is
// the delegated handler below — no separate wiring exists.
window.shareBarHtml = function (o) {
  o = o || {};
  var url = String(o.url || "");
  var title = String(o.title || "");
  var xTitle = o.xTitle != null ? String(o.xTitle) : title;
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var ic = function (n) { return window.iconSvg ? window.iconSvg(n) : ""; };
  var tx = function (k) { return window.t ? window.t(k) : k; };
  return '<div class="share-bar' + (o.compact ? " sb-compact" : "") + '"'
    + ' data-share-url="' + esc(url) + '" data-share-title="' + esc(title) + '">'
    + '<button type="button" class="sb-btn" data-sb="copy" aria-label="' + esc(tx("copy_link")) + '" title="' + esc(tx("copy_link")) + '">'
    + ic("copy") + '<span data-i18n="copy_link">' + esc(tx("copy_link")) + "</span></button>"
    + '<button type="button" class="sb-btn" data-sb="share" aria-label="' + esc(tx("share_via_system")) + '" title="' + esc(tx("share_via_system")) + '">'
    + ic("share") + '<span data-i18n="share_via_system">' + esc(tx("share_via_system")) + "</span></button>"
    + '<a class="sb-btn" href="https://x.com/intent/tweet?text=' + encodeURIComponent(xTitle)
    + "&amp;url=" + encodeURIComponent(url) + '" target="_blank" rel="noreferrer" aria-label="X" title="X">'
    + '<span class="x-glyph" aria-hidden="true">𝕏</span><span>X</span></a>'
    + (o.qrSrc
      ? '<button type="button" class="sb-btn" data-sb="qr" data-qr-src="' + esc(o.qrSrc)
      + '" aria-label="' + esc(tx("qrCode")) + '" title="' + esc(tx("qrCode")) + '">'
      + ic("qr") + '<span data-i18n="qrCode">' + esc(tx("qrCode")) + "</span></button>"
      : "")
    + "</div>";
};

// THE site-wide QR modal (single surface: share-bar entries and the
// history list both open it). Loading/error states ride the .qr-code
// classes (VIS-007: spinner until the SVG loads, error surface on a
// failed load — never a blank panel).
function _sbQr(src) {
  var m = document.getElementById("sbQrModal");
  if (!m) {
    m = document.createElement("div");
    m.className = "modal hidden";
    m.id = "sbQrModal";
    m.setAttribute("role", "dialog");
    m.setAttribute("aria-modal", "true");
    m.innerHTML = '<div class="modal-panel qr-modal-panel"><button class="modal-close" type="button" aria-label="Close">'
      + (window.iconSvg ? window.iconSvg("close") : "×") + '</button><img alt="QR" width="200" height="200" class="qr-code"></div>';
    document.body.appendChild(m);
    m.addEventListener("click", function (e) {
      if (e.target === m || (e.target.closest && e.target.closest(".modal-close"))) m.classList.add("hidden");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") m.classList.add("hidden");
    });
  }
  var im = m.querySelector("img");
  im.classList.add("qr-loading");
  im.onload = function () { im.classList.remove("qr-loading", "qr-error"); };
  im.onerror = function () { im.classList.remove("qr-loading"); im.classList.add("qr-error"); };
  im.src = src;
  m.classList.remove("hidden");
}
// Share bar (partials/share_bar.html): copy / system share / QR; the X
// entry is a plain link built server-side for the same url + title.
document.addEventListener("click", function (e) {
  var b = e.target.closest && e.target.closest(".share-bar [data-sb]");
  if (!b) return;
  var bar = b.closest(".share-bar");
  var url = bar.getAttribute("data-share-url") || window.location.href.split("#")[0];
  var act = b.getAttribute("data-sb");
  if (act === "share") window.shareLink(url, bar.getAttribute("data-share-title"), b);
  else if (act === "copy" && navigator.clipboard) {
    // feedback FIRST (sync): a denied/delayed clipboard must never
    // leave the click without response
    window.copyFeedback(b);
    navigator.clipboard.writeText(url).catch(function () {});
  } else if (act === "qr") _sbQr(b.getAttribute("data-qr-src"));
});

// ── Site-wide toast (the single transient notice surface) ──
// Drives the .toast component (components.css). Single-slot: a new
// notice replaces the one on screen — rapid rejections must not stack.
// Text-first by design; no icons, no actions.
window.globalToast = function (message) {
  var old = document.querySelector(".toast");
  if (old) old.remove();
  var el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "status");
  el.textContent = message;
  document.body.appendChild(el);
  requestAnimationFrame(function () { el.classList.add("on"); });
  setTimeout(function () {
    el.classList.remove("on");
    setTimeout(function () { el.remove(); }, 260);
  }, 2600);
};

// ── Site-wide copy feedback (the ONE in-place confirmation for copy) ──
// 交互规范: 复制属"就地确认"——触发钮图标翻对勾(图标钮)或文本变
// 确认文案(文本钮, 走 i18n 的 copied 键), 1.4s 还原; busy 守卫防双击
// 竞态(无守卫时第二击会把对勾当原样保存, 图标卡死)。全局 toast 留给
// 事件通知(错误/成功类), 不打断的就地确认不弹 toast——两条通道分工。
window.copyFeedback = function (btn) {
  if (!btn || btn.dataset.cpBusy) return;
  btn.dataset.cpBusy = "1";
  var origHtml = btn.innerHTML;
  var icon = btn.querySelector("svg");
  if (icon && window.iconSvg) {
    icon.outerHTML = window.iconSvg("check");
  } else {
    btn.textContent = "✓ " + (window.t ? window.t("copied") : "copied");
  }
  btn.classList.add("copied");
  setTimeout(function () {
    btn.innerHTML = origHtml;
    btn.classList.remove("copied");
    delete btn.dataset.cpBusy;
  }, 1400);
};

// ── Site-wide async confirm authority (VIS-006 / RC-VIS-002) ──
// One modal-based confirm for core destructive operations: history
// delete, detail delete, profile X unbind, token revoke, homepage
// upload delete. Lives in base-engine.js because only this file is
// loaded by every page (tokens/profile don't include app.js) — that is
// what "site-wide authority" means here. Replaces native confirm()
// everywhere: backdrop cancel, Escape cancel, focus lands on the
// primary action, focus restored on close — mobile/WebView safe (no
// browser native dialog dependency).
window.confirmAction = function (opts) {
  return new Promise((resolve) => {
    let layer = document.getElementById("confirmModal");
    if (!layer) {
      layer = document.createElement("div");
      layer.id = "confirmModal";
      layer.className = "modal hidden";
      layer.setAttribute("role", "dialog");
      layer.setAttribute("aria-modal", "true");
      layer.innerHTML =
        '<div class="modal-panel confirm-panel">' +
        '<h3 id="confirmModalTitle" class="confirm-title"></h3>' +
        '<p id="confirmModalText" class="confirm-text"></p>' +
        '<div class="tool-panel-actions">' +
        '<button class="btn btn-secondary" id="confirmModalCancel" type="button"></button>' +
        '<button class="btn btn-primary" id="confirmModalOk" type="button"></button>' +
        "</div></div>";
      document.body.appendChild(layer);
    }
    const titleEl = document.getElementById("confirmModalTitle");
    const textEl = document.getElementById("confirmModalText");
    const okBtn = document.getElementById("confirmModalOk");
    const cancelBtn = document.getElementById("confirmModalCancel");
    titleEl.textContent = opts.title || "";
    textEl.textContent = opts.message || "";
    okBtn.textContent = opts.confirmLabel || window.t("confirm");
    cancelBtn.textContent = opts.cancelLabel || window.t("cancel");

    const prevFocus = document.activeElement;
    let settled = false;
    function done(val) {
      if (settled) return;
      settled = true;
      layer.classList.add("hidden");
      document.removeEventListener("keydown", onKey, true);
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      resolve(val);
    }
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); done(false); }
    }
    okBtn.addEventListener("click", () => done(true));
    cancelBtn.addEventListener("click", () => done(false));
    layer.addEventListener("click", (e) => { if (e.target === layer) done(false); });
    document.addEventListener("keydown", onKey, true);
    layer.classList.remove("hidden");
    okBtn.focus(); // focus lands on the primary action
  });
};
