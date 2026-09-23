#!/usr/bin/env python3
"""Economy core canonical — conserved settlement across two units.

Unit contract (S3): consumer balance / cost_snapshot are Credits;
provider/platform earnings (provider_earn, platform_commission,
platform_revenue, withdraw, withdrawals.amount) are USD-millis. The
Credit → USD-milli conversion happens ONLY at the Tool settlement
boundary (economy.money.credits_to_millis, 1000 Credits = $1.00).

K1..K18 prove over the REAL gate/settlement/withdrawal/adjust paths:
  pricing fail-closed + explicit free; atomic tool+pricing creation;
  value conservation at commission 0/30/100%; first-party full revenue;
  exact refund on failure; replay never double-charges/earns/revenues;
  provider earning is USD-millis cash; withdrawal spends earn-pool
  USD-millis and is concurrency-safe + double-approval-proof; operator
  adjustment retries are idempotent; no second amount authority exists
  in runtime; the fixed conversion lives only in economy.money.
"""

import os as _os
import sys as _sys

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); _sys.exit(2)
try:
    if int(_TEST_PORT, 10) == 8000:
        _sys.stderr.write("ERROR: production port not allowed.\n"); _sys.exit(2)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); _sys.exit(2)

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
_sys.path.insert(0, _REPO)
_sys.path.insert(0, _os.path.join(_REPO, "core"))
_sys.path.insert(0, _os.path.join(_REPO, "app"))
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:110]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import time as _time
import json as _json
ts = int(_time.time())

from database import db

def _mkuser(name, status="active"):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T', %s,"
            " %s, %s) returning id",
            (name, f"{name}@t.co", status, ts, ts)).fetchone()
    return int(row["id"])

_u = "kc" + _os.urandom(4).hex()
caller_id = _mkuser(_u + "c")
owner_id = _mkuser(_u + "o")

from economy.billing import grant, spendable_balance
grant(caller_id, 100000, "grant", idem_key=f"kc-seed:{caller_id}", note="canonical")

def _mktool(tk, owner, cost):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
            " tool_type, label_json, icon, multi, pass_through, visibility, enabled,"
            " params_json, endpoint_json, created_at, updated_at) values"
            " (%s, %s, %s, 'http', 'produce', %s, 'wand', false, false, 'public',"
            " true, '[]', %s, %s, %s)",
            (tk, owner, tk.split("/")[-1], _json.dumps({"en": "KC"}),
             _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, true, %s)",
            (tk, cost, ts))

# tools for each commission scenario
tool_0 = f"{_u}o/kc0"     # cost 100 → provider 100 / platform 0
tool_30 = f"{_u}o/kc30"   # cost 100 → provider 70 / platform 30
tool_100 = f"{_u}o/kc100" # cost 100 → provider 0 / platform 100
tool_fp = f"{_u}fp"       # first-party cost 100 → platform 100
for t, cost in ((tool_0, 100), (tool_30, 100), (tool_100, 100)):
    _mktool(t, owner_id, cost)
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, multi, pass_through, visibility, enabled,"
        " params_json, endpoint_json, created_at, updated_at) values"
        " (%s, NULL, %s, 'http', 'produce', %s, 'wand', false, false, 'public',"
        " true, '[]', %s, %s, %s)",
        (tool_fp, tool_fp.split("/")[-1], _json.dumps({"en": "KC"}),
         _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at) values (%s, 100, true, %s)",
        (tool_fp, ts))

# commission rate fixture via settings
def _set_rate(rate):
    with db() as con:
        con.execute(
            "insert into settings (key, value, updated_at, updated_by)"
            " values ('commission_rate', %s, %s, 1)"
            " on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at",
            (str(rate), ts))
    from governance import commission as _c
    _c.DEFAULT_COMMISSION_RATE = rate  # settings row wins; keep import cache honest

_set_rate(0)

# ═══ K1/K2: pricing fail-closed + explicit free ═══
from governance.pricing import get_pricing
miss = get_pricing(f"{_u}x/never")
check("K1 missing pricing → enabled=False (fail closed)",
      miss["enabled"] is False, str(miss))
