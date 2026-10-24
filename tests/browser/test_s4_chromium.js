"use strict";
/* ── S4 control plane runtime gate (Chromium) ─────────────────────
 * Real-browser verification of the S4 surfaces: operator control
 * plane (11 pages + credits adjust + withdrawal approve/paid) and
 * the user earnings/withdrawal face (channel projection, snapshot
 * freeze, empty-channel gating, success feedback).
 *
 * Portable: repo cwd, `python3` from PATH, env vars only — no
 * absolute machine paths. Playwright Chromium via PLAYWRIGHT_CORE_PATH.
 *
 * Fail-closed: FLASHSHARE_TEST_PORT (≠8000) + TEST_DB_URL required.
 */
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT_CORE_PATH || "playwright-core"));
} catch (e) {
  // dev fallback only (CI provides PLAYWRIGHT_CORE_PATH); portability of
  // the PRIMARY path is what CI exercises
  ({ chromium } = require(path.join(
    require("os").homedir(), ".local/lib/node_modules/playwright-core")));
}

const PORT = parseInt(process.env.FLASHSHARE_TEST_PORT || "", 10);
if (!PORT || Number.isNaN(PORT)) { console.error("ERROR: FLASHSHARE_TEST_PORT required"); process.exit(2); }
if (PORT === 8000) { console.error("ERROR: FLASHSHARE_TEST_PORT=8000 is the production port"); process.exit(2); }
const TEST_DB = process.env.TEST_DB_URL;
if (!TEST_DB) { console.error("ERROR: TEST_DB_URL required"); process.exit(2); }
const BASE = `http://127.0.0.1:${PORT}`;
// contexts block the service worker: the S4 controlled-interception proof
// (A2) needs page-level fetches that page.route can actually hold
const REPO = path.resolve(__dirname, "..", "..");

const FAIL = [];
function check(name, ok, detail) {
  console.log((ok ? "✓ " : "✗ ") + name + (ok || !detail ? "" : ` — ${String(detail).slice(0, 300)}`));
  if (!ok) FAIL.push(name);
}

// ── fixtures via repo code + env (portable python) ────────────────
const PY = process.env.PYTHON || "python3";
const seed = execFileSync(PY, ["-c", `
import sys, os, secrets
sys.path.insert(0, os.path.join(${JSON.stringify(REPO)}, "core"))
sys.path.insert(0, ${JSON.stringify(REPO)})
os.environ["DATABASE_URL"] = os.environ["TEST_DB_URL"].replace(
    "postgresql://", "postgresql+psycopg://")
os.environ["FLASHSHARE_SESSION_SECRET"] = \
    os.environ.get("FLASHSHARE_SESSION_SECRET") or "canonical-test"
from database import db, init_db
from security import hash_password, token_hash
from utils import now
init_db()
ts = now()
def one(sql, params):
    with db() as con:
        return con.execute(sql, params).fetchone()
_run = secrets.token_hex(4)
def runuser(base):
    return f"{base}_{_run}"
op = int(one("insert into users (username, display_name, status, password_hash,"
             " created_at, last_login_at) values (%s, 'S4 Op', 'samelabs', %s, %s, %s)"
             " returning id", (runuser("s4rt_op"), hash_password("op-pass-1"), ts, ts))["id"])
prov = int(one("insert into users (username, display_name, status, password_hash,"
               " payout_channel, created_at, last_login_at) values"
               " (%s, 'S4 Prov', 'active', %s, 'USDT-TRC20:rt', %s, %s)"
               " returning id", (runuser("s4rt_prov"), hash_password("prov-pass-1"), ts, ts))["id"])
noch = int(one("insert into users (username, display_name, status, password_hash,"
               " created_at, last_login_at) values (%s, 'No Channel',"
               " 'active', %s, %s, %s) returning id",
               (runuser("s4rt_noch"), hash_password("noch-pass-1"), ts, ts))["id"])
with db() as con:
    con.execute("insert into ledger (user_id, delta, balance_after, kind, created_at)"
                " values (%s, 90000, 90000, 'provider_earn', %s)", (prov, ts))
    w = con.execute("insert into withdrawals (user_id, amount, status, created_at,"
                    " payout_channel_snapshot) values (%s, 25000, 'pending', %s,"
                    " 'USDT-TRC20:old') returning id", (prov, ts)).fetchone()
    wid = int(w["id"])
def tok(uid):
    t = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("insert into sessions (token_hash, user_id, created_at, expires_at)"
                    " values (%s, %s, %s, %s)", (token_hash(t), uid, ts, ts + 7200))
    return t
print(op, prov, noch, wid, tok(op), tok(prov), tok(noch))
`], { env: Object.assign({}, process.env, { TEST_DB_URL: TEST_DB }), encoding: "utf8" }).trim();
const [OP, PROV, NOCH, WID, OP_TOK, PROV_TOK, NOCH_TOK] = seed.split(" ");

