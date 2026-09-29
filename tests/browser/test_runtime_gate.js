#!/usr/bin/env node
/* Browser Runtime Gate — real Chromium against the real FlashShare app.
 *
 * Stack (mirrors .github/workflows/ci.yml `canonical` job):
 *   PostgreSQL 16 + fake Gokapi HTTP boundary (tests/support/fake_gokapi.py)
 *   + real uvicorn app (app.main:app). No mocked DOM, no mocked fetch.
 *
 * Fail-closed gates (canonical convention):
 *   FLASHSHARE_TEST_PORT (isolated port != 8000) and TEST_DB_URL required.
 *
 * Coverage (single run):
 *   1.  register (real form)                    8.  create real fsk_ API key
 *   2.  login (real form)                       9.  MCP initialize/tools/list
 *   3.  home real render                       10.  MCP tools/call compress
 *   4.  upload file                            11.  MCP structuredContent items
 *   5.  select + execute compress              12.  retryable failure + Retry
 *   6.  success result real render                 (same logical operation,
 *   7.  download action visible/usable             no re-upload, no double
 *   +  page visual checks per page/viewport         charge, recovery)
 *   +  full-page screenshots desktop 1280x900 & mobile 390x844
 *
 * Artifacts: <OUT>/desktop/*.png, <OUT>/mobile/*.png (full-page).
 */
"use strict";
const fs = require("fs");
const path = require("path");
const http = require("http");
let chromium;
({ chromium } = require(process.env.PLAYWRIGHT_CORE_PATH || "playwright-core"));

// ── fail-closed gate ──────────────────────────────────────────────
const PORT = parseInt(process.env.FLASHSHARE_TEST_PORT || "", 10);
if (!PORT || Number.isNaN(PORT)) { console.error("ERROR: FLASHSHARE_TEST_PORT required"); process.exit(2); }
if (PORT === 8000) { console.error("ERROR: FLASHSHARE_TEST_PORT=8000 is the production port"); process.exit(2); }
const TEST_DB = process.env.TEST_DB_URL;
if (!TEST_DB) { console.error("ERROR: TEST_DB_URL required"); process.exit(2); }
const BASE = `http://127.0.0.1:${PORT}`;
const OUT = process.env.GATE_ARTIFACTS || "/tmp/fs-browser-gate-artifacts";
const GOKAPI_ADMIN = `${(process.env.FLASHSHARE_GOKAPI_URL || "http://127.0.0.1:53898").replace(/\/$/, "")}/ci/fail-internal`;
const GOKAPI_KEY = process.env.FLASHSHARE_GOKAPI_KEY || "";

// PNG: 800x600 red-ish photo-like image (~3.7KB) — real decodable PNG.
const IMG = Buffer.from(
  "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", "base64");

const FAIL = [];
function check(name, ok, detail) {
  console.log((ok ? "✓ " : "✗ ") + name + (ok || !detail ? "" : ` — ${String(detail).slice(0, 400)}`));
  if (!ok) FAIL.push(name);
}

// ── helpers ───────────────────────────────────────────────────────
function post(path, body, headers) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request(`${BASE}${path}`, {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, headers || {}),
    }, (res) => {
      let buf = "";
      res.on("data", (c) => (buf += c));
      res.on("end", () => {
        let parsed = null;
        try { parsed = JSON.parse(buf); } catch (e) { /* keep null */ }
        resolve({ status: res.statusCode, json: parsed, text: buf, headers: res.headers });
      });
    });
    req.on("error", reject);
    req.end(data);
  });
}
function setGokapiFail(mode) {
  return fetch(GOKAPI_ADMIN, {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: GOKAPI_KEY },
    body: JSON.stringify({ mode }),
  }).then((r) => r.text());
}

const VIEWPORTS = {
  desktop: { width: 1280, height: 900 },
  mobile: { width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
};

// visual contract per page: overflow/overlap/CTA checks are generic; the
// per-page required selectors anchor "this page actually rendered".
const PAGES = [
  { path: "/", name: "home", anonOk: true, need: ["#toolInput", "#homeBento .tc"] },
  { path: "/tools", name: "tools", anonOk: true, need: [] },
  { path: "/tool/compress", name: "tool-compress", anonOk: true, need: [".ti-file-input", ".composer"] },
  { path: "/tool/compress/mcp", name: "tool-compress-mcp", anonOk: true, need: [".code-block", "#copyMcp", "#mcpAdd"], copy: ["#copyMcp", "#copyMcpAdd"] },
  { path: "/agents", name: "agents", anonOk: true, need: ["#mcpUrl", "#cfgClaudeCode", "#cfgCurl"], copy: ["#copyMcp", '[data-copy-target="cfgClaudeCode"]'] },
  { path: "/pricing", name: "pricing", anonOk: true, need: [".pr-plans .pr-card", "#refund"] },
  { path: "/auth/login", name: "auth-login", anonOk: true, need: ["input[name=login]"] },
  { path: "/auth/register", name: "auth-register", anonOk: true, need: ["#regUsername"] },
  { path: "/account/credits", name: "account-credits", need: [] },
  { path: "/account/tools", name: "account-tools", need: ["#openPublish"] },
  { path: "/account/tools/publish", name: "account-publish", need: ["#pubForm"] },
  { path: "/account/keys", name: "account-keys", need: ["#createKey", "#newKeyName"] },
];

function attachErrorCollectors(context) {
  return context.addInitScript(() => {
    window.__pageErrors = [];
      document.addEventListener("submit", () => {
        window.__submits = (window.__submits || 0) + 1;
        const fi = document.getElementById("file");
        (window.__submitSnapshots = window.__submitSnapshots || []).push({
          files: fi ? fi.files.length : -1, user: !!window.__USER__,
          href: location.pathname, tool: document.body.dataset.tool || "",
        });
      }, true);
    window.addEventListener("error", (e) => {
      window.__pageErrors.push(String((e.error && e.error.stack) || e.message));
    });
    window.addEventListener("unhandledrejection", (e) => {
      window.__pageErrors.push("unhandledrejection: " + String(e.reason));
    });
  });
}

async function visualAudit(ctx, page, name, screenshot = true) {
  const audit = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const offenders = [];
    document.querySelectorAll("body *").forEach((el) => {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || cs.position === "fixed") return;
      if (r.width === 0 && r.height === 0) return;
      if (r.right > vw + 1 || r.left < -1) {
        offenders.push({ tag: el.tagName, cls: String(el.className).slice(0, 60), left: Math.round(r.left), right: Math.round(r.right), vw });
      }
    });
    return {
      vw,
      scrollW: document.documentElement.scrollWidth,
      offenders: offenders.slice(0, 8),
      bodyText: (document.body.innerText || "").slice(0, 4000),
    };
  });
  check(`${ctx} ${name}: no horizontal overflow`, audit.scrollW <= audit.vw + 1, JSON.stringify(audit.offenders));
  if (screenshot) {
    const dir = path.join(OUT, ctx);
    await page.screenshot({ path: path.join(dir, `${name}.png`), fullPage: true });
  }
  return audit;
}