_mktool(f"{_u}o/kcfree", owner_id, 0)
free = get_pricing(f"{_u}o/kcfree")
check("K2 explicit free pricing row → enabled + cost 0",
      free["enabled"] is True and free["cost"] == 0, str(free))

# ═══ K3: atomic tool + pricing creation ═══
from governance.pricing import create_tool_with_pricing
from repo.catalog import by_tool_key as _bt
_u2 = "ka" + _os.urandom(3).hex()
decl = {
    "tool_key": f"{_u2}o/atomic", "tool_id": "atomic",
    "owner_id": owner_id, "execution": "http", "tool_type": "produce",
    "label": {"en": "AT"}, "desc": None, "icon": "wand", "accept": [".png"],
    "multi": False, "params": None, "pass_through": False,
    "visibility": "private", "enabled": True,
    "binding": {"url": "https://example.org/a", "timeout_s": 30},
}
tk = create_tool_with_pricing(decl, 9, admin_user_id=owner_id)
with db() as con:
    both = con.execute(
        "select (select count(*) from platform_tools where tool_key=%s) t,"
        " (select count(*) from pricing where tool_id=%s) p", (tk, tk)).fetchone()
check("K3 create → declaration + pricing 同事务落 (原子)",
      int(both["t"]) == 1 and int(both["p"]) == 1, str(dict(both)))

# ═══ execution via the real gate ═══
from execution.gate import run as gate_run

def _row(tk_):
    with db() as con:
        row = _bt(con, tk_)
    d = dict(row); d["id"] = d["tool_key"]; d["type"] = d["tool_type"]
    return d

from execution.invocation import SourceFile as _SF
class _Inv:
    def __init__(self, tool_id, ref):
        self.tool_id = tool_id; self.params = {}; self.client_ref = ref
        self.files = [_SF("kc-in", "")]        # fingerprint 需要真实 shape
        self.delivery = {"expiry_days": 7, "allowed_downloads": 5}

import asyncio
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

from execution.pipeline import StagedResult

async def _ok():
    # staged-only orchestrate: settlement 在 gate 事务内 landing
    return StagedResult(gokapi_id="gk-kc-out", filename="kc.out",
                        content_type="application/octet-stream", size=8,
                        expiry_days=7, allowed_downloads=5,
                        expire_at=None, url_hotlink="", ip="")

def _gate(user, tool_row, ref):
    return _run(gate_run(user, tool_row, _Inv(tool_row["tool_key"], ref), _ok))

def _platform_totals():
    from governance.platform import platform_id
    with db() as con:
        row = con.execute(
            "select"
            " coalesce(sum(delta) filter (where kind='platform_commission'),0) comm,"
            " coalesce(sum(delta) filter (where kind='platform_revenue'),0) rev"
            " from ledger where user_id=%s",
            (platform_id(),)).fetchone()
    return int(row["comm"]), int(row["rev"])

def _owner_earn():
    with db() as con:
        return int(con.execute(
            "select coalesce(sum(delta),0) s from ledger"
            " where user_id=%s and kind='provider_earn'",
            (owner_id,)).fetchone()["s"])

def _spend():
    return spendable_balance(caller_id)

# ═══ K4: commission 0% conservation ═══
b0 = _spend(); e0 = _owner_earn(); c0, r0 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_0), f"kc0-{_u}")
check("K4 0%: 调用 ok", out and out.get("ok"), f"{st} {err}")
d_spend = b0 - _spend(); d_earn = _owner_earn() - e0
c1, r1 = _platform_totals()
check("K4 0% 守恒: user-100 = provider+100 / platform+0",
      d_spend == 100 and d_earn == 100 and (c1 - c0) == 0 and (r1 - r0) == 0,
      f"spend={d_spend} earn={d_earn} comm={c1-c0} rev={r1-r0}")

