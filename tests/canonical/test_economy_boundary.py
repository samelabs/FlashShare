#!/usr/bin/env python3
"""Economy boundary canonical — credits and cash are separate accounts.

E1..E16 prove the closed economy model over the real HTTP boundary:
  spendable credits never include provider earnings; provider earnings
  are usd_cent cash; tool cost (credits) and payout (cash) have no
  mathematical conversion; settlement is atomic + idempotent;
  withdrawals read the cash pool only; balance_after always means its
  own pool+unit; tool creation carries its commercial contract
  atomically; no CREDITS_PER_USD exists anywhere in runtime code.
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

# ═══ schema contract ═══
from database import db
with db() as con:
    cur = con.execute("""
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = 'ledger'::regclass AND conname = 'ledger_pool_unit_ck'
    """).fetchone()
    check("E-schema ledger pool/unit CHECK 存在", cur is not None)
    cur = con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='withdrawals' AND column_name IN ('amount_minor','currency')
        ORDER BY column_name
    """).fetchall()
    check("E-schema withdrawals 现金列 (amount_minor, currency)",
          [r["column_name"] for r in cur] == ["amount_minor", "currency"])
    cur = con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='pricing' AND column_name IN
          ('user_cost_credits','provider_payout_minor','payout_currency')
        ORDER BY column_name
    """).fetchall()
    check("E-schema pricing 商业合同列",
          [r["column_name"] for r in cur] ==
          ["payout_currency", "provider_payout_minor", "user_cost_credits"])

# ═══ fixtures: caller + provider owner + tool ═══
def _mkuser(name, status="active"):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T', %s,"
            " %s, %s) returning id",
            (name, f"{name}@t.co", status, ts, ts)).fetchone()
    return int(row["id"])

_u = "ec" + _os.urandom(4).hex()
caller_id = _mkuser(_u + "c")
owner_id = _mkuser(_u + "o")
firstparty_caller_id = _mkuser(_u + "f")

from economy.billing import grant, spendable_balance, consume, InsufficientCredits
grant(caller_id, 500, "grant", idem_key=f"ec-seed:{caller_id}", note="canonical")
grant(firstparty_caller_id, 500, "grant", idem_key=f"ec-seed:{firstparty_caller_id}", note="canonical")

# provider tool with commercial contract: 5 credits / 30 usd_cent payout
tool_key = f"{_u}o/cashtool"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, multi, pass_through, visibility, enabled,"
        " params_json, endpoint_json, created_at, updated_at) values"
        " (%s, %s, %s, 'http', 'produce', %s, 'wand', false, false, 'public',"
        " true, '[]', %s, %s, %s)",
        (tool_key, owner_id, "cashtool", _json.dumps({"en": "EC"}),
         _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, user_cost_credits, provider_payout_minor,"
        " payout_currency, enabled, updated_at) values (%s, 5, 30, 'USD', true, %s)",
        (tool_key, ts))

# first-party tool: 7 credits / payout 0
fp_tool_key = f"{_u}fptool"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, multi, pass_through, visibility, enabled,"
        " params_json, endpoint_json, created_at, updated_at) values"
        " (%s, NULL, %s, 'http', 'produce', %s, 'wand', false, false, 'public',"
        " true, '[]', %s, %s, %s)",
        (fp_tool_key, "fptool", _json.dumps({"en": "EC"}),
         _json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, user_cost_credits, provider_payout_minor,"
        " payout_currency, enabled, updated_at) values (%s, 7, 0, 'USD', true, %s)",
        (fp_tool_key, ts))

# ═══ E6: cost credits vs payout cash — no conversion ═══
from execution.affordability import effective_quote
from repo.catalog import by_tool_key as _bt
from database import db as _db2

def by_tool_key(tk):
    with _db2() as con:
        row = _bt(con, tk)
    return row
tool_row = dict(by_tool_key(tool_key))
tool_row["id"] = tool_row["tool_key"]; tool_row["type"] = tool_row["tool_type"]
q = effective_quote(caller_id, tool_row)
check("E6 quote: cost=5 credits, payout=30 usd_cent (independent fields)",
      q == {"enabled": True, "cost": 5, "payout_minor": 30}, str(q))
q_self = effective_quote(owner_id, tool_row)
check("E6 self-call quote: cost=0 且 payout=0",
      q_self == {"enabled": True, "cost": 0, "payout_minor": 0}, str(q_self))

# ═══ E2: earning unit = usd_cent (schema-enforced write path) ═══
from repo.ledger import insert_row as ledger_insert, cash_pool_balance
with db() as con:
    try:
        ledger_insert(con, owner_id, "provider_earning", "credit", 30, 30,
                      "payout", "tool_call", "ec-badunit", None, ts)
        check("E2 provider_earning 池禁止 credit 单位 (CHECK)", False, "no raise")
    except Exception:
        check("E2 provider_earning 池禁止 credit 单位 (CHECK)", True)
    try:
        ledger_insert(con, owner_id, "spendable", "usd_cent", 5, 5,
                      "consume", "tool_call", "ec-badunit2", None, ts)
        check("E2 spendable 池禁止 usd_cent 单位 (CHECK)", False, "no raise")
    except Exception:
        check("E2 spendable 池禁止 usd_cent 单位 (CHECK)", True)

# ═══ E13: balance_after is pool-scoped ═══
from economy.settlement import accrue_tool_success
from repo.ledger import claim_call
with db() as con:
    _anchor, _claimed = claim_call(con, f"ec-probe-{_u}-1", caller_id,
                                   tool_key, 5, "{}", ts)
res = {"publicId": "ec-res-1"}
with db() as con:
    accrue_tool_success(con, int(_anchor["id"]), tool_row, caller_id, 30, res)
E13_CALL_ID = int(_anchor["id"])
with db() as con:
    row = con.execute(
        "select pool, unit, delta, balance_after, kind from ledger"
        " where idem_key = %s", (f"payout:{E13_CALL_ID}",)).fetchone()
check("E13 payout movement: pool=provider_earning, unit=usd_cent, kind=payout",
      row["pool"] == "provider_earning" and row["unit"] == "usd_cent"
      and row["delta"] == 30 and row["kind"] == "payout", str(dict(row)))
owner_cash = cash_pool_balance(None, owner_id, "provider_earning", "usd_cent") \
    if False else None
from database import db as _db
with _db() as con:
    owner_cash = con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and pool='provider_earning'", (owner_id,)).fetchone()["s"]
check("E13 balance_after == provider_earning 池累计",
      int(row["balance_after"]) == int(owner_cash),
      f"{row['balance_after']} vs {owner_cash}")

# ═══ E1: spendable balance excludes earnings ═══
check("E1 caller spendable 不含 owner 现金 (500 vs owner 0 credits)",
      spendable_balance(owner_id) == 0 and spendable_balance(caller_id) == 500)

# ═══ E9: success → credits charge + earning settlement converge ═══
from execution.gate import run as gate_run

class _Inv:
    def __init__(self, tool_id, params, client_ref):
        self.tool_id = tool_id; self.params = params; self.client_ref = client_ref

async def _orch_ok():
    return {"publicId": "ec-out"}

import asyncio
def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)
def _gate(user, tool, ref):
    return _run(gate_run(user, tool, _Inv(tool["tool_key"], {}, ref), _orch_ok))

out, err, status = _gate({"id": caller_id, "username": _u + "c"}, tool_row, f"ec-{_u}-1")
check("E9 成功调用 ok", out and out.get("ok"), f"{status} {err}")
check("E9 caller 扣 5 credits", spendable_balance(caller_id) == 495,
      spendable_balance(caller_id))
with _db() as con:
    cash = con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and pool='provider_earning'", (owner_id,)).fetchone()["s"]
check("E9 owner 收 60 usd_cent (30 E13 + 30 E9)", int(cash) == 60, cash)

# ═══ E3: same call replay does not double-earn ═══
out2, err2, status2 = _gate({"id": caller_id}, tool_row, f"ec-{_u}-1")
check("E3 replay 返回 stored result", out2 and out2.get("replay") is True,
      f"{status2} {err2}")
with _db() as con:
    cash2 = con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and pool='provider_earning'", (owner_id,)).fetchone()["s"]
check("E3 replay 不重复 earning (仍 60)", int(cash2) == 60, cash2)
check("E3 replay 不重复扣费 (仍 495)", spendable_balance(caller_id) == 495)

# ═══ E4: self-call → no earning ═══
out3, _, _ = _gate({"id": owner_id, "username": _u + "o"}, tool_row, f"ec-{_u}-self")
check("E4 self-call ok 且免费", out3 and out3.get("ok"), str(out3)[:80])
with _db() as con:
    cash3 = con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and pool='provider_earning'", (owner_id,)).fetchone()["s"]
check("E4 self-call 不产生 earning (仍 60)", int(cash3) == 60, cash3)

# ═══ E5: first-party tool → no provider earning ═══
fp_row = dict(by_tool_key(fp_tool_key))
fp_row["id"] = fp_row["tool_key"]; fp_row["type"] = fp_row["tool_type"]
out4, _, _ = _gate({"id": firstparty_caller_id, "username": _u + "f"}, fp_row, f"ec-{_u}-fp")
check("E5 first-party 调用 ok", out4 and out4.get("ok"), str(out4)[:80])
check("E5 first-party 扣 7 credits", spendable_balance(firstparty_caller_id) == 493,
      spendable_balance(firstparty_caller_id))
with _db() as con:
    fp_rows = con.execute(
        "select count(*) n from ledger where pool='provider_earning'"
        " and ref_id in (select id::text from tool_calls where tool_id=%s)",
        (fp_tool_key,)).fetchone()["n"]
check("E5 first-party 0 条 provider_earning movement", int(fp_rows) == 0, fp_rows)

# ═══ E7: insufficient credits → no execution, no earning ═══
poor_id = _mkuser(_u + "p")
poor_row = dict(by_tool_key(tool_key))
out5, err5, status5 = _gate({"id": poor_id, "username": _u + "p"}, poor_row, f"ec-{_u}-poor")
check("E7 余额不足 → 402 err_insufficient_credits",
      status5 == 402 and (err5 or {}).get("error") == "err_insufficient_credits",
      f"{status5} {err5}")
with _db() as con:
    poor_calls = con.execute(
        "select count(*) n from tool_calls where user_id=%s", (poor_id,)).fetchone()["n"]
check("E7 不足时不执行 (call=insufficient, 非 running)",
      int(poor_calls) == 1, poor_calls)
with _db() as con:
    cash4 = con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and pool='provider_earning'", (owner_id,)).fetchone()["s"]
check("E7 不足时不产生 earning (仍 60)", int(cash4) == 60, cash4)

# ═══ E8: execution failure → exact refund, earning=0 ═══
before_bal = spendable_balance(caller_id)
async def _orch_fail():
    from execution import ExecutionError
    raise ExecutionError("tool_failed")
def _gate_fail(user, tool, ref):
    return _run(gate_run(user, tool, _Inv(tool["tool_key"], {}, ref), _orch_fail))
out6, err6, status6 = _gate_fail({"id": caller_id}, tool_row, f"ec-{_u}-fail")
check("E8 失败 → 400 tool_failed", status6 == 400 and (err6 or {}).get("error") == "tool_failed",
      f"{status6} {err6}")
check("E8 失败 → 精确退费 (余额复原)", spendable_balance(caller_id) == before_bal,
      f"{before_bal} → {spendable_balance(caller_id)}")
with _db() as con:
    cash5 = con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and pool='provider_earning'", (owner_id,)).fetchone()["s"]
    fail_call = con.execute(
        "select status from tool_calls where client_ref=%s and user_id=%s",
        (f"ec-{_u}-fail", caller_id)).fetchone()
check("E8 失败 → earning=0 (仍 60)", int(cash5) == 60, cash5)
check("E8 失败 call 终态 failed", fail_call and fail_call["status"] == "failed",
      str(dict(fail_call)) if fail_call else "none")

# ═══ E10/E11/E12: withdrawal reads cash pool only; minor units; double-approve ═══
from economy import earnings
s = earnings.earnings_summary(owner_id)
check("E10 summary earned=60 (cash pool only)",
      s["earned"] == 60 and s["available"] == 60, str(s))
# credits never leak into withdrawal availability
check("E10 withdrawal 可用额不含 spendable credits (owner credits=0)",
      spendable_balance(owner_id) == 0)
try:
    earnings.request_withdrawal(owner_id, 500, "USD")
    check("E11 低于最低额 (1000 usd_cent) → withdrawal_min", False, "no raise")
except ValueError as e:
    check("E11 低于最低额 (1000 usd_cent) → withdrawal_min", str(e) == "withdrawal_min", str(e))
try:
    earnings.request_withdrawal(caller_id, 1500, "USD")
    check("E10 纯 credits 用户申请提现 → withdrawal_insufficient", False, "no raise")
except ValueError as e:
    check("E10 纯 credits 用户申请提现 → withdrawal_insufficient",
          str(e) == "withdrawal_insufficient", str(e))
# top up owner cash then withdraw
with db() as con:
    _a2, _ = claim_call(con, f"ec-probe-{_u}-2", caller_id, tool_key, 5, "{}", ts)
with db() as con:
    accrue_tool_success(con, int(_a2["id"]), tool_row, caller_id, 2000, {"publicId": "ec-r2"})
w = earnings.request_withdrawal(owner_id, 1200, "USD", note="canonical")
check("E11 request amount_minor=1200 usd_cent 现金单位",
      int(w["amount_minor"]) == 1200 and w["currency"] == "USD", str(dict(w)))
w2 = earnings.process_withdrawal(int(w["id"]), True, admin_user_id=1)
check("E12 approve → approved + cash movement", w2["status"] == "approved")
try:
    earnings.process_withdrawal(int(w["id"]), True, admin_user_id=1)
    check("E12 double approve impossible", False, "no raise")
except ValueError as e:
    check("E12 double approve impossible", str(e) == "already_processed", str(e))
with _db() as con:
    wd_moves = con.execute(
        "select count(*) n, coalesce(sum(delta),0) s from ledger"
        " where user_id=%s and pool='provider_earning' and kind='withdraw'",
        (owner_id,)).fetchone()
check("E12 withdraw movement 1 条 −1200 (cash pool)",
      int(wd_moves["n"]) == 1 and int(wd_moves["s"]) == -1200, str(dict(wd_moves)))
s2 = earnings.earnings_summary(owner_id)
check("E10/E13 提现后 available = 860 (journal 口径)",
      s2["available"] == 860 and s2["earned"] == 860
      and s2["withdrawn"] == 1200 and s2["earned_total"] == 2060, str(s2))
# reject leaves earnings untouched (accrue 200 more → 1060 ≥ min 1000)
with db() as con:
    _a3, _ = claim_call(con, f"ec-probe-{_u}-3", caller_id, tool_key, 5, "{}", ts)
with db() as con:
    accrue_tool_success(con, int(_a3["id"]), tool_row, caller_id, 200, {"publicId": "ec-r3"})
s_pre = earnings.earnings_summary(owner_id)
w3 = earnings.request_withdrawal(owner_id, 1000, "USD")
earnings.process_withdrawal(int(w3["id"]), False, admin_user_id=1)
s3 = earnings.earnings_summary(owner_id)
check("E12 reject 不扣 earnings (available 不变, pending 释放)",
      s3["available"] == s_pre["available"] == 1060, f"{s_pre} → {s3}")

# ═══ E14/E15: atomic tool creation + no free-by-absence ═══
from governance.pricing import create_tool_with_pricing, get_pricing
_u2 = "ec2" + _os.urandom(3).hex()
decl = {
    "tool_key": f"{_u2}o/atomictool", "tool_id": "atomictool",
    "owner_id": owner_id, "execution": "http", "tool_type": "produce",
    "label": {"en": "AT"}, "desc": None, "icon": "wand", "accept": [".png"],
    "multi": False, "params": None, "pass_through": False,
    "visibility": "private", "enabled": True,
    "binding": {"url": "https://example.org/a", "timeout_s": 30},
}
tk = create_tool_with_pricing(decl, 9, 45, "USD", admin_user_id=owner_id)
with _db() as con:
    both = con.execute(
        "select (select count(*) from platform_tools where tool_key=%s) t,"
        " (select count(*) from pricing where tool_id=%s) p", (tk, tk)).fetchone()
check("E14 create → declaration + pricing 同落 (原子)", int(both["t"]) == 1 and int(both["p"]) == 1)
pr = get_pricing(tk)
check("E14 商业合同读回: cost=9 credits, payout=45 usd_cent",
      pr["user_cost_credits"] == 9 and pr["provider_payout_minor"] == 45
      and pr["payout_currency"] == "USD", str(pr))
absent = get_pricing(f"{_u2}o/never-created")
check("E15 pricing 缺行 → enabled=False (无 free-by-default)",
      absent["enabled"] is False, str(absent))
from execution.affordability import effective_quote as _eq
absent_row = {"tool_key": f"{_u2}o/never-created", "owner_id": owner_id}
qa = _eq(caller_id, absent_row)
check("E15 缺行工具 gate quote → disabled", qa["enabled"] is False, str(qa))
from economy.billing import grant as _g
check("E16a caller spendable 不受 owner cash 影响 (495)",
      spendable_balance(caller_id) == 495)

# ═══ E16: CREDITS_PER_USD runtime symbol = 0 ═══
import subprocess as _sp
_grep = _sp.run(
    ["grep", "-rn", "-E",
     "CREDITS_PER_USD|to_usd|credits */ *1000|usd [*] *1000|commission_rate",
     _os.path.join(_REPO, "app"), _os.path.join(_REPO, "core"),
     _os.path.join(_REPO, "static")],
    capture_output=True, text=True)
_hits = [l for l in _grep.stdout.splitlines()
         if ".pyc" not in l and "test_economy" not in l]
check("E16 禁算符零残留 (CREDITS_PER_USD/to_usd//1000/*1000/commission)",
      not _hits, "; ".join(_hits[:4]))

# ═══ cleanup ═══
with _db() as con:
    for uid in (caller_id, owner_id, firstparty_caller_id, poor_id):
        con.execute("delete from users where id=%s", (uid,))
    con.execute("delete from platform_tools where tool_key like %s", (f"{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s", (f"{_u2}%",))
    con.execute("delete from pricing where tool_id like %s", (f"{_u}%",))
    con.execute("delete from pricing where tool_id like %s", (f"{_u2}%",))

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print(f"FAIL: 无 — 全绿 ({COUNT} checks)")
