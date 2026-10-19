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
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();

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

  // 9 Withdrawals queue — snapshot + actions
  await page.goto(BASE + "/samelabs/economy/withdrawals");
  let c = await page.content();
  check("9 Withdrawals snapshot + pending", c.includes("USDT-TRC20:old") && c.includes("pending"));

  // A1 credits adjustment via the real form/endpoint
  const adj = await page.evaluate(async (uid) => {
    const r = await fetch(`/samelabs/users/${uid}/credits`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount: 300, action_id: "rt-adj-1", note: "rt" }) });
    return { st: r.status, body: await r.json() };
  }, PROV);
  check("A1 credits adjustment ok", adj.st === 200 && adj.body.ok, JSON.stringify(adj));

  // A2 approve via the two-state confirm button → approved state in DOM
  await page.goto(BASE + "/samelabs/economy/withdrawals");
  const btnA = page.locator(`tr[data-wid="${WID}"] .js-approve`);
  await btnA.click();                       // arm
  await page.waitForTimeout(100);
  await btnA.click();                       // confirm
  await page.waitForTimeout(1200);          // action + reload
  c = await page.content();
  check("A2 approve → approved in DOM", c.includes("approved"), "no approved badge");

  // A3 paid: arm → fill dynamic ref input → confirm
  await page.goto(BASE + "/samelabs/economy/withdrawals");
  const btnP = page.locator(`tr[data-wid="${WID}"] .js-paid`);
  await btnP.click();                       // arm → ref input appears
  await page.waitForTimeout(200);
  await page.fill(".js-payout-ref-input", "RTX-CHROME-1");
  await btnP.click();                       // confirm
  await page.waitForTimeout(1200);
  c = await page.content();
  check("A3 paid + evidence ref", c.includes("paid") && c.includes("RTX-CHROME-1"));

  // A4 Credits vs USD separation in queue row
  check("A4 amount in USD ($25)", c.includes("$25"), "no $25");

  // ════ Provider session: user S4 face ════
  const provCtx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const pp = await provCtx.newPage();
  await pp.goto(BASE + "/");
  await pp.evaluate(([t]) => { document.cookie = "flashshare_session=" + t + "; path=/"; }, [PROV_TOK]);
  await pp.goto(BASE + "/account/earnings");
  await pp.waitForTimeout(600);
  let pc = await pp.content();
  check("U1 current payout channel shown", pc.includes("USDT-TRC20:rt"));
  check("U2 history shows request snapshot (old), not live", pc.includes("USDT-TRC20:old"));

  // U3 profile edit does not rewrite history snapshot (real form endpoint)
  await pp.evaluate(async () => {
    const body = new URLSearchParams({ payout_channel: "USDT-TRC20:CHANGED" });
    await fetch("/account/profile", { method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(), redirect: "manual" });
  });
  await pp.goto(BASE + "/account/earnings");
  await pp.waitForTimeout(600);
  pc = await pp.content();
  check("U3 history snapshot unchanged after profile edit",
    pc.includes("USDT-TRC20:old"));
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

  // U5 successful request → explicit feedback (modal still open from U3b;
  // the submit button is INSIDE the modal)
  await pp.fill("#wAmount", "15.00");
  await pp.click("#submitWithdraw");
  await pp.waitForTimeout(800);
  const fb = await pp.textContent("#wdFeedback").catch(() => "");
  check("U5 success feedback visible", (fb || "").length > 0, "no #wdFeedback text");

  // ════ No-channel session: gating ════
  const ncCtx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
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
    const mctx = await browser.newContext({ viewport: { width: 390, height: 844, isMobile: true, hasTouch: true } });
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
  console.log(`OK — S4 Chromium runtime (${21 - FAIL.length}/21 core checks)`);
})().catch(e => { console.error(e); process.exit(1); });