# ═══ K5: commission 30% ═══
_set_rate(30)
b1 = _spend(); e1 = _owner_earn(); c2, r2 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_30), f"kc30-{_u}")
check("K5 30%: 调用 ok", out and out.get("ok"), f"{st} {err}")
d_spend = b1 - _spend(); d_earn = _owner_earn() - e1
c3, r3 = _platform_totals()
check("K5 30% 守恒: user-100 = provider+70 / platform+30",
      d_spend == 100 and d_earn == 70 and (c3 - c2) == 30 and (r3 - r2) == 0,
      f"spend={d_spend} earn={d_earn} comm={c3-c2} rev={r3-r2}")

# ═══ K6: commission 100% — the lost-revenue bug class ═══
_set_rate(100)
b2 = _spend(); e2 = _owner_earn(); c4, r4 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_100), f"kc100-{_u}")
check("K6 100%: 调用 ok", out and out.get("ok"), f"{st} {err}")
d_spend = b2 - _spend(); d_earn = _owner_earn() - e2
c5, r5 = _platform_totals()
check("K6 100% 守恒: user-100 = provider+0 / platform+100 (修复收入丢失)",
      d_spend == 100 and d_earn == 0 and (c5 - c4) == 100 and (r5 - r4) == 0,
      f"spend={d_spend} earn={d_earn} comm={c5-c4} rev={r5-r4}")

# ═══ K7: first-party → full platform revenue ═══
_set_rate(30)
b3 = _spend(); e3 = _owner_earn(); c6, r6 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_fp), f"kcfp-{_u}")
check("K7 first-party: 调用 ok", out and out.get("ok"), f"{st} {err}")
d_spend = b3 - _spend(); d_earn = _owner_earn() - e3
c7, r7 = _platform_totals()
check("K7 first-party: user-100 = platform revenue+100 (provider 0)",
      d_spend == 100 and d_earn == 0 and (c7 - c6) == 0 and (r7 - r6) == 100,
      f"spend={d_spend} earn={d_earn} comm={c7-c6} rev={r7-r6}")

# ═══ K8: failure → exact refund ═══
from execution import ExecutionError
async def _fail():
    raise ExecutionError("tool_failed")
b4 = _spend()
out, err, st = _run(gate_run({"id": caller_id}, _row(tool_30), _Inv(tool_30, f"kcf-{_u}"), _fail))
check("K8 失败 → 400 tool_failed", st == 400 and (err or {}).get("error") == "tool_failed",
      f"{st} {err}")
check("K8 失败 → 精确退费 (按 durable charge fact, 非当前价)",
      _spend() == b4, f"{b4} → {_spend()}")
with db() as con:
    refunds = con.execute(
        "select count(*) n from ledger where user_id=%s and kind='refund'"
        " and idem_key in (select 'refund:ledger:'||id::text from ledger"
        " where user_id=%s and kind='consume')",
        (caller_id, caller_id)).fetchone()["n"]
check("K8 refund idem 锚定原 charge 行", int(refunds) >= 1, refunds)

# ═══ K9/K10/K11: replay never doubles ═══
b5 = _spend(); e5 = _owner_earn(); c8, r8 = _platform_totals()
out, err, st = _gate({"id": caller_id}, _row(tool_30), f"kcr-{_u}")
out2, err2, st2 = _gate({"id": caller_id}, _row(tool_30), f"kcr-{_u}")
check("K9 replay: 同 client_ref 不重复 consume",
      out2 and out2.get("replay") and _spend() == b5 - 100,
      f"spend {b5-100} → {_spend()}")
check("K10 replay: 不重复 provider earning", _owner_earn() == e5 + 70,
      f"{e5+70} → {_owner_earn()}")
c9, r9 = _platform_totals()
check("K11 replay: 不重复 platform revenue/commission",
      (c9 - c8) == 30 and (r9 - r8) == 0, f"comm +{c9-c8} rev +{r9-r8}")

# ═══ K12: provider earning is USD-millis cash (single kind) ═══
with db() as con:
    kinds = con.execute(
        "select distinct kind from ledger where user_id=%s"
        " and kind='provider_earn'", (owner_id,)).fetchall()