// Navigate and wait for an EXPLICIT readiness signal (element visible).
// networkidle is retired here: it demands 500ms of zero connections and
// the pages legitimately trail async fetches (page data + SW register +
// manifest/icons), which on a slow CI runner can miss every idle window
// for 30s straight.
async function gotoReady(page, path, readySel, timeout = 20000) {
  const resp = await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded", timeout });
  if (readySel) await page.waitForSelector(readySel, { timeout });
  return resp;
}

async function checkPageContract(ctx, page, spec, sessionCookie) {
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));
  const resp = await gotoReady(page, spec.path, spec.need[0] || "body");
  check(`${ctx} ${spec.name}: HTTP ${spec.anonOk || sessionCookie ? "200" : "auth"}`, resp && (resp.status() === 200 || resp.status() === 302), resp && resp.status());
  for (const sel of spec.need) {
    check(`${ctx} ${spec.name}: has ${sel}`, await page.$(sel) !== null);
  }
  // copy buttons are wired (page JS handler gives the copied state)
  for (const sel of spec.copy || []) {
    const btn = await page.$(sel);
    if (btn) await btn.click();
    const copied = btn ? await page.$eval(sel, (b) => b.classList.contains("copied")) : false;
    check(`${ctx} ${spec.name}: ${sel} copy handler`, copied);
  }
  // pageerror must be zero (console.error filtered for expected network noise)
  const errs = consoleErrors.filter((e) => !/net::|Failed to load resource|ERR_/i.test(e));
  check(`${ctx} ${spec.name}: zero pageerror`, errs.length === 0, errs.join(" | "));
  await visualAudit(ctx, page, spec.name);
}

