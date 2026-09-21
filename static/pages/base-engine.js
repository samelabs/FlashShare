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
    window.t=function(k,v){var I=window.__I18N__||{};var T=I[window.__LANG]||I.en||{};var s=T[k]||k;if(v){for(var p in v)s=s.split("{"+p+"}").join(v[p])}return s};
    window.applyI18n=function(){
      var L=window.__LANG||"en";
      document.documentElement.lang=window.__LOCALE_MAP[L]||"en";
      document.documentElement.dir=L==="ar"?"rtl":"ltr";
      document.querySelectorAll("[data-i18n]").forEach(function(el){try{el.textContent=window.t(el.getAttribute("data-i18n"))}catch(err){}});
      document.querySelectorAll("[data-i18n-placeholder]").forEach(function(el){try{el.setAttribute("placeholder",window.t(el.getAttribute("data-i18n-placeholder")))}catch(err){}});
      if(typeof window.__afterI18n==="function")window.__afterI18n();
    };
    // Language switcher
    (function(){
      if(window.__LANG_INIT)return;window.__LANG_INIT=true;
      var lb=document.getElementById("langButton"),lm=document.getElementById("langMenu");
      if(!lb||!lm)return;
      lb.textContent=window.__LANG_LABELS[window.__LANG]||"English";
      lb.addEventListener("click",function(){var h=lm.classList.toggle("hidden");lb.setAttribute("aria-expanded",h?"false":"true")});
      lm.innerHTML=window.__SUPPORTED_LANGS.map(function(c){return'<button class="lang-option'+(c===window.__LANG?" active":"")+'" type="button" role="option" aria-selected="'+(c===window.__LANG)+'" data-lang="'+c+'">'+window.__LANG_LABELS[c]+"</button>"}).join("");
      lm.addEventListener("click",function(e){var o=e.target.closest("[data-lang]");if(!o)return;var l=window.__SUPPORTED_LANGS.indexOf(o.dataset.lang)>=0?o.dataset.lang:"en";window.__LANG=l;document.cookie="flashshare_lang="+l+";Path=/;Max-Age=31536000;SameSite=Lax";if(typeof window.__afterI18n==="function"){lb.textContent=window.__LANG_LABELS[l]||"English";lm.classList.add("hidden");lb.setAttribute("aria-expanded","false");lm.querySelectorAll(".lang-option").forEach(function(b){b.classList.toggle("active",b.dataset.lang===l);b.setAttribute("aria-selected",b.dataset.lang===l)});window.applyI18n()}else{location.reload()}});
      document.addEventListener("click",function(e){if(e.target===lb||lm.contains(e.target))return;lm.classList.add("hidden");lb.setAttribute("aria-expanded","false")});
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
 * (result card, history, detail page) call window.pwaDownload /
 * window.shareFileViaURL; nobody re-implements the standalone branch.
 *
 * Standalone hard constraints (product decision):
 *   - never fetch the file body, never build Blob/File for downloads
 *   - never navigate the current PWA window to the download URL
 *   - Web Share available → share the URL itself ({title, url});
 *     user cancel (AbortError) is a normal end, no fallback
 *   - no Web Share → secondary browsing context (_blank) handoff
 *   - non-standalone browsers keep the plain download behavior
 */
window.isStandalonePWA = function () {
  return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches)
      || window.navigator.standalone === true;
};
window.pwaDownload = async function (downloadUrl, title) {
  // standalone-only path; callers must use plain download elsewhere
  title = title || "download";
  if (navigator.share) {
    try {
      await navigator.share({ title: title, url: downloadUrl });
    } catch (e) {
      if (e && e.name === "AbortError") return true; // user cancel = normal end
      // share failed: secondary context handoff — NEVER current-window nav
      _pwaSecondaryHandoff(downloadUrl);
    }
    return true;
  }
  _pwaSecondaryHandoff(downloadUrl);
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
// Explicit "share the FILE" action (distinct from download): may read the
// File, but must gate on canShare({files}) and, inside a standalone PWA,
// must never fall back to navigating the current window.
window.shareFileViaURL = async function (url, filename, btn) {
  var orig = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<div class="btn-spinner"></div>'; }
  var restore = function () { if (btn) { btn.disabled = false; btn.innerHTML = orig; } };
  var filesShareable = !!(navigator.canShare && navigator.share);
  if (filesShareable) {
    try {
      var res = await fetch(url);
      if (res.ok) {
        var blob = await res.blob();
        var file = new File([blob], filename || "download", { type: blob.type || "application/octet-stream" });
        if (navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file], title: filename || "download" });
          restore();
          return true;
        }
      }
    } catch (e) {
      if (e && e.name === "AbortError") { restore(); return true; }
      // fall through: share URL instead — never navigate current window
    }
  }
  try {
    if (navigator.share) { await navigator.share({ title: filename || "download", url: url }); restore(); return true; }
  } catch (e) {
    if (e && e.name === "AbortError") { restore(); return true; }
  }
  _pwaSecondaryHandoff(url);
  restore();
  return true;
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
        '<button class="action-btn" id="confirmModalCancel" type="button"></button>' +
        '<button class="action-btn primary" id="confirmModalOk" type="button"></button>' +
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
