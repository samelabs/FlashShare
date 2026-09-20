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
  uploading: "Uploading", processing: "Processing..." }[k] || k);
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
    assert.ok(resultEl.innerHTML.includes("result-error"), "error card rendered");
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

  // 3. machine code never surfaces: _errm authority falls back to "failed" text
  assert.strictEqual(sandbox.window._errm("tool_failed"), "Upload failed",
    "unknown tool_failed → user-facing failed text, never raw code");

  // 4. static source bans: no raw window.t("...") inside HTML strings in
  //    detail.js (VIS-003), no native confirm( in profile/tokens/footer
  const detail = fs.readFileSync(path.join(REPO, "static", "pages", "detail.js"), "utf8");
  assert.ok(!/'>window\.t\(|>window\.t\(/.test(detail), "detail.js has no raw window.t literal in HTML strings");
  assert.ok(!/'__PAGE__\.icons/.test(detail), "detail.js uses the canonical icon renderer");
  for (const f of ["profile.js", "tokens.js", "footer.js"]) {
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
  assert.ok(appjs.includes("runToolFlow"), "app.js delegates to the process-flow authority");

  console.log("PASS: UI error/retry contract (5 groups)");
})().catch((e) => { console.error(e); process.exit(1); });
