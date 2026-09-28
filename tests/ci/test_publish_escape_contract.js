#!/usr/bin/env node
/* Publish-page escaping contract (audit-fix item 5).
 *
 * The publish editor rebuilds param rows / enum options from the stored
 * declaration via innerHTML. Every piece of declaration data reaching
 * that HTML (key, label, placeholder, default, option value AND option
 * text) must go through the shared five-character escaper
 * (window.escapeHtml, defined by app.js which loads before publish.js).
 * The quote-only `.replace(/"/g, "&quot;")` idiom is forbidden — it left
 * text-content sinks (the enum default <option>) fully unescaped, a
 * stored self-XSS on the author's own edit page.
 *
 * Source-contract assertions here; the behavioral proof (real Chromium,
 * malicious option value refilled without execution) lives in the
 * browser runtime gate.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "static", "pages", "publish.js"), "utf8");
const appJs = fs.readFileSync(
  path.join(__dirname, "..", "..", "static", "app.js"), "utf8");

let failed = 0;
function check(name, ok, detail) {
  console.log((ok ? "✓" : "✗") + " " + name + (ok || !detail ? "" : " — " + detail));
  if (!ok) failed++;
}

check("no quote-only escaping idiom remains",
  !src.includes('.replace(/"/g, "&quot;")'));
check("shared escaper alias is defined",
  /const _esc = s => window\.escapeHtml\(String\(s == null \? "" : s\)\);/.test(src));
check("escaper is the app.js five-character escapeHtml",
  appJs.includes("window.escapeHtml = escapeHtml;")
  && escapeCovers(appJs));
function escapeCovers(code) {
  const fn = code.split("function escapeHtml")[1].split("\n}")[0];
  return /[&<>"']/.test(fn) && fn.includes("&amp;") && fn.includes("&lt;")
    && fn.includes("&gt;") && fn.includes("&quot;") && fn.includes("&#39;");
}

// every user-data interpolation inside the param editor builders uses _esc
const paramHtml = src.split("function paramRowHtml")[1].split("\nfunction bindParamRow")[0];
const interp = [...paramHtml.matchAll(/\$\{([^}]+)\}/g)].map((m) => m[1]);
// _msg(...) i18n dictionary values and pure structural expressions are
// trusted; anything else (declaration data) must be wrapped in _esc(...)
const unescaped = interp.filter((e) => {
  if (e.includes("_esc(") || e.includes("_msg(")) return false;
  if (/^p\.type === /.test(e)) return false;
  if (/^p\.(min|max|default) != null \? _esc\(/.test(e) || / : ""$/.test(e)) return false;
  if (/^(p\.required|id|v|typeOpts|opts)$/.test(e.trim())) return false;
  if (e.includes(".map(")) return false;   // container expr; inner sinks asserted below
  if (/^\(p\.options/.test(e.trim())) return false;  // nested-regex fragment of the same
  return true;
});
check("paramRowHtml/paramBodyHtml: every declaration-data interpolation uses _esc",
  unescaped.length === 0, JSON.stringify(unescaped));

// the historical XSS sink specifically: enum default option value+text
check("enum default <option> value and text both escaped",
  paramHtml.includes('value="${_esc(o.value)}"')
  && paramHtml.includes(">${_esc(o.value)}</option>"));

check("escapeHtml coerces null/undefined to empty string",
  appJs.split("function escapeHtml")[1].split("\n}")[0].includes('String(s || "")'));

process.exit(failed ? 1 : 0);