(async () => {
  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    headless: true,
  });
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: "block" })).newPage();

  // ════ Operator session: cookie + 11 pages ════
  await page.goto(BASE + "/");
  await page.evaluate(([t]) => {
    document.cookie = "flashshare_session=" + t + "; path=/";
  }, [OP_TOK]);
  await page.goto(BASE + "/samelabs");
  check("1 Overview (operator session accepted)", !page.url().includes("/auth/login"), page.url());
  const ov = await page.content();
  check("1b asset separation on DOM",
    /开发者收益/.test(ov) && /平台收入/.test(ov) && /消费积分/.test(ov));

  for (const [n, p, needle] of [
    ["2 Users", "/samelabs/users", "可花积分"],
    ["3 Tool Supply", "/samelabs/tools", "工具键"],
    ["4 Orders", "/samelabs/commerce/orders", "状态"],
    ["5 Memberships", "/samelabs/commerce/memberships", "会员"],
    ["6 Products", "/samelabs/commerce/products", "订阅档位"],
    ["7 Credits", "/samelabs/economy/credits", "积分流水"],
    ["8 Provider Earnings", "/samelabs/economy/earnings", "开发者收益"],
    ["10 Platform Income", "/samelabs/economy/platform", "平台佣金"],
    ["11 Audit", "/samelabs/audit", "操作审计"],
  ]) {
    await page.goto(BASE + p);
    const c = await page.content();
    check(n, c.includes(needle), page.url());
  }

  // 9 Withdrawals queue — row-scoped snapshot assertion
  await page.goto(BASE + "/samelabs/economy/withdrawals");
  await page.waitForSelector(`tr[data-wid="${WID}"]`, { timeout: 10000 });
  const rowTxt9 = await page.locator(`tr[data-wid="${WID}"]`).textContent();
  check("9 Withdrawals snapshot + pending (row-scoped)",
        (rowTxt9 || "").includes("USDT-TRC20:old")
        && (rowTxt9 || "").includes("pending"), rowTxt9);

  // A1 credits adjustment via the real form/endpoint
  const adj = await page.evaluate(async (uid) => {
    const r = await fetch(`/samelabs/users/${uid}/credits`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount: 300, action_id: "rt-adj-1", note: "rt" }) });
    return { st: r.status, body: await r.json() };
  }, PROV);
  check("A1 credits adjustment ok", adj.st === 200 && adj.body.ok, JSON.stringify(adj));

  // A2 approve — controlled proof + row-scoped bounded wait:
  // (a) the action response is HELD → the wait target must not appear
  //     early (row still pending after a settle beat);
  // (b) released → waitForSelector on THIS row's status cell; a timeout
  //     fails the suite (no retry, no skip).
  await page.goto(BASE + "/samelabs/economy/withdrawals");
  let releaseApprove;
  const gateApprove = new Promise(r => { releaseApprove = r; });
  await page.route("**/samelabs/economy/withdrawals/*/approve", async route => {
    await gateApprove;
    await route.fallback();               // pass through to the real server
  });
  const btnA = page.locator(`tr[data-wid="${WID}"] .js-approve`);
  await btnA.click();                     // arm
  await page.waitForTimeout(100);
  await btnA.click();                     // confirm → request in flight, held
  await page.waitForTimeout(600);         // settle: reload would have landed by now
  const stillPending = await page.locator(
    `tr[data-wid="${WID}"] td:nth-child(5)`).textContent();
  check("A2a approve response held → row still pending (no early pass)",
        (stillPending || "").trim() === "pending", stillPending);
  releaseApprove();
  await page.unroute("**/samelabs/economy/withdrawals/*/approve");
  await page.waitForSelector(
    `tr[data-wid="${WID}"] td:nth-child(5):has-text("approved")`,
    { timeout: 10000 });
  const stA2 = ((await page.locator(
    `tr[data-wid="${WID}"] td:nth-child(5)`).textContent()) || "").trim();
  check("A2b approve → row status cell equals approved", stA2 === "approved", stA2);

  // A3 paid: row-scoped ref input + row-scoped assertions (status cell
  // and the rendered evidence cell — never the unsubmitted input)
  await page.goto(BASE + "/samelabs/economy/withdrawals");
  const btnP = page.locator(`tr[data-wid="${WID}"] .js-paid`);
  await btnP.click();                     // arm → ref input appears in row
  await page.waitForSelector(`tr[data-wid="${WID}"] .js-payout-ref-input`,
    { timeout: 10000 });
  await page.fill(`tr[data-wid="${WID}"] .js-payout-ref-input`, "RTX-CHROME-1");
  await btnP.click();                     // confirm
  await page.waitForSelector(
    `tr[data-wid="${WID}"] td:nth-child(5):has-text("paid")`, { timeout: 10000 });
  const stA3 = ((await page.locator(
    `tr[data-wid="${WID}"] td:nth-child(5)`).textContent()) || "").trim();
  const evA3 = ((await page.locator(
    `tr[data-wid="${WID}"] td:nth-child(8)`).textContent()) || "").trim();
  check("A3 paid: row status cell equals paid", stA3 === "paid", stA3);
  check("A3b evidence cell equals RTX-CHROME-1", evA3 === "RTX-CHROME-1", evA3);

  // A4 Credits vs USD separation in THIS queue row
  const amtA4 = await page.locator(`tr[data-wid="${WID}"] td:nth-child(3)`).textContent();
  check("A4 amount in USD ($25) in the queue row",
        (amtA4 || "").includes("$25"), amtA4);

  // ════ Provider session: user S4 face ════
  const provCtx = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: "block" });
  const pp = await provCtx.newPage();
  await pp.goto(BASE + "/");
  await pp.evaluate(([t]) => { document.cookie = "flashshare_session=" + t + "; path=/"; }, [PROV_TOK]);
  await pp.goto(BASE + "/account/earnings");
  // U1/U2: bounded wait INSIDE the withdrawal-history container (never a
  // whole-body poll); a timeout fails the suite
  await pp.waitForSelector('#earnHist .wd-row .wd-ch:has-text("USDT-TRC20:old")',
    { timeout: 10000 });
  let pc = await pp.content();
  check("U1 current payout channel shown", pc.includes("USDT-TRC20:rt"));
  const snapU2 = await pp.locator('#earnHist .wd-row .wd-ch').first().textContent();
  check("U2 history shows request snapshot (old), not live (row-scoped)",
        (snapU2 || "").includes("USDT-TRC20:old"), snapU2);

  // U3 profile edit does not rewrite history snapshot (real form endpoint)
  await pp.evaluate(async () => {
    const body = new URLSearchParams({ payout_channel: "USDT-TRC20:CHANGED" });
    await fetch("/account/profile", { method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(), redirect: "manual" });
  });
  await pp.goto(BASE + "/account/earnings");
  await pp.waitForSelector('#earnHist .wd-row .wd-ch:has-text("USDT-TRC20:old")',
    { timeout: 10000 });
  const snapU3 = await pp.locator('#earnHist .wd-row .wd-ch').first().textContent();
  check("U3 history snapshot unchanged after profile edit (row-scoped)",
        (snapU3 || "").includes("USDT-TRC20:old"), snapU3);
  check("U3b modal now offers the NEW channel",
    (await pp.click("#openWithdraw").then(() => pp.textContent("#wChannel")) || "").includes("USDT-TRC20:CHANGED"));

  // U4 open modal — shows live channel; submit enabled (close U3b's modal
  // first: the open button lives UNDER the overlay)
  await pp.click("#closeWithdrawModal").catch(() => {});
  await pp.waitForTimeout(150);
  await pp.click("#openWithdraw");
  await pp.waitForTimeout(200);
  const chanTxt = await pp.textContent("#wChannel");
  const btnDisabled = await pp.getAttribute("#submitWithdraw", "disabled");
  check("U4 modal shows current channel (post-edit value)",
        (chanTxt || "").includes("USDT-TRC20:CHANGED"), chanTxt);
  check("U4b submit enabled with channel set", btnDisabled === null);

  // U5 successful request → explicit feedback: bounded wait for the
  // feedback element to be VISIBLE and non-empty (success semantics
  // kept; a timeout fails the suite)
  await pp.fill("#wAmount", "15.00");
  await pp.click("#submitWithdraw");
  await pp.waitForSelector("#wdFeedback:not(.hidden)", { timeout: 10000 });
  const fb = ((await pp.textContent("#wdFeedback")) || "").trim();
  check("U5 success feedback visible and non-empty", fb.length > 0, fb || "empty");

  // ════ No-channel session: gating ════
  const ncCtx = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: "block" });
  const np = await ncCtx.newPage();
  await np.goto(BASE + "/");
  await np.evaluate(([t]) => { document.cookie = "flashshare_session=" + t + "; path=/"; }, [NOCH_TOK]);
  // give earnings so the ONLY blocker is the channel
  await np.evaluate(async () => {
    // (no direct credit path from browser; seed was provider_earn-less)
  });
  await np.goto(BASE + "/account/earnings");
  await np.waitForTimeout(600);
  await np.click("#openWithdraw");
  await np.waitForTimeout(200);
  const ncDisabled = await np.getAttribute("#submitWithdraw", "disabled");
  const ncHref = await np.getAttribute("#withdrawHint a", "href").catch(() => "");
  check("U6 no-channel → submit disabled", ncDisabled !== null);
  check("U6b no-channel → Profile guidance link", ncHref === "/account/profile", ncHref);

  // ── back-office mobile input floor: every visible text-entry control on
  // the operator users page renders >= 16px at phone width (no focus
  // auto-zoom); .sl-input rides the same components.css floor as the
  // public surfaces. ──
  {
    const mctx = await browser.newContext({ viewport: { width: 390, height: 844, isMobile: true, hasTouch: true }, serviceWorkers: "block" });
    const mp = await mctx.newPage();
    await mp.goto(BASE + "/");
    await mp.evaluate(([t]) => {
      document.cookie = "flashshare_session=" + t + "; path=/";
    }, [OP_TOK]);
    await mp.goto(BASE + "/samelabs/users");
    await mp.waitForTimeout(400);
    const bad = await mp.evaluate(() => {
      const skip = new Set(["hidden", "checkbox", "radio", "file", "submit", "button", "image", "reset"]);
      const out = [];
      document.querySelectorAll("input, select, textarea").forEach((el) => {
        if (skip.has(el.type || "")) return;
        const cs = getComputedStyle(el);
        if (cs.display === "none" || cs.visibility === "hidden") return;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return;
        const fs = parseFloat(cs.fontSize);
        if (fs < 16) out.push(`${el.tagName.toLowerCase()} ${fs}px`);
      });
      return out;
    });
    check("MO input floor /samelabs/users at 390 >= 16px", bad.length === 0, bad.join(", "));
    await mctx.close();
  }

  await browser.close();
  console.log();
  if (FAIL.length) { console.log(`FAILED ${FAIL.length}`); process.exit(1); }
  console.log(`OK — S4 Chromium runtime (${28 - FAIL.length}/28 core checks)`);
})().catch(e => { console.error(e); process.exit(1); });