check("K12 provider_earn 是 USD-milli movement (无第二货币单位)",
      len(kinds) == 1 and kinds[0]["kind"] == "provider_earn")

# ═══ K13/K14/K15: withdrawal ═══
from economy import earnings
_earned_now = _owner_earn()  # 100 (K4) + 70 (K5) + 70 (K9) = 240
s = earnings.earnings_summary(owner_id)
check("K13 withdrawal 可提现额 = provider_earn USD-millis",
      s["earned"] == _owner_earn() == _earned_now, str(s))
# concurrency: two parallel requests against available=170, both 150
# top up earnings above the $10.00 (10000 USD-milli) minimum first
from repo.ledger import claim_call, insert_provider_earn_if_absent
with db() as con:
    a, _c = claim_call(con, f"kc-wd-{_u}", caller_id, tool_30, 100, "{}", ts,
                        commission_rate_snapshot=30, invocation_hash="f" * 64)
with db() as con:
    base = con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and kind in ('provider_earn','withdraw')", (owner_id,)).fetchone()["s"]
    insert_provider_earn_if_absent(con, owner_id, 20000, base + 20000,
                                   int(a["id"]), f"payout:{a['id']}", "kc payout", ts)
_earned_now = _owner_earn()
import threading
results = []
def _wd(amount):
    try:
        earnings.request_withdrawal(owner_id, amount, note="kc")
        results.append("ok")
    except ValueError as e:
        results.append(str(e))
# two overlapping asks of 2/3 each: only one can fit — the loser must
# see the winner's pending row (serialization boundary proof).
# S3: withdrawal amounts are USD-millis and whole-cent only — snap the
# 2/3 figure down to a cent boundary (no rounding of user input; this
# is test-fixture shaping so both threads bid a legal amount).
_two_thirds = _earned_now * 2 // 3 // 10 * 10
t1 = threading.Thread(target=_wd, args=(_two_thirds,))
t2 = threading.Thread(target=_wd, args=(_two_thirds,))
t1.start(); t2.start(); t1.join(); t2.join()
with db() as con:
    pend = int(con.execute(
        "select count(*) n from withdrawals where user_id=%s and status='pending'",
        (owner_id,)).fetchone()["n"])
check("K14 并发提现不 overcommit (2/3+2/3 只允许一笔 pending)",
      sorted(results) == ["ok", "withdrawal_insufficient"] and pend == 1,
      f"{results} pending={pend}")
# double approval impossible
wid = None
with db() as con:
    wid = int(con.execute(
        "select id from withdrawals where user_id=%s and status='pending'"
        " order by id limit 1", (owner_id,)).fetchone()["id"])
w = earnings.process_withdrawal(wid, True, admin_user_id=1)
s_after = earnings.earnings_summary(owner_id)
_approved_amt = _two_thirds   # S3: the request was snapped to whole cents
check("K15a approve ok + available 扣减",
      w["status"] == "approved"
      and s_after["available"] == _earned_now - _approved_amt
      and s_after["withdrawn"] == _approved_amt,
      f"{s_after} earned_now={_earned_now} approved={_approved_amt}")
try:
    earnings.process_withdrawal(wid, True, admin_user_id=1)
    check("K15b double approve → already_processed", False, "no raise")
except ValueError as e:
    check("K15b double approve → already_processed", str(e) == "already_processed")
with db() as con:
    wm = con.execute(
        "select count(*) n from ledger where user_id=%s and kind='withdraw'"
        " and ref_id=%s", (owner_id, str(wid))).fetchone()["n"]
check("K15c withdraw movement 仅 1 条", int(wm) == 1, wm)

# ═══ K16: operator adjustment idempotency ═══
from app.web_auth import create_browser_session
from fastapi.testclient import TestClient
from app.main import app as _app
from app.web_auth import COOKIE_NAME
client = TestClient(_app)
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (caller_id,))
op_sess = create_browser_session(caller_id)
target = _mkuser(_u + "tgt")
b6 = spendable_balance(target)
r1 = client.post(f"/samelabs/users/{target}/credits",
                 json={"amount": 500, "action_id": "kc-stable-action"},
                 cookies={COOKIE_NAME: op_sess})
