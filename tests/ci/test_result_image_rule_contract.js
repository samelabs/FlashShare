#!/usr/bin/env node
/* Result image embeddable rule contract (JS twin of the server rule).
 *
 * Drives the REAL app.js showShareResult() in a vm sandbox with a stub
 * DOM and asserts the preview rule matches the server inline/detail
 * canonical rule exactly:
 *   image/* → auto /inline preview, EXCEPT image/svg+xml (no preview,
 *   no second SVG-specific preview mechanism).
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const REPO = path.resolve(__dirname, "..", "..");
const SRC = fs.readFileSync(path.join(REPO, "static", "app.js"), "utf8");

// Extract the showShareResult function body from the real source and
// run it with the module-scope identifiers it expects.
const fnStart = SRC.indexOf("function showShareResult(file) {");
assert.ok(fnStart >= 0, "showShareResult found in app.js");
// balanced-brace scan
let depth = 0, i = fnStart;
for (; i < SRC.length; i++) {
  if (SRC[i] === "{") depth++;
  else if (SRC[i] === "}") { depth--; if (depth === 0) break; }
}
const FN_SRC = SRC.slice(fnStart, i + 1);
assert.ok(FN_SRC.includes("image/svg+xml"),
  "client rule excludes image/svg+xml (aligned with server)");

function runShowShareResult(file, contentType) {
  const calls = { resultHTML: [], byId: new Map() };
  const noop = () => {};
  const mkEl = () => ({
    addEventListener: noop, style: {}, classList: { add: noop, remove: noop },
    setAttribute: noop, appendChild: noop, remove: noop,
    innerHTML: "", textContent: "",
    querySelector: () => null, querySelectorAll: () => [],
  });
  const sandbox = {
    console,
    window: {},
    document: {
      getElementById: (id) => calls.byId.get(id) || null,
      querySelector: () => null,
      createElement: mkEl,
      addEventListener: noop,
    },
    lastErrorKey: null,
    t: (k) => k,
    escapeHtml: (s) => String(s),
    fmtSize: () => "1 KB",
    svgIcon: () => "<svg></svg>",
    showResult: (html) => { calls.resultHTML.push(html); },
    showQrModal: noop,
    ensureZipPreview: noop,
    isStandalonePWA: () => false,
    shareFile: async () => true,
    URLSearchParams, Date, Math, JSON,
    setTimeout: (fn) => fn(),
  };
  sandbox.window.__syncCredits = undefined;
  vm.createContext(sandbox);
  const fn = vm.runInContext(`(${FN_SRC})`, sandbox, { filename: "showShareResult" });
  // element stubs for the ids the function binds afterwards
  for (const id of ["copy", "qr", "zip", "dl", "shareimg"]) calls.byId.set(id, mkEl());
  fn({ ...file, contentType });
  return calls;
}

// ── png → preview generated ──
{
  const c = runShowShareResult(
    { publicId: "p1", filename: "a.png", downloadUrl: "https://x/f/p1" },
    "image/png");
  const html = c.resultHTML.join("");
  assert.ok(html.includes('class="result-preview"'),
    "image/png generates a result preview");
  assert.ok(html.includes("/f/p1/inline"),
    "image/png preview uses the /inline URL");
  console.log("✓ image/png → result preview with /inline img");
}

// ── svg → NO preview, no svg-specific second mechanism ──
{
  const c = runShowShareResult(
    { publicId: "p2", filename: "a.svg", downloadUrl: "https://x/f/p2" },
    "image/svg+xml");
  const html = c.resultHTML.join("");
  assert.ok(!html.includes('class="result-preview"'),
    "image/svg+xml generates NO inline preview");
  assert.ok(!html.includes("/f/p2/inline"),
    "image/svg+xml never references /inline");
  console.log("✓ image/svg+xml → no inline preview, no second mechanism");
}

console.log("PASS: result image embeddable rule aligned with server (2 groups)");
