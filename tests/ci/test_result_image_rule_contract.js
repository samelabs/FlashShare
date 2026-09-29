#!/usr/bin/env node
/* Result image embeddable rule contract (JS twin of the server rule).
 *
 * Drives the REAL app.js renderResultItems() (the shared items renderer
 * used by both the inline result card and mirroring the /r result page)
 * in a vm sandbox with a stub DOM and asserts the preview rule matches
 * the server inline/detail canonical rule exactly:
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

// Extract the renderResultItems function body from the real source and
// run it with the module-scope identifiers it expects.
const fnStart = SRC.indexOf("function renderResultItems(items, opts) {");
assert.ok(fnStart >= 0, "renderResultItems found in app.js");
// balanced-brace scan
let depth = 0, i = fnStart;
for (; i < SRC.length; i++) {
  if (SRC[i] === "{") depth++;
  else if (SRC[i] === "}") { depth--; if (depth === 0) break; }
}
const FN_SRC = SRC.slice(fnStart, i + 1);
assert.ok(FN_SRC.includes("image/svg+xml"),
  "client rule excludes image/svg+xml (aligned with server)");

function runRenderItems(item, contentType) {
  const noop = () => {};
  const sandbox = {
    console,
    document: {
      getElementById: () => null,
      querySelector: () => null,
      createElement: () => ({ addEventListener: noop }),
      addEventListener: noop,
    },
    t: (k) => k,
    escapeHtml: (s) => String(s),
    fmtSize: () => "1 KB",
    svgIcon: () => "<svg></svg>",
    encodeURIComponent,
    window: { isStandalonePWA: () => false },
    URLSearchParams, Date, Math, JSON,
    setTimeout: (fn) => fn(),
  };
  vm.createContext(sandbox);
  const fn = vm.runInContext(`(${FN_SRC})`, sandbox, { filename: "renderResultItems" });
  return fn([{ ...item, content_type: contentType }], {});
}

// multi-item runner: passes a whole items array straight through
function runRenderItemsMulti(items) {
  const noop = () => {};
  const sandbox = {
    console,
    document: {
      getElementById: () => null,
      querySelector: () => null,
      createElement: () => ({ addEventListener: noop }),
      addEventListener: noop,
    },
    t: (k) => k,
    escapeHtml: (s) => String(s),
    fmtSize: () => "1 KB",
    svgIcon: () => "<svg></svg>",
    encodeURIComponent,
    window: { isStandalonePWA: () => false },
    URLSearchParams, Date, Math, JSON,
    setTimeout: (fn) => fn(),
  };
  vm.createContext(sandbox);
  const fn = vm.runInContext(`(${FN_SRC})`, sandbox, { filename: "renderResultItems" });
  return fn(items, {});
}

// ── png → main view generated ──
{
  const html = runRenderItems(
    { kind: "file", public_id: "p1", filename: "a.png", size: 8,
      download_url: "/f/p1?download=1", page_url: "/f/p1" },
    "image/png");
  assert.ok(html.includes('class="rs-image"'),
    "single image/png renders the full main view");
  assert.ok(html.includes("/f/p1/inline"),
    "image/png main view uses the /inline URL");
  assert.ok(html.includes("?download=1"),
    "file item carries the direct download link");
  console.log("✓ image/png → main view with /inline img");
}

// ── svg → NO preview, no svg-specific second mechanism ──
{
  const html = runRenderItems(
    { kind: "file", public_id: "p2", filename: "a.svg", size: 8,
      download_url: "/f/p2?download=1", page_url: "/f/p2" },
    "image/svg+xml");
  assert.ok(!html.includes('/f/p2/inline'),
    "image/svg+xml generates NO inline preview");
  assert.ok(!html.includes("/f/p2/inline"),
    "image/svg+xml never references /inline");
  assert.ok(html.includes('class="pv doc"'),
    "image/svg+xml falls back to the document row");
  console.log("✓ image/svg+xml → no inline preview, no second mechanism");
}

// ── multiple images → grid cells before the non-image file list ──
{
  const html = runRenderItemsMulti(
    [{ kind: "file", public_id: "p3", filename: "a.png", size: 8,
       content_type: "image/png", download_url: "/f/p3?download=1" },
     { kind: "file", public_id: "p4", filename: "b.jpg", size: 8,
       content_type: "image/jpeg", download_url: "/f/p4?download=1" },
     { kind: "file", public_id: "p5", filename: "c.pdf", size: 8,
       content_type: "application/pdf", download_url: "/f/p5?download=1" }]);
  assert.ok(html.includes('class="rs-grid"'),
    "multiple images render the grid");
  assert.ok((html.match(/rs-cell"/g) || []).length === 2,
    "only image files become grid cells");
  assert.ok(html.includes('data-zoom="/f/p3/inline"'),
    "grid cell opens the full-size view");
  assert.ok(html.indexOf("rs-grid") < html.indexOf('class="item-file'),
    "grid renders before the non-image file list");
  console.log("✓ multiple images → grid before the file list");
}

console.log("PASS: result image embeddable rule aligned with server (3 groups)");