(async () => {
  fs.mkdirSync(path.join(OUT, "desktop"), { recursive: true });
  fs.mkdirSync(path.join(OUT, "mobile"), { recursive: true });

  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });

  const username = "gate" + Math.random().toString(36).slice(2, 8);
  const password = "Test12345";
  let apiKey = "";
  let retryRef = "";
  let createdToolId = "";

  for (const [ctxName, vp] of Object.entries(VIEWPORTS)) {
    // ── 1. register (real form) + 2. login (real form) — desktop does the real flows ──
    const context = await browser.newContext({ viewport: vp });
    const page = await context.newPage();
    attachErrorCollectors(context);
    const netTrace = [];
    {
      const cdp = await context.newCDPSession(page);
      await cdp.send("Network.enable");
      cdp.on("Network.requestWillBeSent", (e) => {
        if (e.request.url.includes("/api/")) netTrace.push({ ev: "sent", url: e.request.url.replace(/^https?:\/\/[^/]+/, ""), id: e.requestId, t: Date.now() });
      });
      cdp.on("Network.requestFailed", (e) => {
        netTrace.push({ ev: "FAILED", url: (netTrace.find(x => x.id === e.requestId) || {}).url || "?", err: e.errorText, id: e.requestId, t: Date.now() });
      });
      cdp.on("Network.loadingFailed", (e) => {
        netTrace.push({ ev: "LOAD-FAILED", url: (netTrace.find(x => x.id === e.requestId) || {}).url || "?", err: e.errorText, id: e.requestId, t: Date.now() });
      });
    }

    if (ctxName === "desktop") {
      // REGISTER (console stream is captured for the whole context flow)
      const flowConsole = [];
      page.on("console", (m) => { if (m.type() === "error") flowConsole.push(m.text()); });
      await gotoReady(page, "/auth/register", "#regUsername");
      await page.fill("#regUsername", username);
      await page.fill("#regEmail", `${username}@t.co`);
      await page.fill("#regPassword", password);
      await page.fill("#regPasswordConfirm", password);
      await Promise.all([page.waitForNavigation({ timeout: 20000, waitUntil: "load" }), page.click("button[type=submit]")]);
      check("web register: redirects to /", new URL(page.url()).pathname === "/", page.url());
      check("web register: logged-in header", (await page.$("#userMenuBtn")) !== null);
      check("web register: zero console.error", flowConsole.filter((e) => !/net::|Failed to load resource|ERR_/i.test(e)).length === 0, flowConsole.join(" | "));
      await visualAudit("desktop", page, "after-register-home");

      // LOGOUT + LOGIN (logout lives in the profile dropdown — open it first)
      await gotoReady(page, "/account/profile", "#userMenuBtn");
      const menuBtn = await page.$("#userMenuBtn");
      const logout = await page.$(".logout-link");
      if (menuBtn && logout) {
        await menuBtn.click();
        await page.waitForSelector("#userDropdown:not(.hidden)", { timeout: 5000 });
        await Promise.all([page.waitForNavigation({ timeout: 20000, waitUntil: "load" }), logout.click()]);
        check("web logout: back to anon", (await page.$('header a[href="/auth/login"]')) !== null);
      }
      await gotoReady(page, "/auth/login", "input[name=login]");
      await page.fill("input[name=login]", username);
      await page.fill("input[name=password]", password);
      await Promise.all([page.waitForNavigation({ timeout: 20000, waitUntil: "load" }), page.click("button[type=submit]")]);
      check("web login: redirects to /", new URL(page.url()).pathname === "/", page.url());
      check("web login: session cookie present", (await context.cookies()).some((c) => c.name === "fs_session" || c.name.includes("session")));

      // 3. home real render
      const homeAudit = await visualAudit("desktop", page, "home-logged-in", true);
      check("home: tool grid rendered", (await page.$$("#homeBento .tc")).length >= 1, homeAudit.bodyText.slice(0, 120));

      // 4-7. upload + execute compress via real UI (home surface)
      await page.click(".ti-picker");
      await page.click('.tsel-item[data-tool="compress"]');
      // params render inline in the shared input component — no overlay
      check("home: compress selected", await page.evaluate(() => document.body.dataset.tool === "compress"));
      check("home: input component rendered (run + file picker)",
        (await page.$(".ti-run")) !== null && (await page.$(".ti-file-input")) !== null);

      // track network legs for retry assertions
      const legs = { uploads: [], executes: [] };
      page.on("request", (r) => {
        if (r.url().includes("/api/uploads") && r.method() === "POST") legs.uploads.push(r);
        if (r.url().includes("/api/tools/execute") && r.method() === "POST") legs.executes.push(r);
      });

      await setInputFiles(page, ".ti-file-input", [{ name: "gate-photo.png", mimeType: "image/png", buffer: IMG }]);
      await page.click(".ti-run");
      // poll for result card (success or error)
      try {
        await page.waitForSelector(".result-card", { timeout: 60000 });
      } catch (e) {
        const dbg = await page.evaluate(() => ({
          submits: window.__submits || 0,
          snapshot: (window.__submitSnapshots || [])[0] || null,
          fileCount: (document.querySelector(".ti-file-input") || { files: [] }).files.length,
          busy: !!(window.toolOperationBusy && window.toolOperationBusy()),
          btnDisabled: (document.querySelector(".ti-run") || {}).disabled,
          datasetTool: document.body.dataset.tool,
          resultHTML: ((document.getElementById("result") || {}).innerHTML || "").slice(0, 200),
          net: netTrace.slice(-12),
        })).catch(() => null);
        check("first-run: DEBUG dump", false, JSON.stringify(dbg));
        throw e;
      }
      // wait for terminal state: either preview/actions (success) or error
      let okResult = false;
      try {
        await page.waitForSelector(".result-card .rs-dl, .result-card .rs-image img", { timeout: 45000 });
        okResult = true;
      } catch (e) { okResult = false; }
      check("web execute compress: success result rendered", okResult);
      if (okResult) {
        const dl = await page.$(".result-card .rs-dl");
        check("web execute compress: download action visible", dl !== null);
        const dlInfo = dl ? await dl.evaluate((el) => ({ href: el.getAttribute("href"), box: el.getBoundingClientRect() })) : null;
        check("web execute compress: download href is a share link", !!dlInfo && /\/f\//.test(dlInfo.href || ""), dlInfo && dlInfo.href);
        await visualAudit("desktop", page, "tool-success-result", true);
      }

      // ── 12. retryable failure + retry (real UI, same surface) ──
      // Arm fake-gokapi internal-read failure: the execute source read fails
      // typed (tool_failed) AFTER upload — retryable per contract, same ref.
      await setGokapiFail("500");
      await setInputFiles(page, ".ti-file-input", [{ name: "gate-retry.png", mimeType: "image/png", buffer: IMG }]);
      await page.click(".ti-run");
      try {
        await page.waitForSelector("#toolRetry", { timeout: 60000 });
      } catch (e) {
        const dbg = await page.evaluate(() => ({
          canRetry: !!(window.canRetryToolOperation && window.canRetryToolOperation()),
          busy: !!(window.toolOperationBusy && window.toolOperationBusy()),
          resultHTML: (document.getElementById("result") || {}).innerHTML || "",
          errs: (window.__pageErrors || []).join(" || ").slice(0, 500),
          net: netTrace.slice(-12),
        })).catch(() => null);
        check("retry: DEBUG state dump", false, JSON.stringify(dbg));
        throw e;
      }
      const errText = await page.evaluate(() => {
        const el = document.querySelector(".result-card .error-note");
        return el ? el.textContent.trim() : "";
      });
      check("retry: error state shown", errText.length > 0, errText);
      check("retry: no raw machine code leaked", !/tool_failed|err_metering|gokapi|InternalDownload/i.test(errText), errText);
      check("retry: Retry button visible", (await page.$("#toolRetry")) !== null);
      const retryEnabled = await page.evaluate(() => {
        const b = document.getElementById("toolRetry");
        return b && !b.disabled && b.getBoundingClientRect().height > 0;
      });
      check("retry: Retry button clickable", retryEnabled === true);
      // capture the client_ref of the failed operation: last execute leg body
      const failedExec = legs.executes[legs.executes.length - 1];
      const failedRef = failedExec ? await (async () => {
        try { return JSON.parse(failedExec.postData()).client_ref; } catch (e) { return null; }
      })() : null;
      check("retry: failed op carried client_ref", typeof failedRef === "string" && failedRef.length > 0, failedRef);
      await visualAudit("desktop", page, "tool-error-retry", true);

      // Disarm failure, click Retry — must reuse SAME ref, no re-upload
      await setGokapiFail(null);
      const uploadsBefore = legs.uploads.length;
      const execBefore = legs.executes.length;
      await page.click("#toolRetry");
      let recovered = false;
      try {
        await page.waitForSelector(".result-card .rs-dl, .result-card .rs-image img", { timeout: 60000 });
        recovered = true;
      } catch (e) { recovered = false; }
      check("retry: success state recovered", recovered === true);
      const retryRefBody = legs.executes[execBefore] ? await (async () => {
        try { return JSON.parse(legs.executes[execBefore].postData()).client_ref; } catch (e) { return null; }
      })() : null;
      check("retry: same logical operation (client_ref reused)", retryRefBody === failedRef, `${failedRef} vs ${retryRefBody}`);
      check("retry: no re-upload", legs.uploads.length === uploadsBefore, `${uploadsBefore} -> ${legs.uploads.length}`);
      await visualAudit("desktop", page, "tool-retry-success", true);

      // 8. create a real fsk_ API key via UI (viewable-anytime flow:
      //    creation reveals the first row's full key inline)
      await gotoReady(page, "/account/keys", "#newKeyName");
      await page.fill("#newKeyName", "browser-gate");
      await page.click("#createKey");
      await page.waitForSelector(".key-item .key-value", { state: "visible", timeout: 15000 });
      apiKey = await page.evaluate(() => document.querySelector(".key-item .key-value").textContent.trim());
      check("keys: real fsk_ API key created + auto-revealed", /^fsk_/.test(apiKey), apiKey.slice(0, 10));
      check("keys: key appears in list", (await page.$$(".key-item")).length >= 1);
      await visualAudit("desktop", page, "account-keys-created", true);

      // ── 13. publish edit refill escapes stored declaration data ──
      // A malicious enum option value stored in a declaration must
      // refill as TEXT on the author's edit page, never execute.
      const xssTool = "gxp" + Math.random().toString(36).slice(2, 7);
      createdToolId = xssTool;
      const xssPayload = '"><img src=x onerror="window.__xssAudit=1">';
      const created = await page.evaluate(async ({ tid, payload }) => {
        const r = await fetch("/api/usertools", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool_id: tid, label: "Gate XSS", url: "https://93.184.216.34/x",
            timeout_s: 30, files_min: 0, files_max: 1,
            format_families: "image", accept: [".png"],
            outputs: ["text"], cost: 0,
            params: [{ key: "style", type: "enum", required: false,
                       options: [{ value: payload, label: payload }] }],
          }),
        });
        return { ok: r.ok, status: r.status, body: (await r.text()).slice(0, 160) };
      }, { tid: xssTool, payload: xssPayload });
      check("publish-xss: tool with malicious option value created",
        created.ok, created.status + " " + created.body);
      await gotoReady(page, `/account/tools/publish?id=${xssTool}`, "#pubForm");
      const xssState = await page.evaluate(() => ({
        fired: window.__xssAudit === 1,
        optVal: (document.querySelector(".pr-opt-val") || {}).value,
        optText: (document.querySelector(".pr-default option") || {}).textContent,
      }));
      check("publish-xss: malicious option value did NOT execute", xssState.fired === false);
      check("publish-xss: option value refilled as editable text",
        xssState.optVal === xssPayload, xssState.optVal);
      check("publish-xss: default <option> text escaped",
        xssState.optText === xssPayload, xssState.optText);
      await visualAudit("desktop", page, "publish-edit-xss-refill", true);
    }

    // ── per-viewport page contract sweep ──
    const sweepCtx = await browser.newContext({ viewport: vp });
    if (apiKey || ctxName !== "desktop") {
      // mobile: log in via API to sweep authenticated pages with same user
      if (ctxName !== "desktop") {
        const login = await post("/auth/login", null, {}); // form post not JSON; do cookie-less login below
      }
    }
    const spage = await sweepCtx.newPage();
    // authenticated sweep: reuse the desktop session cookie for account pages
    if (ctxName === "desktop") {
      const cookies = await (await browser.contexts())[0].cookies();
      await sweepCtx.addCookies(cookies);
    } else {
      // mobile: programmatic form login to get a session cookie
      const lp = await sweepCtx.newPage();
      await gotoReady(lp, "/auth/login", "input[name=login]");
      await lp.fill("input[name=login]", username);
      await lp.fill("input[name=password]", password);
      await Promise.all([lp.waitForNavigation({ timeout: 15000 }), lp.click("button[type=submit]")]);
      await lp.close();
    }
    for (const spec of PAGES) {
      // fresh page per spec so console/pageerror collectors are per-page
      const p = await sweepCtx.newPage();
      await checkPageContract(ctxName, p, spec, true);
      await p.close();
    }
    await sweepCtx.close();
    await context.close();
  }

  // ── 9-11. MCP: initialize → tools/list → tools/call (real API key) ──
  check("mcp: API key available", /^fsk_/.test(apiKey));
  const init = await post("/mcp", { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } }, { Authorization: `Bearer ${apiKey}` });
  check("mcp initialize: ok", init.json && init.json.result && init.json.result.serverInfo && init.json.result.serverInfo.name === "518plus", init.text);
  const tl = await post("/mcp", { jsonrpc: "2.0", id: 2, method: "tools/list" }, { Authorization: `Bearer ${apiKey}` });
  const tools = ((tl.json || {}).result || {}).tools || [];
  check("mcp tools/list: ok + compress present", tools.some((t) => t.name === "compress"), `count=${tools.length}`);
  const mcall = await post("/mcp", {
    jsonrpc: "2.0", id: 3, method: "tools/call", params: {
      name: "compress",
      arguments: {
        files: [{ filename: "gate-mcp.png", b64: IMG.toString("base64") }],
        params: {}, delivery: { expiry_days: 1, allowed_downloads: 50 },
        client_ref: "mcp-gate-" + Math.random().toString(36).slice(2, 10),
      },
    },
  }, { Authorization: `Bearer ${apiKey}` });
  let mcpResult = {};
  try { mcpResult = JSON.parse((((mcall.json || {}).result || {}).content || [{}])[0].text || "{}"); } catch (e) { /* */ }
  check("mcp tools/call: ok", ((mcall.json || {}).result || {}).isError !== true, mcall.text && mcall.text.slice(0, 200));
  // IO-1 items contract: file identity lives on structuredContent.items
  const sc = ((mcall.json || {}).result || {}).structuredContent || {};
  const item0 = (sc.items || [])[0] || {};
  check("mcp tools/call: real result items (file, /f/ page_url, /r/ result_url)",
    item0.kind === "file" && /\/f\//.test(item0.page_url || "") && /\/r\//.test(sc.result_url || ""),
    JSON.stringify(sc).slice(0, 200));
  check("mcp tools/call: share link downloadUrl", /\/f\//.test(item0.download_url || "") && /\?download=1$/.test(item0.download_url || ""));

  // ── UI-polish permanent regression ─────────────────────────────
  // (a) horizontal-overflow sweep at 360px with long-word (de) and RTL (ar)
  //     locales on the core surfaces;
  // (b) mobile language switch entry exists in the nav sheet and switches
  //     in place;
  // (c) primary touch targets reach 44x44 on mobile (rect or ::after hit
  //     extension);
  // (d) the empty-state upload zone is visible and clickable on a file tool;
  // (e) CLS <= 0.1 on home / tool page / publish edit.
  {
    const OVERFLOW_PAGES = ["/", "/tools", "/tool/compress", "/pricing",
      "/auth/login", "/account/credits"];
    for (const lang of ["de", "ar"]) {
      const ctx = await browser.newContext({ viewport: { width: 360, height: 640 } });
      await ctx.addCookies([{ name: "flashshare_lang", value: lang, url: BASE }]);
      const page = await ctx.newPage();
      for (const p of OVERFLOW_PAGES) {
        if (p.startsWith("/account")) {
          const cookies = await (await browser.contexts())[0].cookies();
          await ctx.addCookies(cookies);
        }
        await gotoReady(page, p, "body");
        await page.waitForTimeout(400);
        const audit = await page.evaluate(() => ({
          vw: document.documentElement.clientWidth,
          sw: document.documentElement.scrollWidth,
        }));
        check(`polish 360/${lang} ${p}: no horizontal overflow`, audit.sw <= audit.vw + 1, JSON.stringify(audit));
      }
      await ctx.close();
    }

    // (b) mobile language entry
    {
      const ctx = await browser.newContext({ viewport: { width: 390, height: 844, hasTouch: true } });
      const page = await ctx.newPage();
      await gotoReady(page, "/", "body");
      await page.click("#mobileNavBtn");
      await page.waitForTimeout(300);
      const entry = await page.evaluate(() => {
        const z = document.querySelector(".mobile-nav-lang");
        const b = document.querySelector("#langButtonM");
        const r = b ? b.getBoundingClientRect() : null;
        return {
          present: !!z && z.offsetParent !== null,
          btn: !!b && r.width > 0 && r.height >= 40,
        };
      });
      check("polish mobile: language entry in nav sheet", entry.present && entry.btn, JSON.stringify(entry));
      await page.click("#langButtonM");
      await page.waitForTimeout(300);
      const menuOpen = await page.evaluate(() => {
        const m = document.querySelector("#langMenuM");
        return m && !m.classList.contains("hidden") && m.querySelectorAll(".lang-option").length >= 10;
      });
      check("polish mobile: language menu opens with options", menuOpen);
      await page.click("#langMenuM .lang-option[data-lang=en]");
      await page.waitForTimeout(600);
      const switched = await page.evaluate(() => window.__LANG);
      check("polish mobile: language switch applies in place", switched === "en", switched);
      await ctx.close();
    }

    // (c) touch targets + (d) upload zone + (e) CLS
    {
      const ctx = await browser.newContext({ viewport: { width: 390, height: 844, hasTouch: true } });
      await ctx.addInitScript(() => {
        window.__cls = 0;
        try {
          new PerformanceObserver((l) => {
            for (const e of l.getEntries()) if (!e.hadRecentInput) window.__cls += e.value;
          }).observe({ type: "layout-shift", buffered: true });
        } catch (e) { /* older engine */ }
      });
      const page = await ctx.newPage();
      await gotoReady(page, "/tool/compress", ".composer");
      await page.waitForTimeout(900);
      const touch = await page.evaluate(() => {
        const sels = [".ti-drop", ".ti-add", "button.send", "#mobileNavBtn", ".ft a"];
        const bad = [];
        for (const sel of sels) {
          for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 3)) {
            const r = el.getBoundingClientRect();
            if (r.width === 0) continue;
            let hit = r.width >= 44 && r.height >= 44;
            if (!hit) {
              // ::after hit-extension counts (padding expansion pattern)
              const pa = getComputedStyle(el, "::after");
              hit = pa.content !== "none" && pa.position === "absolute";
            }
            if (!hit) bad.push(`${sel} ${Math.round(r.width)}x${Math.round(r.height)}`);
          }
        }
        return bad;
      });
      check("polish mobile: primary touch targets >= 44 (rect or ::after)", touch.length === 0, touch.join(", "));

      const zone = await page.evaluate(() => {
        const z = document.querySelector(".ti-drop");
        if (!z) return { present: false };
        const r = z.getBoundingClientRect();
        const cx = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return { present: true, w: Math.round(r.width), clickable: cx === z || z.contains(cx) };
      });
      check("polish mobile: upload zone visible + clickable", zone.present && zone.w >= 120 && zone.clickable, JSON.stringify(zone));

      // ── F20/F21: zone states formats + count + per-file cap + daily quota;
      //    an oversize file is rejected client-side with the cap value ──
      const zoneCopy = await page.evaluate(() => ({
        sub: (document.querySelector(".ti-drop small") || {}).innerText || "",
        limits: window.__LIMITS__ || null,
      }));
      check("polish limits: zone copy shows formats/count/size/daily",
        /MB/.test(zoneCopy.sub) && zoneCopy.sub.split("·").length >= 4,
        zoneCopy.sub);
      const mb = zoneCopy.limits && zoneCopy.limits.maxFileMb;
      check("polish limits: __LIMITS__ projection present", !!mb && !!zoneCopy.limits.dailyFiles, JSON.stringify(zoneCopy.limits));
      const oversize = await page.evaluate(async (mbv) => {
        const buf = new Uint8Array((mbv + 2) * 1024 * 1024);
        const f = new File([buf], "oversize.png", { type: "image/png" });
        const dt = new DataTransfer();
        dt.items.add(f);
        const input = document.querySelector(".ti-file-input");
        input.files = dt.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await new Promise((r) => setTimeout(r, 400));
        const err = document.querySelector(".ti-error");
        return { hidden: err ? err.hidden : null, text: err ? err.innerText : "" };
      }, mb || 50);
      check("polish limits: oversize file blocked client-side with cap value",
        oversize.hidden === false && new RegExp(String(mb || 50)).test(oversize.text),
        JSON.stringify(oversize));

      // authenticated context for the publish probes (form login, same
      // pattern as the sweep)
      const authCtx = await browser.newContext({ viewport: { width: 390, height: 844 } });
      {
        const lp = await authCtx.newPage();
        await gotoReady(lp, "/auth/login", "input[name=login]");
        await lp.fill("input[name=login]", username);
        await lp.fill("input[name=password]", password);
        await Promise.all([lp.waitForNavigation({ timeout: 15000 }), lp.click("button[type=submit]")]);
        await lp.close();
      }

      // __LIMITS__ identical fields across surfaces
      const limFields = async (p) => {
        const pg = await (p.startsWith("/account") ? authCtx : ctx).newPage();
        await gotoReady(pg, p, "body");
        await pg.waitForTimeout(300);
        const v = await pg.evaluate(() => window.__LIMITS__ || null);
        await pg.close();
        return v ? Object.keys(v).sort().join(",") + "|" + JSON.stringify(v.maxFileMb) + "," + JSON.stringify(v.dailyFiles) : "none";
      };
      const fHome = await limFields("/");
      const fToolV = await limFields("/tool/compress");
      const fPub = await limFields("/account/tools/publish");
      check("polish limits: __LIMITS__ consistent across home/tool/publish",
        fHome === fToolV && fHome === fPub && fHome !== "none", `${fHome} | ${fToolV} | ${fPub}`);

      // (e) CLS on tool page (same page), then home, then publish edit
      const clsTool = await page.evaluate(() => +(window.__cls || 0).toFixed(3));
      check("polish CLS tool page <= 0.1", clsTool <= 0.1, String(clsTool));
      const home = await ctx.newPage();
      await gotoReady(home, "/", "body");
      await home.waitForTimeout(1200);
      const clsHome = await home.evaluate(() => +(window.__cls || 0).toFixed(3));
      check("polish CLS home <= 0.1", clsHome <= 0.1, String(clsHome));
      const pub = await authCtx.newPage();
      await gotoReady(pub, `/account/tools/publish?id=${createdToolId}`, "#pubForm");
      await pub.waitForTimeout(1200);
      const clsPub = await pub.evaluate(() => +(window.__cls || 0).toFixed(3));
      check("polish CLS publish edit <= 0.1", clsPub <= 0.1, String(clsPub));
      await authCtx.close();
      await ctx.close();
    }
  }

  // ── SEO/PWA batch: mobile input floor + focus stability + offline ──
  // (a) at 390x844 and 360x640 every VISIBLE text-entry control renders
  //     >= 16px (no iOS/Android focus auto-zoom) and focusing them never
  //     blows out the viewport horizontally;
  // (b) the site-wide login overlay carries the same floor;
  // (c) the service worker registers on home and an offline navigation
  //     serves the offline page (HTML is never cached, /offline is).
  {
    const INPUT_PAGES = ["/", "/tool/compress", "/pricing",
      "/account/tools/publish", "/account/keys", "/account/profile"];
    const auditInputs = (pg) => pg.evaluate(() => {
      const skip = new Set(["hidden", "checkbox", "radio", "file", "submit", "button", "image", "reset"]);
      const bad = [];
      document.querySelectorAll("input, select, textarea").forEach((el) => {
        if (skip.has(el.type || "")) return;
        const cs = getComputedStyle(el);
        if (cs.display === "none" || cs.visibility === "hidden") return;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return;
        const fs = parseFloat(cs.fontSize);
        if (fs < 16) bad.push(`${el.tagName.toLowerCase()}[${(el.getAttribute("name") || el.id || el.className || "").toString().slice(0, 30)}] ${fs}px`);
      });
      return bad;
    });
    for (const w of [390, 360]) {
      const ctx = await browser.newContext({ viewport: { width: w, height: w === 390 ? 844 : 640, isMobile: true, hasTouch: true } });
      {
        const lp = await ctx.newPage();
        await gotoReady(lp, "/auth/login", "input[name=login]");
        await lp.fill("input[name=login]", username);
        await lp.fill("input[name=password]", password);
        await Promise.all([lp.waitForNavigation({ timeout: 15000 }), lp.click("button[type=submit]")]);
        await lp.close();
      }
      for (const p of INPUT_PAGES) {
        const page = await ctx.newPage();
        await gotoReady(page, p, "body");
        await page.waitForTimeout(300);
        const bad = await auditInputs(page);
        check(`input-floor ${w} ${p}: visible inputs >= 16px`, bad.length === 0, bad.join(", "));
        const focus = await page.evaluate(() => {
          const skip = new Set(["hidden", "checkbox", "radio", "file", "submit", "button", "image", "reset"]);
          [...document.querySelectorAll("input, select, textarea")]
            .filter((el) => !skip.has(el.type || "")).forEach((el) => el.focus());
          return { sw: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth };
        });
        check(`input-floor ${w} ${p}: focus keeps scrollWidth <= innerWidth`, focus.sw <= focus.vw + 1, JSON.stringify(focus));
        await page.close();
      }
      await ctx.close();
    }

    // (b) login overlay on a phone viewport
    {
      const ctx = await browser.newContext({ viewport: { width: 390, height: 844, isMobile: true, hasTouch: true } });
      const page = await ctx.newPage();
      await gotoReady(page, "/", "body");
      await page.click("#mobileNavBtn");
      await page.waitForTimeout(300);
      await page.click(".mobile-nav-login");
      await page.waitForSelector(".auth-modal input", { timeout: 5000 });
      const bad = await auditInputs(page);
      check("input-floor 390 login overlay: inputs >= 16px", bad.length === 0, bad.join(", "));
      await ctx.close();
    }

    // (c) PWA: SW registers + offline navigation. The offline half uses a
    // REAL dead origin: page-level offline emulation does not cover the
    // service worker's own network-first fetch (verified: playwright 1.55
    // leaves SW fetches online), so the gate serves the real app on a
    // throwaway port, lets the real sw.js register there, stops that
    // server and navigates — the SW's fetch must genuinely fail and fall
    // back to the pre-cached /offline page.
    {
      const { spawn } = require("child_process");
      let port = -1;
      for (const cand of [8410, 8411, 8412, 8413]) {
        const busy = await new Promise((resolve) => {
          const net = require("net");
          const s = net.connect({ host: "127.0.0.1", port: cand }, () => { s.destroy(); resolve(true); });
          s.on("error", () => resolve(false));
        });
        if (!busy) { port = cand; break; }
      }
      check("pwa: throwaway port available for the offline probe", port > 0, "8410-8413 all busy");
      if (port > 0) {
        const OFFLINE_BASE = `http://127.0.0.1:${port}`;
        const py = process.env.PYTHON || "python3";
        const child = spawn(py, ["-m", "uvicorn", "app.main:app",
          "--host", "127.0.0.1", "--port", String(port), "--workers", "1"],
          { cwd: process.cwd(), env: process.env, stdio: "ignore" });
        let up = false;
        for (let i = 0; i < 60 && !up; i++) {
          up = await new Promise((resolve) => {
            const req = require("http").get(`${OFFLINE_BASE}/healthz`, (r) => { r.resume(); resolve(r.statusCode === 200); });
            req.on("error", () => resolve(false));
            req.setTimeout(1500, () => { req.destroy(); resolve(false); });
          });
          if (!up) await new Promise((r) => setTimeout(r, 500));
        }
        check("pwa: throwaway app up", up === true);
        if (up) {
          const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
          const page = await ctx.newPage();
          await page.goto(`${OFFLINE_BASE}/`, { waitUntil: "domcontentloaded", timeout: 20000 });
          const sw = await page.evaluate(async (origin) => {
            if (!("serviceWorker" in navigator)) return { ok: false };
            await navigator.serviceWorker.register(origin + "/sw.js", { updateViaCache: "none" }).catch(() => {});
            await navigator.serviceWorker.ready;
            const keys = await caches.keys();
            let offlineCached = false;
            try {
              const c = await caches.open(keys.find((k) => k.startsWith("flashshare-pwa-")) || keys[0]);
              offlineCached = !!(await c.match("/offline"));
            } catch (e) { /* no cache */ }
            return { ok: true, offlineCached };
          }, OFFLINE_BASE);
          check("pwa: service worker registers + activates (real origin)", sw.ok === true);
          check("pwa: offline page pre-cached at install", sw.offlineCached === true);
          child.kill("SIGTERM");
          await new Promise((resolve) => child.once("exit", resolve));
          let offlineOk = false;
          try {
            await page.goto(`${OFFLINE_BASE}/tools`, { waitUntil: "domcontentloaded", timeout: 15000 });
            await page.waitForSelector(".status-glyph", { timeout: 8000 });
            offlineOk = true;
          } catch (e) { offlineOk = false; }
          check("pwa: offline navigation returns the offline page", offlineOk === true);
          await ctx.close();
        }
        try { child.kill("SIGKILL"); } catch (e) { /* already gone */ }
      }
    }
  }

  // ── Text results: line breaks, exact copy, safe links, single-link card ──
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    await gotoReady(page, "/", "#toolInput");
    await page.evaluate(() => {
      window.__copied = null;
      Object.defineProperty(navigator, "clipboard", {
        value: { writeText: (s) => { window.__copied = s; return Promise.resolve(); } },
        configurable: true,
      });
    });
    const MIXED = "line one\nline two see https://example.com/a. " +
      "and javascript:alert(1) or http://plain.example/x";
    await page.evaluate((text) => showItemsResult({ result_id: "rtest1",
      items: [{ kind: "text", text }] }), MIXED);
    await page.evaluate(() => document.getElementById("result").classList.remove("hidden"));
    const mixed = await page.evaluate(() => {
      const pre = document.querySelector("#result pre.rs-text");
      const links = [...pre.querySelectorAll("a")].map((a) => [a.getAttribute("href"), a.rel, a.target]);
      return { lines: pre.getBoundingClientRect().height, lh: parseFloat(getComputedStyle(pre).lineHeight) || 20,
        ws: getComputedStyle(pre).whiteSpace, links, bar: !!document.querySelector("#result .linkbar") };
    });
    check("text result keeps line breaks (pre-wrap, >1 line)",
      mixed.ws === "pre-wrap" && mixed.lines > mixed.lh * 1.5, JSON.stringify(mixed));
    check("text result links only https, new tab, noopener",
      mixed.links.length === 1 && mixed.links[0][0] === "https://example.com/a"
      && /noopener/.test(mixed.links[0][1]) && mixed.links[0][2] === "_blank", JSON.stringify(mixed.links));
    check("mixed text keeps the platform result link bar", mixed.bar);
    await page.click("#result .item-text .rs-copy");
    await page.waitForTimeout(100);
    check("text copy equals the original text exactly",
      await page.evaluate((t) => window.__copied === t, MIXED));
    const SINGLE = "https://example.com/page?q=\"><img/src=x/onerror=window.__x=1>";
    await page.evaluate((text) => showItemsResult({ result_id: "rtest2",
      items: [{ kind: "text", text }] }), SINGLE);
    const single = await page.evaluate(() => ({
      card: !!document.querySelector("#result .rs-link-card"),
      host: (document.querySelector("#result .rs-link-card b") || {}).textContent,
      bar: !!document.querySelector("#result .linkbar"),
      page: !!document.querySelector('#result a[href^="/r/"]'),
      img: !!document.querySelector("#result img[src='x']"),
      x: window.__x === 1,
    }));
    check("single https URL renders a link card; platform link demoted to result-page button",
      single.card && single.host === "example.com" && !single.bar && single.page, JSON.stringify(single));
    check("hostile URL text never becomes markup", !single.img && !single.x, JSON.stringify(single));
    await ctx.close();
  }

  await browser.close();

  // artifacts sanity
  const shots = fs.readdirSync(path.join(OUT, "desktop")).concat(fs.readdirSync(path.join(OUT, "mobile")));
  check("artifacts: screenshots non-empty", shots.length >= 24 && shots.every((f) => f.endsWith(".png")), `${shots.length} files`);
  console.log(`\nshots: ${shots.length}`);
  if (FAIL.length) {
    console.error(`\nFAIL count: ${FAIL.length}`);
    FAIL.forEach((f) => console.error(" ✗ " + f));
    process.exit(1);
  }
  console.log("\nALL GREEN");
  process.exit(0);
})().catch((e) => { console.error("GATE CRASH:", e); process.exit(1); });

// playwright-core input files helper (page.setInputFiles exists on ElementHandle)
async function setInputFiles(page, sel, files) {
  const handle = await page.$(sel);
  if (!handle) throw new Error("no file input " + sel);
  await handle.setInputFiles ? handle.setInputFiles(files) : page.setInputFiles(sel, files);
}
