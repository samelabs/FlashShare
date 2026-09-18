#!/usr/bin/env python3
"""Result settlement atomicity canonical.

No durable/public tool result without a committed settlement: the
orchestrator only STAGES the physical Gokapi object; uploads row +
tool_calls.upload_id + result binding + earning/revenue land in ONE
gate transaction. Fault injection proves rollbacks, staged cleanup,
exact refund, commit-ACK-loss reconciliation (durable ok → success, no
refund), unreadable-outcome (stays running, no refund), startup
recovery, and same-client_ref response-loss replay (single execution /
consume / uploads row / settlement).
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
_u = "rs" + _os.urandom(4).hex()

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

from economy.billing import grant as _grant, spendable_balance
from repo.ledger import by_idem_key
from execution.pipeline import StagedResult, land_tool_result

def _mkstaged(tag="x", **kw):
    d = dict(gokapi_id=f"gk_{_u}_{tag}",
             filename="out.bin", content_type="application/octet-stream",
             size=10, expiry_days=7, allowed_downloads=5,
             expire_at=ts + 86400, url_hotlink="", ip="127.0.0.1")
    d.pop("tool_id", None)   # 闭包误传容忍
    d.update(kw)
    return StagedResult(**d)

def _mktool(tool_id, owner_id=None, cost=25):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, tool_type, label_json, icon, multi, pass_through,"
            " visibility, enabled, created_at, updated_at)"
            " values (%s, %s, %s, 'local', 'produce', %s, 'wand', FALSE,"
            " FALSE, 'public', TRUE, %s, %s)",
            (tool_id, owner_id, tool_id,
             _json.dumps({"en": "RS"}), ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts))

# ══════════ A. staging-only contract ══════════
st = _mkstaged(tag="contract")
check("A1 StagedResult 无 publicId/downloadUrl/uploads id 字段",
      not hasattr(st, "public_id") and not hasattr(st, "downloadUrl")
      and not hasattr(st, "upload_id"))
_orch_src = open(_os.path.join(_REPO, "core", "execution", "orchestrator.py"),
                 encoding="utf-8").read()
check("A2 orchestrator 不再调用 save_upload_record",
      "save_upload_record" not in _orch_src)

# ══════════ B. atomic success transaction (first-party) ══════════
from execution import gate

user1 = _mkuser(_u + "u1")
tool1 = f"rstool1_{_u}"
_mktool(tool1, owner_id=None, cost=25)
_grant(user1, 1000, "manual_topup", note="rs")

from execution.invocation import ToolInvocation, SourceFile
def _inv(tool_id, ref=None):
    return ToolInvocation(tool_id=tool_id,
                          files=[SourceFile("p", "t")], params={},
                          delivery={"expiry_days": 7, "allowed_downloads": 5},
                          client_ref=ref)

async def _stage_ok(tool_id, tag="ok", **kw):
    return _mkstaged(tag=tag, **kw)

def _run_gate(user, tool_id, ref, staged_fn, tool_owner=None):
    return asyncio.new_event_loop().run_until_complete(gate.run(
        {"id": user},
        {"id": tool_id, "tool_key": tool_id, "type": "produce",
         "owner_id": tool_owner},
        _inv(tool_id, ref), staged_fn))

n_uploads_before = None
with db() as con:
    n_uploads_before = int(con.execute(
        "select count(*) c from uploads where user_id=%s",
        (user1,)).fetchone()["c"])

res, err, st1 = _run_gate(user1, tool1, None, lambda: _stage_ok(tool1))
check("B1 成功 settlement → success response", res and res.get("ok"))
with db() as con:
    row = con.execute(
        "select t.id, t.status, t.upload_id, t.result_public_id,"
        " t.result_json, u.public_id from tool_calls t"
        " left join uploads u on u.id = t.upload_id"
        " where t.user_id=%s and t.tool_id=%s order by t.id desc limit 1",
        (user1, tool1)).fetchone()
    n_uploads_after = int(con.execute(
        "select count(*) c from uploads where user_id=%s",
        (user1,)).fetchone()["c"])
check("B2 uploads 恰新增一行", n_uploads_after - n_uploads_before == 1,
      f"{n_uploads_before}→{n_uploads_after}")
check("B3 tool_call.upload_id 指向该行 + status ok",
      row["status"] == "ok" and row["upload_id"] is not None)
check("B4 result_public_id == uploads.public_id",
      row["result_public_id"] == row["public_id"])
rj = _json.loads(row["result_json"])
check("B5 result_json.publicId == uploads.public_id",
      rj.get("publicId") == row["public_id"])
# B6 详情在 B6b (绑定 call id 的 revenue 聚合)
from repo import ledger as _led
with db() as con:
    rev = con.execute(
        "select coalesce(sum(delta),0) s from ledger where kind='platform_revenue'"
        " and ref_id=%s", (str(row["id"]),)).fetchone()["s"]
check("B6b platform_revenue = 25 (绑定 call id)", int(rev) == 25, str(rev))

# ══════════ C. third-party earning/commission 同事务 ══════════
owner = _mkuser(_u + "owner")
user2 = _mkuser(_u + "u2")
tool2 = f"rstool2_{_u}"
_mktool(tool2, owner_id=owner, cost=100)
_grant(user2, 1000, "manual_topup", note="rs")
res2, err2, _ = _run_gate(user2, tool2, None, lambda: _stage_ok(tool2),
                          tool_owner=owner)
with db() as con:
    r2 = con.execute(
        "select t.id from tool_calls t where t.user_id=%s and t.tool_id=%s"
        " order by t.id desc limit 1", (user2, tool2)).fetchone()
    earn = con.execute(
        "select coalesce(sum(delta),0) s from ledger where kind='provider_earn'"
        " and ref_id=%s", (str(r2["id"]),)).fetchone()["s"]
    comm = con.execute(
        "select coalesce(sum(delta),0) s from ledger where kind='platform_commission'"
        " and ref_id=%s", (str(r2["id"]),)).fetchone()["s"]
check("C1 third-party: earning 70 + commission 30 (rate 30)",
      int(earn) == 70 and int(comm) == 30, f"earn={earn} comm={comm}")

# ══════════ D. fault injection: R1 (uploads insert 后 / finalize 前) ══════════
user3 = _mkuser(_u + "u3")
tool3 = f"rstool3_{_u}"
_mktool(tool3, owner_id=None, cost=25)
_grant(user3, 1000, "manual_topup", note="rs")
bal3 = spendable_balance(user3)

import execution.pipeline as _pipe
_orig_land = _pipe.land_tool_result
def _land_fail_after_upload(con, user_id, staged, base_url):
    uid, rec = _orig_land(con, user_id, staged, base_url)
    raise RuntimeError("R1: fault after uploads INSERT")
import execution.gate as _gate_mod
import core.execution.gate as _g2

# R1 走完整 gate 路径: patch pipeline 模块 (gate._finish 函数内 import 的
# 正是 execution.pipeline 模块属性)
def _r1_gate_run():
    _pipe.land_tool_result = _land_fail_after_upload
    try:
        return _run_gate(user3, tool3, None, lambda: _stage_ok(tool3, tag="r1"))
    finally:
        _pipe.land_tool_result = _orig_land
res3, err3, st3 = _r1_gate_run()
with db() as con:
    up = con.execute(
        "select count(*) c from uploads where user_id=%s", (user3,)).fetchone()["c"]
    t3 = con.execute(
        "select status, result_public_id, upload_id from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (user3, tool3)).fetchone()
    refund3 = con.execute(
        "select l.delta from ledger l where l.user_id=%s and l.kind='refund'"
        " order by l.id desc limit 1", (user3,)).fetchone()
check("D1 R1: uploads row=0 / call failed / 无 result binding",
      int(up) == 0 and t3["status"] == "failed"
      and t3["result_public_id"] is None and t3["upload_id"] is None,
      f"up={up} status={t3['status']}")
check("D2 R1: exact refund (+25) → 余额恢复",
      refund3 is not None and int(refund3["delta"]) == 25
      and spendable_balance(user3) == bal3,
      f"refund={refund3 and refund3['delta']} bal={spendable_balance(user3)}")

# ══════════ E. fault injection: R2 (finalize 后 / earning 写入时) ══════════
user4 = _mkuser(_u + "u4")
tool4 = f"rstool4_{_u}"
_mktool(tool4, owner_id=None, cost=25)
_grant(user4, 1000, "manual_topup", note="rs")
bal4 = spendable_balance(user4)

import repo.ledger as _rled
_orig_fok = _rled.finalize_call_ok
def _fok_then_fail(con, call_id, upload_id, rpid, rjson, fin_at):
    row = _orig_fok(con, call_id, upload_id, rpid, rjson, fin_at)
    raise RuntimeError("R2: fault after finalize, before revenue")
# gate 顶部 module 级 from repo.ledger import finalize_call_ok →
# patch execution.gate 模块属性 (测试顶部 from execution import gate 的
# 同一模块对象)
_orig_gate_fok = _gate_mod.finalize_call_ok
_gate_mod.finalize_call_ok = _fok_then_fail
try:
    res4, err4, st4 = _run_gate(user4, tool4, None, lambda: _stage_ok(tool4, tag="r2"))
except Exception:
    pass
_gate_mod.finalize_call_ok = _orig_gate_fok
with db() as con:
    up4 = con.execute(
        "select count(*) c from uploads where user_id=%s", (user4,)).fetchone()["c"]
    t4 = con.execute(
        "select status, upload_id from tool_calls where user_id=%s and"
        " tool_id=%s order by id desc limit 1", (user4, tool4)).fetchone()
check("E1 R2: 整事务回滚 (uploads=0 / call 非 ok / 无 binding)",
      int(up4) == 0 and t4["status"] == "failed" and t4["upload_id"] is None,
      f"up={up4} status={t4['status']}")
check("E2 R2: exact refund → 余额恢复",
      spendable_balance(user4) == bal4, str(spendable_balance(user4)))

# ══════════ F. commit-ACK 丢失 → durable ok 收敛 (case A) ══════════
user5 = _mkuser(_u + "u5")
tool5 = f"rstool5_{_u}"
_mktool(tool5, owner_id=None, cost=25)
_grant(user5, 1000, "manual_topup", note="rs")
bal5 = spendable_balance(user5)

_deleted = []
import gokapi as _gk
_orig_del = _gk.delete_gokapi_file
_gk.delete_gokapi_file = lambda gid: _deleted.append(gid)

# 制造: settlement COMMIT 成功但 _finish 在 commit 后抛 (模拟 ACK 丢失):
_orig_spend = None
import economy.billing as _bill
class _CommitAckLoss(Exception):
    pass
# 手法: monkeypatch db 上下文? 更简单: 跑一次正常成功, 然后手动把 call 拨回
# running 前状态再重放? 不行。真实手法: patch spendable_balance (commit 后
# 调用) 抛异常 — _finish 的 balance 段在事务外? 看代码: balance 在 with 外。
# 那么: patch json.dumps? 不行。直接模拟: 正常跑完后, 人为把 _finish 包上
# commit 后异常 — 用一个 wrapper: 跑 gate.run 两次不行。
# 采用: patch _g2.finalize_call_ok 正常执行 + 让 record_platform_income 抛
# → 这是 R2 (rollback)。ACK 丢失场景需要 commit 后异常 — patch
# spendable_balance 抛 (balance 段在 commit 后, 是 response 构造):
# ACK-loss: settlement 真实 commit 成功, 但 db() 上下文在 commit 后抛
# (连接断/ACK 丢失) — patch gate 模块的 db 工厂, 返回的连接 commit()
# 先真提交再抛。
_orig_finish = _gate_mod._finish
def _finish_ack_loss(user, tool, call_id, cost, staged):
    # 真实执行 settlement (完整 commit), 然后模拟连接在 ACK 返回前断掉:
    # 客户端视角 _finish 抛异常, 但 durable fact 已提交。
    _orig_finish(user, tool, call_id, cost, staged)
    raise RuntimeError("ACK loss: connection dropped after commit")

_gate_mod._finish = _finish_ack_loss
try:
    res5, err5, st5 = _run_gate(user5, tool5, None, lambda: _stage_ok(tool5, tag="ack"))
except Exception:
    res5, err5, st5 = None, None, None
_gate_mod._finish = _orig_finish
with db() as con:
    t5 = con.execute(
        "select status, result_json, upload_id from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (user5, tool5)).fetchone()
    n_refund5 = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'",
        (user5,)).fetchone()["c"])
    n_earn5 = int(con.execute(
        "select count(*) c from ledger where kind='platform_revenue'"
        " and ref_id=(select max(id)::text from tool_calls where user_id=%s)",
        (user5,)).fetchone()["c"])
check("F1 commit-ACK 丢失 → durable ok 被识别, 返回 durable success",
      t5["status"] == "ok" and res5 is not None and res5.get("ok")
      and res5.get("replay") is True,
      f"status={t5['status']} res={res5 and dict(res5)}")
check("F2 durable ok → 零 refund (无双重退款)",
      n_refund5 == 0, str(n_refund5))
check("F3 durable ok → 零 staged delete", not _deleted, str(_deleted))
check("F4 durable ok → 无重复 earning (恰 1)", n_earn5 == 1, str(n_earn5))
durable_rj = _json.loads(t5["result_json"])
check("F5 返回的是 durable result_json (同 publicId)",
      res5["result"]["publicId"] == durable_rj["publicId"])
_gk.delete_gokapi_file = _orig_del

# ══════════ G. outcome DB 不可读 → unresolved (case C) ══════════
user6 = _mkuser(_u + "u6")
tool6 = f"rstool6_{_u}"
_mktool(tool6, owner_id=None, cost=25)
_grant(user6, 1000, "manual_topup", note="rs")
bal6 = spendable_balance(user6)

_orig_rso = _gate_mod.resolve_success_outcome
def _rso_unreachable(call_id):
    return None          # DB outcome unreadable
_gate_mod.resolve_success_outcome = _rso_unreachable
_gk_deleted6 = []
_gk.delete_gokapi_file = lambda gid: _gk_deleted6.append(gid)
# 让 _finish 本身失败 (R1 fault) + outcome 不可读 → unresolved
_pipe.land_tool_result = _land_fail_after_upload
try:
    res6, err6, st6 = _run_gate(user6, tool6, None, lambda: _stage_ok(tool6, tag="g"))
except Exception:
    res6, err6, st6 = None, None, None
finally:
    _pipe.land_tool_result = _orig_land
    _gate_mod.resolve_success_outcome = _orig_rso
with db() as con:
    t6 = con.execute(
        "select status from tool_calls where user_id=%s and tool_id=%s"
        " order by id desc limit 1", (user6, tool6)).fetchone()
check("G1 outcome 不可读 → err_metering_failed",
      err6 is not None and err6.get("error") == "err_metering_failed",
      str(err6))
check("G2 call 保持 running (未裁决)", t6["status"] == "running", t6["status"])
check("G3 零 refund", spendable_balance(user6) == bal6 - 25,
      f"{spendable_balance(user6)} vs {bal6-25} (charge 保留)")
check("G4 零 staged delete", not _gk_deleted6, str(_gk_deleted6))

# ══════════ H. known-rollback → staged cleanup attempted ══════════
user7 = _mkuser(_u + "u7")
tool7 = f"rstool7_{_u}"
_mktool(tool7, owner_id=None, cost=25)
_grant(user7, 1000, "manual_topup", note="rs")
bal7 = spendable_balance(user7)
_staged7 = _mkstaged(tag="cleanup7")
_gk_deleted7 = []
_gk.delete_gokapi_file = lambda gid: _gk_deleted7.append(gid)
async def _stage7():
    return _staged7
_pipe.land_tool_result = _land_fail_after_upload
try:
    res7, err7, st7 = _run_gate(user7, tool7, None, _stage7)
except Exception:
    pass
finally:
    _pipe.land_tool_result = _orig_land
check("H1 known rollback → staged Gokapi cleanup 被调用",
      _staged7.gokapi_id in _gk_deleted7, str(_gk_deleted7))
check("H2 known rollback → exact refund + terminal failed",
      spendable_balance(user7) == bal7)
with db() as con:
    t7 = con.execute(
        "select status from tool_calls where user_id=%s and tool_id=%s"
        " order by id desc limit 1", (user7, tool7)).fetchone()
    up7 = con.execute(
        "select count(*) c from uploads where user_id=%s",
        (user7,)).fetchone()["c"]
check("H3 零 public result (uploads=0 / failed)",
      t7["status"] == "failed" and int(up7) == 0)
_gk.delete_gokapi_file = _orig_del

# ══════════ I. startup recovery: running+charge 无 result → refund+failed ══════════
user8 = _mkuser(_u + "u8")
tool8 = f"rstool8_{_u}"
_mktool(tool8, owner_id=None, cost=25)
_grant(user8, 1000, "manual_topup", note="rs")
with db() as con:
    from repo.ledger import claim_call
    anchor8, _ = claim_call(con, f"startup_{_u}", user8, tool8, 25, "{}", ts)
call8 = int(anchor8["id"])
from economy.billing import consume as _consume
_charge8 = _consume(user8, 25, "consume", ref_type="tool_call",
                    ref_id=str(call8), idem_key=f"call:{call8}",
                    note="startup fixture")
bal8_pre = spendable_balance(user8)
out8 = gate.recover_abandoned_calls()
with db() as con:
    t8 = con.execute(
        "select status, upload_id, result_public_id from tool_calls"
        " where id=%s", (call8,)).fetchone()
check("I1 startup recovery: running+charge → exact refund+failed",
      t8["status"] == "failed" and t8["upload_id"] is None
      and t8["result_public_id"] is None
      and spendable_balance(user8) == bal8_pre + 25,
      f"status={t8['status']} bal={spendable_balance(user8)}/{bal8_pre+25}")
check("I2 recovery case 无 result mapping (atomicity 保证)",
      t8["upload_id"] is None and t8["result_public_id"] is None)

# ══════════ J. same client_ref response-loss replay ══════════
user9 = _mkuser(_u + "u9")
tool9 = f"rstool9_{_u}"
_mktool(tool9, owner_id=None, cost=25)
_grant(user9, 1000, "manual_topup", note="rs")
ref9 = f"replay_{_u}"
_exec_count = {"n": 0}
async def _stage9():
    _exec_count["n"] += 1
    return _mkstaged(tag="replay9")
resA, errA, stA = _run_gate(user9, tool9, ref9, _stage9)
# 模拟 response loss: 客户端重试同一 client_ref
resB, errB, stB = _run_gate(user9, tool9, ref9, _stage9)
with db() as con:
    n_calls9 = int(con.execute(
        "select count(*) c from tool_calls where user_id=%s and tool_id=%s"
        " and client_ref=%s", (user9, tool9, ref9)).fetchone()["c"])
    n_uploads9 = int(con.execute(
        "select count(*) c from uploads where user_id=%s",
        (user9,)).fetchone()["c"])
    n_consume9 = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='consume'"
        " and idem_key like 'call:%%'", (user9,)).fetchone()["c"])
    n_rev9 = int(con.execute(
        "select count(*) c from ledger where kind='platform_revenue'"
        " and ref_id=(select max(id)::text from tool_calls where user_id=%s)",
        (user9,)).fetchone()["c"])
check("J1 replay → replay=True / 同 call_id / 同 publicId",
      resB.get("replay") is True and resB["call_id"] == resA["call_id"]
      and resB["result"]["publicId"] == resA["result"]["publicId"],
      f"A={resA['call_id']} B={resB['call_id']}")
check("J2 handler/stage 只执行一次", _exec_count["n"] == 1, str(_exec_count))
check("J3 恰一行 tool_call / 一行 uploads / 一次 consume / 一次 settlement",
      n_calls9 == 1 and n_uploads9 == 1 and n_consume9 == 1 and n_rev9 == 1,
      f"calls={n_calls9} up={n_uploads9} consume={n_consume9} rev={n_rev9}")

# ══════════ K. non-ok result DB constraint ══════════
def _db_rejects(sql, params=None):
    try:
        with db() as con:
            con.execute(sql, params or ())
        return False
    except Exception:
        return True
check("K1 non-ok + result_public_id → DB reject",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, started_at, finished_at,"
                  " result_public_id) values (%s,%s,%s,'failed',0,%s,%s,'x')",
                  (f"kr1_{_u}", user9, tool9, ts, ts)))
check("K2 non-ok + upload_id → DB reject",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, started_at, finished_at,"
                  " upload_id) values (%s,%s,%s,'failed',0,%s,%s,"
                  " (select min(id) from uploads))",
                  (f"kr2_{_u}", user9, tool9, ts, ts)))
check("K3 ok + upload_id NULL 合法 (未来非文件成功不设限)",
      not _db_rejects("insert into tool_calls (client_ref, user_id,"
                      " tool_id, status, cost_snapshot, started_at,"
                      " finished_at) values (%s,%s,%s,'ok',0,%s,%s)",
                      (f"kr3_{_u}", user9, tool9, ts, ts)))

# ══════════ L. direct share save_upload_record 不变 ══════════
from execution.pipeline import save_upload_record
rec_direct = save_upload_record(user9, "127.0.0.1", f"gk_direct_{_u}",
                                "share.txt", "text/plain", 5, 7, 10,
                                ts + 86400, "", BASE_URL)
check("L1 direct share save_upload_record 行为不变 (publicId/downloadUrl)",
      rec_direct.get("publicId") and rec_direct.get("downloadUrl"),
      str(rec_direct)[:80])

# ══════════ cleanup (FK-safe, prefix-scoped) ══════════
_uids = [user1, owner, user2, user3, user4, user5, user6, user7, user8, user9]
with db() as con:
    for uid in _uids:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
with db() as con:
    for uid in _uids:
        con.execute("delete from tool_calls where user_id=%s", (uid,))
        con.execute("delete from uploads where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
with db() as con:
    con.execute("delete from ledger where user_id in (%s)" % ",".join(str(u) for u in _uids))
    con.execute("delete from pricing where tool_id like %s", (f"rstool%_{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"rstool%_{_u}%",))
for uid in _uids:
    with db() as con:
        con.execute("delete from sessions where user_id=%s", (uid,))
        con.execute("delete from api_tokens where user_id=%s", (uid,))
        con.execute("delete from users where id=%s", (uid,))
print("cleanup: settlement fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
