#!/usr/bin/env node
/* UI error/retry contract: showToolError retry-callback semantics.
 *
 * Loads the ACTUAL static/app.js in a vm sandbox with a minimal DOM and
 * asserts the task-2 contract:
 *   - showToolError WITHOUT a callback renders NO Retry button
 *   - showToolError WITH a callback renders a Retry button wired to it
 *   - unknown tool_failed maps to the user-facing "failed" text (never
 *     the raw machine code) via the common.js _errm authority
 * Also verifies detail.js / profile.js contain no raw `window.t("...")`
 * template literals (VIS-003 class) and no native confirm() on core
 * destructive ops (VIS-006 closure).
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const REPO = path.resolve(__dirname, "..", "..");
// helper shared with the ui-error contract: get the result element a
// sandbox's app.js closed over
function resultShared(ctx) { return ctx.document.__resultEl; }

// ── minimal DOM ──
const elements = {};
function fakeEl(id) {
  const el = {
    id, listeners: {}, innerHTML: "", textContent: "", value: "",
    classList: { add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false },
    style: {}, dataset: {}, href: "", src: "", disabled: false, files: [],
    setAttribute: () => {}, getAttribute: () => null, removeAttribute: () => {},
    appendChild: () => {}, focus: () => {}, select: () => {}, click: () => {},
    addEventListener: (ev, fn) => { el.listeners[ev] = fn; },
    removeEventListener: () => {},
    querySelector: () => null, querySelectorAll: () => [], closest: () => null,
  };
  return el;
}
const sandbox = vm.createContext({
  console, setTimeout: (fn) => fn(), clearTimeout: () => {},
  location: { href: "https://ci.test/", reload: () => {} },
  URL, URLSearchParams, TextEncoder, TextDecoder,
});
sandbox.window = sandbox;
const resultEl = fakeEl("result");
sandbox.document = {
  getElementById: (id) => (id === "result" ? resultEl : (elements[id] = elements[id] || fakeEl(id))),
  __resultEl: resultEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => fakeEl(tag),
  addEventListener: () => {}, removeEventListener: () => {},
  documentElement: fakeEl("html"), head: fakeEl("head"), body: fakeEl("body"),
  cookie: "",
};
sandbox.navigator = { clipboard: { writeText: async () => {} }, onLine: true };
sandbox.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
sandbox.matchMedia = () => ({ matches: false });
sandbox.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
sandbox.confirm = () => { throw new Error("native confirm() must not be called"); };
sandbox.__I18N__ = { en: {} };
sandbox.t = (k) => ({ failed: "Upload failed", retry: "Retry", confirm: "Confirm",
  cancel: "Cancel", delete: "Delete", confirmDelete: "Delete this file?",
  uploading: "Uploading", processing: "Processing...", downloadFile: "Download",
  ready: "Link ready", shareText: "Shared via 518.PLUS", shareImg: "Share image",
  copy: "Copy", shareX: "Share on X", zip_preview_btn: "Preview ZIP",
  print_btn: "Print", share: "Share",
  err_unexpected: "Something went wrong. Please try again." }[k] || k);
sandbox.window.t = sandbox.t;
sandbox.__USER__ = { id: 1 };
sandbox.__TOOLS__ = [];
sandbox.__LANG__ = "en";
sandbox.window.__ICONS__ = { check: "<path d='check'/>", copy: "<path/>", info: "<path/>" };

// i18n.js engine pieces app.js expects at load
sandbox.applyI18n = () => {};
sandbox.window.applyI18n = sandbox.applyI18n;
// common.js helpers (app.js may reference them through pages chain)
vm.runInContext(fs.readFileSync(path.join(REPO, "static", "pages", "common.js"), "utf8"), sandbox);

const appSrc = fs.readFileSync(path.join(REPO, "static", "app.js"), "utf8");
vm.runInContext(appSrc, sandbox, { filename: "app.js" });

(async () => {
  // 0. app.js actually loaded — showToolError must be a global function
  assert.strictEqual(typeof sandbox.showToolError, "function", "app.js loaded, showToolError exposed");

  // app.js closes over `result` (getElementById("result")) at load time —
  // one stable element; each block clears it instead of replacing it.
  // (resultEl is defined once at sandbox setup, above.)

  // 1. no callback → NO retry button in the rendered HTML
  {
    resultEl.innerHTML = "";
    sandbox.showToolError("Upload failed", null);
    assert.ok(resultEl.innerHTML.includes("error-note"), "error card rendered");
    assert.ok(!resultEl.innerHTML.includes("toolRetry"), "NO Retry button without callback (dead-button ban)");
  }

  // 2. with callback → Retry button present and wired
  {
    resultEl.innerHTML = "";
    let clicked = 0;
    sandbox.showToolError("Upload failed", null, () => { clicked++; });
    assert.ok(resultEl.innerHTML.includes("toolRetry"), "Retry button rendered with callback");
    const btn = sandbox.document.getElementById("toolRetry");
    assert.ok(btn && btn.listeners.click, "Retry listener attached");
    btn.listeners.click();
    assert.strictEqual(clicked, 1, "Retry callback fires");
  }

  // 3. machine code never surfaces: the unified boundary maps unknown codes
  //    to the canonical localized generic failure (#42)
  assert.strictEqual(sandbox.window._errm("tool_failed"),
    "Something went wrong. Please try again.",
    "unknown tool_failed → canonical generic failure text, never raw code");

  // 3b. server error codes with dedicated i18n text resolve through the
  //     same boundary (never generic, never raw); {n} fills from the
  //     injected limits projection and strips cleanly without it
  {
    const codes = {
      err_upload_in_progress: "An upload is settling — retry the same action.",
      param_too_long: "A text parameter is longer than {n} characters.",
      err_metering_failed: "Billing could not be completed.",
      err_upload_failed: "The upload failed.",
      err_ingress_unresolved: "A file could not be resolved.",
      err_items_shape: "Malformed result.",
      err_items_too_large: "Result too large.",
      err_items_too_many: "Too many items.",
    };
    const prevT = sandbox.t;
    sandbox.t = (k) => codes[k] || prevT(k);
    sandbox.window.t = sandbox.t;
    sandbox.__LIMITS__ = { textParamMax: 100000 };
    for (const [code, text] of Object.entries(codes)) {
      const expected = code === "param_too_long"
        ? "A text parameter is longer than 100000 characters."
        : text;
      assert.strictEqual(sandbox.window._errm(code), expected,
        `_errm(${code}) maps to its localized text`);
    }
    delete sandbox.__LIMITS__;
    assert.strictEqual(sandbox.window._errm("param_too_long"),
      "A text parameter is longer than characters.",
      "no projection → {n} stripped from the sentence");
    sandbox.t = prevT; sandbox.window.t = prevT;
  }

  // 4. static source bans: no raw window.t("...") inside HTML strings in
  //    detail.js (VIS-003), no native confirm( in profile/tokens/footer
  const detail = fs.readFileSync(path.join(REPO, "static", "pages", "detail.js"), "utf8");
  assert.ok(!/'>window\.t\(|>window\.t\(/.test(detail), "detail.js has no raw window.t literal in HTML strings");
  assert.ok(!/'__PAGE__\.icons/.test(detail), "detail.js uses the canonical icon renderer");
  // RC-VIS-003: behavioral proof — Escape closes the visible detail toolModal.
  {
    const dEls = {};
    const dEl = (id) => (dEls[id] = dEls[id] || {
      id, listeners: {}, innerHTML: "",
      classList: { _h: true, add(c) { if (c === "hidden") this._h = true; },
                   remove(c) { if (c === "hidden") this._h = false; },
                   contains(c) { return c === "hidden" ? this._h : false; } },
      addEventListener: (ev, fn) => { dEls[id].listeners[ev] = fn; },
      getAttribute: () => null, setAttribute: () => {}, appendChild: () => {},
      querySelector: () => null, querySelectorAll: () => [], dataset: {},
      textContent: "", style: {}, focus: () => {}, value: "",
    });
    const docListeners = {};
    const dsb = vm.createContext({
      console, setTimeout: (fn) => fn(), clearTimeout: () => {},
      location: { href: "https://ci.test/d", reload: () => {} },
      URL, URLSearchParams,
    });
    dsb.window = dsb;
    dsb.addEventListener = (ev, fn) => { docListeners[ev] = fn; };
    dsb.document = {
      getElementById: (id) => dEl(id),
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => dEl("created"),
      addEventListener: (ev, fn) => { docListeners[ev] = fn; },
      removeEventListener: () => {},
      documentElement: dEl("html"), head: dEl("head"), body: dEl("body"), cookie: "",
    };
    dsb.navigator = { clipboard: { writeText: async () => {} } };
    dsb.matchMedia = () => ({ matches: false });
    dsb.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
    dsb.t = sandbox.t; dsb.window.t = dsb.t;
    dsb.__I18N__ = { en: {} }; dsb.__LANG__ = "en";
    dsb.__PAGE__ = { publicId: "p1", dlUrl: "/f/p1?download=1", fileName: "a.png" };
    dsb.iconSvg = () => "<svg></svg>";
    try {
      vm.runInContext(detail, dsb, { filename: "detail.js" });
    } catch (e) {
      // detail.js touches more DOM than we mock; only the Escape contract
      // matters here — require the keydown listener registered before any throw
    }
    assert.ok(docListeners.keydown, "detail.js registers a document keydown (Escape) handler");
    // modal hidden by default → open state, then Escape must re-hide
    dEls.toolModal.classList.remove("hidden");
    assert.strictEqual(dEls.toolModal.classList.contains("hidden"), false, "modal open");
    docListeners.keydown({ key: "Escape", preventDefault: () => {} });
    assert.strictEqual(dEls.toolModal.classList.contains("hidden"), true,
      "Escape closes the visible toolModal (RC-VIS-003)");
  }
  for (const f of ["profile.js", "keys.js", "footer.js"]) {
    const src = fs.readFileSync(path.join(REPO, "static", "pages", f), "utf8");
    // strip comments, then ban a REAL native confirm( call (window.confirmAction
    // and the word "confirm" in identifiers/text must not match)
    const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    assert.ok(!/(?<![.\w"'`])confirm\s*\(/.test(code), f + " does not call native confirm()");
  }
  const appjs = fs.readFileSync(path.join(REPO, "static", "app.js"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  assert.ok(!/(?<![.\w"'`])confirm\s*\(/.test(appjs), "app.js does not call native confirm()");

  // 5. second-executable-pipeline ban (task 1): app.js no longer owns
  //    lastToolSource / lastUploadResult / retryTool
  assert.ok(!appjs.includes("lastToolSource"), "app.js: lastToolSource removed");
  assert.ok(!appjs.includes("lastUploadResult"), "app.js: lastUploadResult removed");
  assert.ok(!/function\s+retryTool|retryTool\s*=/.test(appjs), "app.js: retryTool removed");
  const toolInput = fs.readFileSync(path.join(REPO, "static", "pages", "tool-input.js"), "utf8");
  assert.ok(toolInput.includes("runToolFlow"), "tool-input.js delegates to the process-flow authority");
  assert.ok(!appjs.includes("new XMLHttpRequest"), "app.js owns no upload pipeline (input component + process-flow only)");

  // 6. PWA detection authority (RC-VIS-001): base-engine.js owns the ONLY
  //    isStandalonePWA; app.js must not declare a same-name top-level
  //    function (that declaration shadowed the engine property and made
  //    every success path recurse → RangeError).
  assert.ok(!/function\s+isStandalonePWA\s*\(/.test(appjs),
    "app.js: no top-level function isStandalonePWA (engine authority shadow ban)");
  assert.ok(!/(?:^|\n)\s*(?:const|let|var)\s+isStandalonePWA\s*=/.test(appjs),
    "app.js: no isStandalonePWA alias/second detection");
  const commonSrc = fs.readFileSync(path.join(REPO, "static", "pages", "common.js"), "utf8");
  assert.ok(/wirePwaDownloadLinks[\s\S]*window\.isStandalonePWA\(\)/.test(commonSrc),
    "common.js: wirePwaDownloadLinks consumes the canonical window.isStandalonePWA()");
  assert.ok(appjs.includes("wirePwaDownloadLinks"),
    "app.js: result wiring routes downloads through the shared PWA authority");

  // 7. behavioral: the success path itself must not recurse. Simulate the
  //    exact failure mode: engine loaded, then app.js — call showShareResult
  //    with a fake file and assert a result card renders (no RangeError).
  {
    // minimal engine pieces app.js expects before its own load
    const engineResultEl = fakeEl("result");
    const engineSandbox = vm.createContext({
      console, setTimeout: (fn) => fn(), clearTimeout: () => {},
      location: { href: "https://ci.test/", reload: () => {} },
      URL, URLSearchParams,
    });
    engineSandbox.window = engineSandbox;
    engineSandbox.addEventListener = () => {}; engineSandbox.removeEventListener = () => {};
    engineSandbox.document = {
      getElementById: (id) => (id === "result" ? engineResultEl : fakeEl(id)),
      __resultEl: engineResultEl,
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => fakeEl("x"), addEventListener: () => {}, removeEventListener: () => {},
      documentElement: fakeEl("html"), head: fakeEl("head"), body: fakeEl("body"), cookie: "",
    };
    engineSandbox.navigator = { sendBeacon: () => {} };
    engineSandbox.matchMedia = (q) => ({ matches: q.includes("standalone") });
    engineSandbox.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
    vm.runInContext(
      fs.readFileSync(path.join(REPO, "static", "pages", "base-engine.js"), "utf8"),
      engineSandbox, { filename: "base-engine.js" });
    assert.strictEqual(engineSandbox.isStandalonePWA(), true, "engine authority live in sandbox");

    // now load app.js INTO THE SAME context (engine present, like home.html)
    const shared = engineSandbox;
    shared.t = sandbox.t;
    shared.window.t = sandbox.t;
    shared.applyI18n = () => {};
    shared.window.applyI18n = shared.applyI18n;
    shared.__USER__ = { id: 1 };
    shared.__TOOLS__ = [];
    shared.__LANG__ = "en";
    shared.__I18N__ = { en: {} };
    shared.__ICONS__ = { check: "<path d='c'/>", copy: "<path/>", info: "<path/>", download: "<path/>", share: "<path/>", qr: "<path/>", print: "<path/>", eye: "<path/>" };
    vm.runInContext(fs.readFileSync(path.join(REPO, "static", "pages", "common.js"), "utf8"), shared);
    let shareErr = null;
    try {
      vm.runInContext(appSrc, shared, { filename: "app.js" });
      shared.showShareResult({
        resultId: "r1",
        items: [
          { kind: "text", text: "done" },
          { kind: "file", filename: "out.png", contentType: "image/png",
            size: 1234, publicId: "abc" },
        ],
      });
    } catch (e) { shareErr = e; }
    assert.ok(!shareErr, "showShareResult must not throw with engine loaded (recursion regression): " + (shareErr && shareErr.message));
    assert.ok(resultShared(shared).innerHTML.includes("result-card"),
      "success result card rendered in shared context");
  }

  // 8. standalone PWA download branch still wired through the engine
  //    authority (pwaDownload listener attached when engine says standalone)
  {
    const s2 = vm.createContext({
      console, setTimeout: (fn) => fn(), clearTimeout: () => {},
      location: { href: "https://ci.test/", reload: () => {} },
      URL, URLSearchParams,
    });
    s2.window = s2;
    s2.addEventListener = () => {}; s2.removeEventListener = () => {};
    const s2ResultEl = fakeEl("result");
    s2.document = {
      getElementById: (id) => (id === "result" ? s2ResultEl : fakeEl(id)),
      __resultEl: s2ResultEl,
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => fakeEl("x"), addEventListener: () => {}, removeEventListener: () => {},
      documentElement: fakeEl("html"), head: fakeEl("head"), body: fakeEl("body"), cookie: "",
    };
    s2.navigator = { clipboard: { writeText: async () => {} } };
    s2.matchMedia = (q) => ({ matches: q.includes("standalone") });
    s2.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
    vm.runInContext(
      fs.readFileSync(path.join(REPO, "static", "pages", "base-engine.js"), "utf8"),
      s2, { filename: "base-engine.js" });
    s2.t = sandbox.t; s2.window.t = s2.t;
    s2.applyI18n = () => {}; s2.window.applyI18n = s2.applyI18n;
    s2.__USER__ = { id: 1 }; s2.__TOOLS__ = []; s2.__LANG__ = "en"; s2.__I18N__ = { en: {} };
    s2.__ICONS__ = { check: "<path d='c'/>", copy: "<path/>", info: "<path/>", download: "<path/>", share: "<path/>", qr: "<path/>", print: "<path/>", eye: "<path/>" };
    vm.runInContext(fs.readFileSync(path.join(REPO, "static", "pages", "common.js"), "utf8"), s2);
    vm.runInContext(appSrc, s2, { filename: "app.js" });
    assert.strictEqual(typeof s2.pwaDownload, "function", "engine pwaDownload present");
    assert.strictEqual(s2.isStandalonePWA(), true, "engine says standalone");
    // app.js items result in standalone: the shared rebind strips every
    // download anchor (no in-window navigation possible)
    s2.showShareResult({
      resultId: "r2",
      items: [
        { kind: "file", filename: "a.zip", contentType: "application/zip",
          size: 10, publicId: "z" },
      ],
    });
    const html = resultShared(s2).innerHTML;
    assert.ok(!/<a[^>]+download\b/.test(html), "standalone: no <a download> anchor left standing");
  }

  // 9. unified error boundary (B5/#42): known → specific localized message,
  //    unknown err_* / exception text → canonical generic; a raw err_* key,
  //    'error' or exception detail never surfaces.
  {
    const b = vm.createContext({
      console, setTimeout: (fn) => fn(), clearTimeout: () => {},
      location: { href: "https://ci.test/", reload: () => {} },
      URL, URLSearchParams,
    });
    b.window = b;
    b.document = { getElementById: () => null, querySelector: () => null,
      querySelectorAll: () => [], createElement: () => null,
      addEventListener: () => {}, documentElement: null, head: null, body: null, cookie: "" };
    const GENERIC = "Something went wrong. Please try again.";
    b.t = (k) => ({
      err_rate_limited: "Too many requests, slow down",
      err_checkout_failed: "Payment could not be started",
      mytools_err_invalid_id: "Invalid source file",
      err_unexpected: GENERIC,
    }[k] || k);
    b.window.t = b.t;
    vm.runInContext(fs.readFileSync(path.join(REPO, "static", "pages", "common.js"), "utf8"), b);
    const _e = b.window._errm;
    assert.strictEqual(_e("err_rate_limited"), "Too many requests, slow down",
      "known code → specific localized message");
    assert.strictEqual(_e("checkout_failed"), "Payment could not be started",
      "bare checkout code resolves through the err_ family");
    assert.strictEqual(_e("invalid_id"), "Invalid source file",
      "mytools_* codes resolve through the mytools_err_ family");
    assert.strictEqual(_e("err_totally_unknown_xyz"), GENERIC,
      "unknown err_* → canonical generic");
    assert.ok(!_e("err_totally_unknown_xyz").includes("err_"),
      "raw err_* key never surfaces");
    assert.strictEqual(_e("boom: psycopg2 detail SECRET"), GENERIC,
      "exception text → generic, detail dropped");
    assert.strictEqual(_e(undefined), GENERIC, "missing code → generic");
    assert.strictEqual(_e(""), GENERIC, "empty code → generic (never literal 'error')");
  }

  // 10. source bans for the boundary rollout (B5): no second error
  //     dictionary and no raw alert(error) fallbacks in the page scripts
  {
    const detail = fs.readFileSync(path.join(REPO, "static", "pages", "detail.js"), "utf8");
    assert.ok(!detail.includes("_errMap"), "detail.js: second error dictionary removed");
    assert.ok(detail.includes("_errm(msg)"), "detail.js routes through the unified boundary");
    for (const f of ["pricing.js", "keys.js", "profile.js", "earnings.js",
                     "publish.js", "tools.js"]) {
      const src = fs.readFileSync(path.join(REPO, "static", "pages", f), "utf8");
      assert.ok(!/\|\|\s*['"]error['"]/.test(src), f + ": no raw 'error' fallback");
      assert.ok(!/alert\((?:d|dd|r\.data)?\.?error\)/.test(src), f + ": no alert(raw error)");
    }
    const pricing = fs.readFileSync(path.join(REPO, "static", "pages", "pricing.js"), "utf8");
    assert.ok(pricing.includes("_errm(why)"), "pricing.js checkout errors go through _errm");
  }

  console.log("PASS: UI error/retry contract (11 groups)");
})().catch((e) => { console.error(e); process.exit(1); });
