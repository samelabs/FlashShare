#!/usr/bin/env python3
"""Commission effective-time canonical — claim-time snapshot authority.

Real gate.run / claim_call / settlement / ledger / settings. The
commission rate is resolved and frozen in the SAME transaction as the
claim INSERT; settlement reads only the persisted snapshot — config
changes affect future claims, never an in-flight attempt, a replay, or
a durable-ok reconciliation.
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

import asyncio
import json as _json
import time
ts = int(time.time())
_u = "ct" + _os.urandom(4).hex()

from database import db
from config import BASE_URL

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

def _set_rate(v):
    with db() as con:
        con.execute(
            "insert into settings (key, value, updated_at) values"
            " ('commission_rate', %s, %s) on conflict (key) do update set"
            " value = excluded.value, updated_at = excluded.updated_at",
            (str(v), ts))

def _get_rate():
    from governance.commission import commission_rate
    return commission_rate()

from execution.pipeline import StagedResult

def _mkstaged(tag):
    return StagedResult(gokapi_id=f"gk_{_u}_{tag}", filename="o.bin",
                        content_type="application/octet-stream", size=10,
                        expiry_days=7, allowed_downloads=5,
                        expire_at=ts + 86400, url_hotlink="", ip="127.0.0.1")

def _mktool(tool_id, owner_id=None, cost=100):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, tool_type, label_json, icon, multi, pass_through,"
            " visibility, enabled, created_at, updated_at)"
            " values (%s, %s, %s, 'local', 'produce', %s, 'wand', FALSE,"
            " FALSE, 'public', TRUE, %s, %s)",
            (tool_id, owner_id, tool_id, _json.dumps({"en": "CT"}), ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts))

from execution.invocation import ToolInvocation, SourceFile

def _inv(tool_id, ref=None):
    return ToolInvocation(tool_id=tool_id, files=[SourceFile("p", "t")],
                          params={},
                          delivery={"expiry_days": 7, "allowed_downloads": 5},
                          client_ref=ref)

from execution import gate

def _run(user, tool_id, ref, staged_fn, owner=None):
    return asyncio.new_event_loop().run_until_complete(gate.run(
        {"id": user},
        {"id": tool_id, "tool_key": tool_id, "type": "produce",
         "owner_id": owner},
        _inv(tool_id, ref), staged_fn))

from economy.billing import grant as _grant, spendable_balance

def _split_for(call_id):
    with db() as con:
        e = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='provider_earn' and ref_id=%s", (str(call_id),)).fetchone()
        c = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='platform_commission' and ref_id=%s",
            (str(call_id),)).fetchone()
        r = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='platform_revenue' and ref_id=%s",
            (str(call_id),)).fetchone()
    return int(e["s"]), int(c["s"]), int(r["s"])

def _call_row(call_id):
    with db() as con:
        return dict(con.execute(
            "select * from tool_calls where id=%s", (call_id,)).fetchone())

# ══════════ C1: claim snapshot persisted ══════════
_set_rate(30)
owner = _mkuser(_u + "owner")
u1 = _mkuser(_u + "u1")
t1 = f"cttool1_{_u}"
_mktool(t1, owner_id=owner, cost=100)
_grant(u1, 10000, "manual_topup", note="ct")

async def _s1():
    return _mkstaged("c1")
res1, err1, _ = _run(u1, t1, None, _s1, owner=owner)
row1 = _call_row(res1["call_id"])
check("C1 claim@30 → row.commission_rate_snapshot = 30",
      res1.get("ok") and row1["commission_rate_snapshot"] == 30,
      str(row1.get("commission_rate_snapshot")))

# ══════════ C2: running call 30→50 config change 不影响 split ══════════
u2 = _mkuser(_u + "u2")
t2 = f"cttool2_{_u}"
_mktool(t2, owner_id=owner, cost=100)
_grant(u2, 10000, "manual_topup", note="ct")

_state = {}

async def _s2():
    # orchestrate 期间改配置 — 模拟 claim 后 operator 改 rate
    _set_rate(50)
    return _mkstaged("c2")
res2, err2, _ = _run(u2, t2, None, _s2, owner=owner)
row2 = _call_row(res2["call_id"])
e2, c2, r2 = _split_for(res2["call_id"])
check("C2 claim@30 + 执行中改 50 → snapshot 仍 30 / earn 70 / commission 30",
      res2.get("ok") and row2["commission_rate_snapshot"] == 30
      and e2 == 70 and c2 == 30,
      f"snap={row2['commission_rate_snapshot']} e={e2} c={c2}")
check("C2b current settings = 50 (仅影响未来 claim)",
      _get_rate() == 50, str(_get_rate()))

# ══════════ C3: next new claim sees 50 ══════════
u3 = _mkuser(_u + "u3")
t3 = f"cttool3_{_u}"
_mktool(t3, owner_id=owner, cost=100)
_grant(u3, 10000, "manual_topup", note="ct")
async def _s3():
    return _mkstaged("c3")
res3, _, _ = _run(u3, t3, None, _s3, owner=owner)
row3 = _call_row(res3["call_id"])
e3, c3, _ = _split_for(res3["call_id"])
check("C3 新 claim → snapshot 50 / earn 50 / commission 50",
      row3["commission_rate_snapshot"] == 50 and e3 == 50 and c3 == 50,
      f"snap={row3['commission_rate_snapshot']} e={e3} c={c3}")

# ══════════ C4: 历史行 snapshot 不可变 ══════════
_set_rate(80)
row2b = _call_row(res2["call_id"])
check("C4 settings→80 后旧 call snapshot 仍 30 (无 writer 改历史)",
      row2b["commission_rate_snapshot"] == 30,
      str(row2b["commission_rate_snapshot"]))

# ══════════ C5: replay 忽略新 rate ══════════
ref5 = f"replay_{_u}"
u5 = _mkuser(_u + "u5")
t5 = f"cttool5_{_u}"
_mktool(t5, owner_id=owner, cost=100)
_grant(u5, 10000, "manual_topup", note="ct")
_set_rate(30)
async def _s5a():
    return _mkstaged("c5")
res5a, _, _ = _run(u5, t5, ref5, _s5a, owner=owner)
call5a = res5a["call_id"]
pid5a = res5a["result"]["publicId"]
_set_rate(80)
bal5_pre = spendable_balance(u5)
n_ledger_pre = None
with db() as con:
    n_ledger_pre = int(con.execute(
        "select count(*) c from ledger where user_id=%s", (u5,)).fetchone()["c"])
async def _s5b():
    return _mkstaged("c5")
res5b, _, _ = _run(u5, t5, ref5, _s5b, owner=owner)
with db() as con:
    n_ledger_post = int(con.execute(
        "select count(*) c from ledger where user_id=%s", (u5,)).fetchone()["c"])
row5 = _call_row(call5a)
e5, c5, _ = _split_for(call5a)
check("C5 replay: 同 call_id / 同 publicId / snapshot 仍 30 / 零新经济事实",
      res5b.get("replay") is True and res5b["call_id"] == call5a
      and res5b["result"]["publicId"] == pid5a
      and row5["commission_rate_snapshot"] == 30
      and n_ledger_post == n_ledger_pre
      and spendable_balance(u5) == bal5_pre,
      f"replay={res5b.get('replay')} snap={row5['commission_rate_snapshot']}")

# ══════════ C6: failed retry takes fresh snapshot ══════════
ref6 = f"retry_{_u}"
u6 = _mkuser(_u + "u6")
t6 = f"cttool6_{_u}"
_mktool(t6, owner_id=owner, cost=100)
_grant(u6, 10000, "manual_topup", note="ct")
_set_rate(30)

async def _fail6():
    from execution import ExecutionError
    raise ExecutionError("err_tool_failed")
res6a, err6a, _ = _run(u6, t6, ref6, _fail6, owner=owner)
row6a = None
with db() as con:
    row6a = dict(con.execute(
        "select id, status, commission_rate_snapshot from tool_calls"
        " where user_id=%s and tool_id=%s and client_ref=%s"
        " order by id asc", (u6, t6, ref6)).fetchone())
bal6_mid = spendable_balance(u6)   # 10000 (已退)
_set_rate(50)
async def _s6b():
    return _mkstaged("c6")
res6b, _, _ = _run(u6, t6, ref6, _s6b, owner=owner)
row6b = None
with db() as con:
    rows6 = con.execute(
        "select id, status, commission_rate_snapshot from tool_calls"
        " where user_id=%s and tool_id=%s and client_ref=%s order by id",
        (u6, t6, ref6)).fetchall()
    row6b = dict(rows6[-1])
e6, c6, _ = _split_for(row6b["id"])
check("C6 两行 attempt: a1 failed@30 + a2 ok@50",
      len(rows6) == 2 and rows6[0]["status"] == "failed"
      and rows6[0]["commission_rate_snapshot"] == 30
      and row6b["status"] == "ok"
      and row6b["commission_rate_snapshot"] == 50,
      f"n={len(rows6)} r0={dict(rows6[0]) if rows6 else None}")
check("C6b a2 分账按 50 (earn 50 / commission 50)",
      e6 == 50 and c6 == 50, f"e={e6} c={c6}")

# ══════════ C7: first-party 不消费 snapshot ══════════
for rate in (0, 30, 100):
    _set_rate(rate)
    uf = _mkuser(_u + f"fp{rate}")
    tf = f"ctfp{rate}_{_u}"
    _mktool(tf, owner_id=None, cost=100)
    _grant(uf, 10000, "manual_topup", note="ct")
    async def _sf():
        return _mkstaged(f"fp{rate}")
    resf, errf, _ = _run(uf, tf, None, _sf)
    ef, cf, rf = _split_for(resf["call_id"])
    check(f"C7 first-party rate={rate} → revenue 100 / earn 0 / commission 0",
          resf.get("ok") and rf == 100 and ef == 0 and cf == 0,
          f"r={rf} e={ef} c={cf}")

# ══════════ C8: self-owned free policy ════════
_set_rate(30)
us = _mkuser(_u + "self")
ts_ = f"ctself_{_u}"
_mktool(ts_, owner_id=us, cost=100)      # self-owned
_grant(us, 1000, "manual_topup", note="ct")
bal_s_pre = spendable_balance(us)
async def _ss():
    return _mkstaged("self")
ress, _, _ = _run(us, ts_, None, _ss, owner=us)
es, cs, rs_ = _split_for(ress["call_id"])
check("C8 self-owned: cost=0 / 零经济腿 / 余额不变",
      ress.get("ok") and spendable_balance(us) == bal_s_pre
      and es == 0 and cs == 0 and rs_ == 0,
      f"bal={spendable_balance(us)}/{bal_s_pre}")

# ══════════ C9-C11: DB invariants ══════════
def _db_rejects(sql, params=None):
    try:
        with db() as con:
            con.execute(sql, params or ())
        return False
    except Exception:
        return True

u9 = _mkuser(_u + "db")
t9 = f"ctdb_{_u}"
_mktool(t9, owner_id=None, cost=10)
check("C9 -1 / 101 reject, 0 / 100 合法",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, commission_rate_snapshot,"
                  " started_at) values (%s,%s,%s,'running',0,-1,%s)",
                  (f"r1_{_u}", u9, t9, ts))
      and _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                      " status, cost_snapshot, commission_rate_snapshot,"
                      " started_at) values (%s,%s,%s,'running',0,101,%s)",
                      (f"r2_{_u}", u9, t9, ts))
      and not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at)"
          " values (%s,%s,%s,'running',0,0,%s)", (f"r3_{_u}", u9, t9, ts))
      and not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at)"
          " values (%s,%s,%s,'running',0,100,%s)", (f"r4_{_u}", u9, t9, ts)))
check("C10 running + NULL snapshot → reject",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, started_at)"
                  " values (%s,%s,%s,'running',0,%s)", (f"r5_{_u}", u9, t9, ts)))
check("C11 historical terminal (failed) + NULL snapshot 合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at, finished_at)"
          " values (%s,%s,%s,'failed',0,%s,%s)",
          (f"r6_{_u}", u9, t9, ts, ts)))

# ══════════ C12: fallback 冻结 resolved 值 ══════════
with db() as con:
    con.execute("delete from settings where key='commission_rate'")
ufb = _mkuser(_u + "fb")
tfb = f"ctfb_{_u}"
_mktool(tfb, owner_id=owner, cost=100)
_grant(ufb, 10000, "manual_topup", note="ct")
from governance.commission import commission_rate
check("C12a missing settings → resolved 30", commission_rate() == 30)
async def _sfb():
    return _mkstaged("fb")
resfb, _, _ = _run(ufb, tfb, None, _sfb, owner=owner)
rowfb = _call_row(resfb["call_id"])
check("C12b missing → snapshot 冻结 30",
      rowfb["commission_rate_snapshot"] == 30,
      str(rowfb["commission_rate_snapshot"]))
with db() as con:
    con.execute("insert into settings (key, value, updated_at) values"
                " ('commission_rate', 'garbage', %s)", (ts,))
ufg = _mkuser(_u + "fg")
tfg = f"ctfg_{_u}"
_mktool(tfg, owner_id=owner, cost=100)
_grant(ufg, 10000, "manual_topup", note="ct")
async def _sfg():
    return _mkstaged("fg")
resfg, _, _ = _run(ufg, tfg, None, _sfg, owner=owner)
rowfg = _call_row(resfg["call_id"])
check("C12c invalid value → fallback 30 冻结",
      rowfg["commission_rate_snapshot"] == 30,
      str(rowfg["commission_rate_snapshot"]))

# semantic: _finish body must not read commission_rate()
_gate_src = open(_os.path.join(_REPO, "core", "execution", "gate.py"),
                 encoding="utf-8").read()
_finish_body = _gate_src.split("def _finish")[1].split("\ndef ")[0]
check("S1 _finish 函数体零 commission_rate() 读取",
      "commission_rate()" not in _finish_body.replace(
          "commission_rate_snapshot", ""))

# ══════════ cleanup ══════════
_set_rate(30)
_uids = [owner, u1, u2, u3, u5, u6, us, u9, ufb, ufg]
with db() as con:
    fp_ids = [r["id"] for r in con.execute(
        "select id from users where username like %s", (f"{_u}fp%",)).fetchall()]
_uids = _uids + fp_ids
with db() as con:
    for uid in _uids:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
with db() as con:
    for uid in _uids:
        con.execute("delete from tool_calls where user_id=%s", (uid,))
        con.execute("delete from uploads where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
with db() as con:
    con.execute("delete from pricing where tool_id like %s", (f"ct%_{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"ct%_{_u}%",))
for uid in _uids:
    with db() as con:
        con.execute("delete from sessions where user_id=%s", (uid,))
        con.execute("delete from api_tokens where user_id=%s", (uid,))
        con.execute("delete from users where id=%s", (uid,))
print("cleanup: commission fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