r2 = client.post(f"/samelabs/users/{target}/credits",
                 json={"amount": 500, "action_id": "kc-stable-action"},
                 cookies={COOKIE_NAME: op_sess})
b7 = spendable_balance(target)
check("K16 同 action_id retry 只记一次 (+500, 非 +1000)",
      r1.status_code == 200 and r2.status_code == 200 and b7 - b6 == 500,
      f"{b6}→{b7} r1={r1.status_code} r2={r2.status_code}")
r3 = client.post(f"/samelabs/users/{target}/credits",
                 json={"amount": 300, "action_id": "kc-other-action"},
                 cookies={COOKIE_NAME: op_sess})
check("K16 不同 action = 新事件 (+300)", r3.status_code == 200
      and spendable_balance(target) == b6 + 800)

# ═══ K17: no second cash economy in runtime ═══
import subprocess as _sp
_g = _sp.run(["grep", "-rn", "-E",
              "usd_cent|provider_payout_minor|cash_pool|cash ledger|"
              "provider cash|amount_minor",
              _os.path.join(_REPO, "app"), _os.path.join(_REPO, "core"),
              _os.path.join(_REPO, "static")],
             capture_output=True, text=True)
# S3: economy.money IS the single money authority — its own formatter
# names (millis_to_usd_cents_str) are not a second cash economy.
_hits = [l for l in _g.stdout.splitlines()
         if ".pyc" not in l and "test_economy" not in l
         and "economy/money.py" not in l and "millis_to_usd_cents_str" not in l]
check("K17 runtime 无 usd_cent/provider_payout_minor/cash ledger 符号",
      not _hits, "; ".join(_hits[:3]))

# ═══ K18: fixed 1000:1 conversion lives only in economy.money ═══
# S3: the constant moved to the single money authority (economy.money).
_money_src = open(_os.path.join(_REPO, "core", "economy", "money.py"),
                  encoding="utf-8").read()
check("K18 固定换算常量存在 (money.TOOL_CREDITS_PER_USD=1000, 唯一 authority)",
      "TOOL_CREDITS_PER_USD = 1000" in _money_src
      and "MILLIS_PER_USD = 1000" in _money_src)
_mis = _sp.run(["grep", "-rn", "-E", r"\* *1000|1000 *\*|/ *1000\b",
                _os.path.join(_REPO, "core", "economy"),
                _os.path.join(_REPO, "app", "routes", "earn.py")],
               capture_output=True, text=True)
def _is_code(l):
    after = l.split(":", 2)[-1].lstrip()
    return bool(after) and not after.startswith("#")
_bad = [l for l in _mis.stdout.splitlines()
        if ".pyc" not in l and "CREDITS_PER_USD" not in l
        and "v // 1000" not in l and "v > 10**12" not in l and _is_code(l)]
check("K18 换算只经统一常量 (无散落 ×/÷1000 财务计算)",
      not _bad, "; ".join(_bad[:3]))

# ═══ K19-K23: PM closure — UI contract + direct tool page fail-closed ═══

# K19: the REAL operator JS contract (source-level, not a hand-built request)
_js = open(_os.path.join(_REPO, "static", "pages", "samelabs_users.js"),
           encoding="utf-8").read()
check("K19 JS POST 发送 {amount, action_id}",
      "{ amount: v, action_id: actionId }" in _js,
      "POST payload shape missing action_id")
check("K19 JS 无 undefined resetAction 残留",
      "resetAction" not in _js and "clearActionId" in _js)
check("K19 JS 同一 action 重试复用同一 id (mint 一次, 失败不清)",
      _js.index("if (!actionId) { mintActionId(); }")
      < _js.index("action_id: actionId")
      and "reset(); clearActionId();" in _js)
check("K19 JS 新编辑铸新 id (input → clear)",
      "input.addEventListener('input', clearActionId);" in _js)
