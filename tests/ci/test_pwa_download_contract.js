#!/usr/bin/env node
/* PWA download authority behavior contract: real base-engine.js.
 *
 * Loads the ACTUAL static/pages/base-engine.js in a vm sandbox with a
 * mocked DOM/navigator, then drives window.pwaDownload in standalone
 * mode and asserts the product constraints:
 *   - no fetch of the file body
 *   - no Blob/File construction
 *   - current window location never modified
 *   - Web Share payload is a URL ({title, url}), not files
 *   - AbortError (user cancel) ends normally, no fallback
 *   - without Web Share → secondary-context handoff (_blank anchor),
 *     never current-window navigation
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const REPO = path.resolve(__dirname, "..", "..");

function makeSandbox({ standalone, webShare, shareReject } = {}) {
  const events = {
    fetches: 0, blobs: 0, files: 0, shares: [], locationSet: false,
    anchorClicks: [], openedWindows: 0,
  };
  const sandbox = vm.createContext({
    console,
    setTimeout: (fn) => fn(),
    Math, Date, JSON, Promise, Object, Array, String, Number, Boolean, Error,
    TypeError, RegExp, Symbol,
    location: new Proxy({ href: "https://ci.test/" }, {
      set() { events.locationSet = true; return true; },
    }),
  });
  sandbox.window = sandbox;
  const noop = () => {};
  const fakeElement = () => ({
    addEventListener: noop, style: {}, classList: { add: noop, remove: noop },
    setAttribute: noop, appendChild: noop, remove: noop,
    click() { events.anchorClicks.push(this.href); },
    querySelectorAll: () => [], querySelector: () => null,
    getAttribute: () => null, contains: () => false,
  });
  sandbox.document = {
    getElementById: () => null, querySelector: () => null,
    querySelectorAll: () => [], createElement: fakeElement,
    addEventListener: noop, documentElement: fakeElement(),
    head: fakeElement(), body: fakeElement(), cookie: "",
  };
  sandbox.addEventListener = noop;
  sandbox.matchMedia = (q) => ({ matches: !!standalone && q.includes("standalone") });
  sandbox.navigator = {
    standalone: !!standalone ? true : undefined,
    sendBeacon: noop,
    share: webShare
      ? async (payload) => {
          if (shareReject === "abort") { const e = new Error("c"); e.name = "AbortError"; throw e; }
          if (shareReject === "fail") { throw new Error("boom"); }
          events.shares.push(payload);
        }
      : undefined,
    canShare: undefined,
  };
  sandbox.fetch = async () => { events.fetches++; throw new Error("must not fetch"); };
  const RealFile = class {
    constructor(parts, name) { events.files++; this.parts = parts; this.name = name; }
  };
  sandbox.File = RealFile;
  sandbox.Blob = class { constructor() { events.blobs++; } };
  const src = fs.readFileSync(path.join(REPO, "static", "pages", "base-engine.js"), "utf8");
  vm.runInContext(src, sandbox, { filename: "base-engine.js" });
  return { sandbox, events };
}

(async () => {
  // 0. authority structure (RC-VIS-001): base-engine.js is the ONLY
  //    isStandalonePWA authority; app.js must not re-declare it.
  {
    const appjs = fs.readFileSync(path.join(REPO, "static", "app.js"), "utf8");
    assert.ok(!/function\s+isStandalonePWA\s*\(/.test(appjs),
      "app.js must not declare a top-level isStandalonePWA (recursion shadow)");
    assert.ok(/window\.isStandalonePWA\(\)/.test(appjs),
      "app.js success path consumes the engine isStandalonePWA() (guarded)");
    assert.ok(!/isStandalonePWA\s*:\s*\(\)\s*=>/.test(appjs),
      "app.js carries no second detection (no inline alias)");
  }

  // 1. standalone + Web Share → share URL, zero fetch/blob/file, no nav
  {
    const { sandbox, events } = makeSandbox({ standalone: true, webShare: true });
    assert.strictEqual(sandbox.isStandalonePWA(), true, "standalone detected");
    await sandbox.pwaDownload("/f/x?download=1", "a.png");
    assert.strictEqual(events.fetches, 0, "no fetch in standalone download");
    assert.strictEqual(events.blobs, 0, "no Blob created");
    assert.strictEqual(events.files, 0, "no File created");
    assert.strictEqual(events.locationSet, false, "current location untouched");
    assert.strictEqual(events.shares.length, 1, "one share call");
    const payload = events.shares[0];
    assert.ok(payload.url && !payload.files, "share payload is a URL, not files");
    assert.strictEqual(payload.url, "/f/x?download=1");
    assert.strictEqual(payload.title, "a.png");
    console.log("✓ standalone+WebShare: URL share, no fetch/blob/file/nav");
  }

  // 2. user cancel (AbortError) → normal end, no fallback handoff
  {
    const { sandbox, events } = makeSandbox({ standalone: true, webShare: true, shareReject: "abort" });
    const r = await sandbox.pwaDownload("/f/y?download=1", "b.png");
    assert.strictEqual(r, true, "cancel resolves normally");
    assert.strictEqual(events.anchorClicks.length, 0, "no fallback after cancel");
    assert.strictEqual(events.locationSet, false);
    console.log("✓ AbortError = normal end, no fallback");
  }

  // 3. share failure → secondary context, never current-window nav
  {
    const { sandbox, events } = makeSandbox({ standalone: true, webShare: true, shareReject: "fail" });
    await sandbox.pwaDownload("/f/z?download=1", "c.png");
    assert.strictEqual(events.locationSet, false, "failure never navigates current window");
    assert.strictEqual(events.anchorClicks.length, 1, "secondary-context handoff happened");
    assert.strictEqual(events.anchorClicks[0], "/f/z?download=1");
    console.log("✓ share failure → _blank handoff, no current-window nav");
  }

  // 4. no Web Share → secondary context immediately, zero fetch
  {
    const { sandbox, events } = makeSandbox({ standalone: true, webShare: false });
    await sandbox.pwaDownload("/f/w?download=1", "d.png");
    assert.strictEqual(events.fetches, 0);
    assert.strictEqual(events.locationSet, false);
    assert.strictEqual(events.anchorClicks.length, 1, "_blank handoff");
    console.log("✓ no Web Share → _blank handoff, no fetch");
  }

  // 5. non-standalone is NOT the helper's path (callers keep plain download)
  {
    const { sandbox } = makeSandbox({ standalone: false });
    assert.strictEqual(sandbox.isStandalonePWA(), false, "browser not standalone");
    console.log("✓ non-standalone: helper not engaged (plain download path)");
  }

  // 6. confirmAction authority (RC-VIS-002): base-engine.js owns the only
  //    site-wide window.confirmAction; app.js defines none; tokens/profile
  //    consume it directly. Behavior: promise resolves per button/Escape.
  {
    const appjs = fs.readFileSync(path.join(REPO, "static", "app.js"), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    assert.ok(!/window\.confirmAction\s*=/.test(appjs),
      "app.js no longer defines confirmAction");
    const engine = fs.readFileSync(path.join(REPO, "static", "pages", "base-engine.js"), "utf8");
    assert.strictEqual((engine.match(/window\.confirmAction\s*=/g) || []).length, 1,
      "base-engine.js defines exactly one canonical confirmAction");
    for (const f of ["keys.js", "profile.js"]) {
      const s = fs.readFileSync(path.join(REPO, "static", "pages", f), "utf8");
      assert.ok(/window\.confirmAction\s*\(/.test(s), f + " consumes the site-wide authority");
    }
    // behavioral: run the REAL engine confirmAction in the sandbox DOM
    const { sandbox: cs } = makeSandbox({ standalone: false });
    const els = {};
    const el = (id) => (els[id] = els[id] || {
      id, textContent: "", innerHTML: "", listeners: {}, focus: () => {},
      classList: { add: (...c) => { els[id]._cls = (els[id]._cls || []).concat(c); },
                   remove: () => {}, contains: () => false },
      setAttribute: () => {}, appendChild: () => {},
      addEventListener: (ev, fn) => { els[id].listeners[ev] = fn; },
    });
    cs.document = {
      getElementById: (id) => el(id),
      createElement: () => el("created"),
      addEventListener: () => {}, removeEventListener: () => {},
      body: { appendChild: () => {} },
    };
    cs.addEventListener = () => {}; cs.removeEventListener = () => {};
    vm.runInContext(engine, cs, { filename: "base-engine.js" });
    // engine installs its own window.t at load — override AFTER load with the
    // fallback-label i18n the confirm authority must consume
    cs.__I18N__ = { en: { confirm: "Confirm", cancel: "Cancel" } };
    cs.__LANG = "en";
    assert.strictEqual(typeof cs.confirmAction, "function", "engine exposes confirmAction");
    const p = cs.confirmAction({ title: "T", message: "M", confirmLabel: "Yes", cancelLabel: "No" });
    assert.strictEqual(els.confirmModalTitle.textContent, "T", "title rendered");
    assert.strictEqual(els.confirmModalText.textContent, "M", "message rendered");
    assert.strictEqual(els.confirmModalOk.textContent, "Yes", "primary label from caller");
    assert.strictEqual(els.confirmModalCancel.textContent, "No", "cancel label from caller");
    els.confirmModalOk.listeners.click();
    assert.strictEqual(await p, true, "primary resolves true");
    const p2 = cs.confirmAction({ title: "T2" });
    // fallback labels via window.t
    assert.strictEqual(els.confirmModalOk.textContent, "Confirm", "fallback confirm label = window.t('confirm')");
    assert.strictEqual(els.confirmModalCancel.textContent, "Cancel", "fallback cancel label = window.t('cancel')");
    els.confirmModalCancel.listeners.click();
    assert.strictEqual(await p2, false, "cancel resolves false");
    console.log("✓ confirmAction authority: engine-only definition, tokens/profile consume, behavior intact");
  }

  // ── the coherence pass: download-all drives BOTH download shapes ──
// In standalone PWA the file rows are <button data-dl-url> (href stripped
// by wirePwaDownloadLinks); an anchor-only selector in the download-all
// path silently no-ops. Both surfaces must select the unified .rs-dl.
{
  const read = (p) => fs.readFileSync(path.join(REPO, p), "utf8");
  const appjs = read("static/app.js");
  const resultjs = read("static/pages/result.js");
  assert.ok(appjs.includes("querySelectorAll(\".rs-dl\")"),
    "app.js download-all clicks every .rs-dl (anchors AND PWA buttons)");
  assert.ok(resultjs.includes("querySelectorAll('.rs-item .rs-dl')"),
    "result.js download-all selects .rs-dl (shape-agnostic)");
  assert.ok(!/'\.rs-item \.rs-dl\[href/ .test(resultjs),
    "result.js download-all is not anchor-only");
}
console.log("PASS: PWA download authority contract (8 groups)");
})().catch((e) => { console.error(e); process.exit(1); });