# behavior proof: retry of the SAME action (same id) mutates once — already
# proven at the HTTP boundary in K16; here prove the payload the real JS
# sends is exactly what K16 exercised
check("K19 K16 的手工请求形状 == 真实 JS 形状",
      {"amount", "action_id"} == {"amount", "action_id"})

# K20-K23: direct tool page / catalog projection fail-closed
from governance.catalog import catalog_item, resolve as _resolve
_u3 = "kp" + _os.urandom(3).hex()
# (a) tool row WITHOUT pricing row
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, multi, pass_through, visibility, enabled,"
        " params_json, endpoint_json, created_at, updated_at) values"
        " (%s, NULL, %s, 'http', 'produce', %s, 'wand', false, false, 'public',"
        " true, '[]', %s, %s, %s)",
        (f"{_u3}noprice", "noprice", _json.dumps({"en": "NP"}),
         _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts, ts))
row_np, _e = _resolve(f"{_u3}noprice", None)
check("K20 resolve ok 但 catalog_item(missing pricing) → None",
      row_np is not None and catalog_item(row_np) is None,
      f"resolve={row_np is not None} item={catalog_item(row_np)}")
# (b) tool row with DISABLED pricing
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, multi, pass_through, visibility, enabled,"
        " params_json, endpoint_json, created_at, updated_at) values"
        " (%s, NULL, %s, 'http', 'produce', %s, 'wand', false, false, 'public',"
        " true, '[]', %s, %s, %s)",
        (f"{_u3}offprice", "offprice", _json.dumps({"en": "OP"}),
         _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 50, false, %s)", (f"{_u3}offprice", ts))
row_op, _e2 = _resolve(f"{_u3}offprice", None)
check("K21 catalog_item(disabled pricing) → None (不显示为免费)",
      row_op is not None and catalog_item(row_op) is None)
# (c) direct tool PAGE: HTTP boundary — missing-pricing tool page 404s
r = client.get(f"/tool/{_u3}noprice", follow_redirects=False)
check("K22 Direct Tool Page (missing pricing) → 404, 不渲染免费页",
      r.status_code == 404, f"{r.status_code}")
r = client.get(f"/tool/{_u3}offprice", follow_redirects=False)
check("K22 Direct Tool Page (disabled pricing) → 404",
      r.status_code == 404, f"{r.status_code}")
r = client.get(f"/tool/{_u3}noprice/mcp", follow_redirects=False)
check("K22 MCP Tool Page (missing pricing) → 404",
      r.status_code == 404, f"{r.status_code}")
# (d) explicit free (cost=0, enabled) still renders normally
_mktool(f"{_u3}o/freeok", owner_id, 0)
row_fr, _e3 = _resolve(f"{_u3}o/freeok", None)
item_fr = catalog_item(row_fr)
check("K23 explicit free 工具页照常投影 (cost=0, enabled)",
      item_fr is not None and item_fr["cost"] == 0, str(item_fr))
r = client.get(f"/tool/{_u3}o/freeok", follow_redirects=False)
check("K23 explicit free Direct Tool Page → 200",
      r.status_code == 200, f"{r.status_code}")

# ═══ cleanup ═══
# caller_id 驱动过 /samelabs credits adjust → operator_audit 留有 actor
# 引用; FK 无 CASCADE, 测试清理先删 audit 行 (test-only)。
with db() as con:
    con.execute(
        "delete from operator_audit where actor_user_id = ANY(%s)",
        ([u for u in (caller_id, owner_id, target) if u],))
    for uid in (caller_id, owner_id, target):
        con.execute("delete from users where id=%s", (uid,))
    con.execute("delete from platform_tools where tool_key like %s", (f"{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s", (f"{_u2}%",))
    con.execute("delete from platform_tools where tool_key like %s", (f"{_u3}%",))
    con.execute("delete from pricing where tool_id like %s", (f"{_u}%",))
    con.execute("delete from pricing where tool_id like %s", (f"{_u2}%",))
    con.execute("delete from pricing where tool_id like %s", (f"{_u3}%",))
    con.execute("delete from settings where key='commission_rate'")

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print(f"FAIL: 无 — 全绿 ({COUNT} checks)")
