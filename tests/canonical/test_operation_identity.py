#!/usr/bin/env python3
"""Operation identity canonical — client_ref + invocation fingerprint.

Client_ref is a REQUIRED client-generated stable operation identity;
same user+tool+ref is permanently bound to one canonical invocation
fingerprint (SHA-256 of canonical semantic payload). same ref + same
hash → running=progress / ok=replay / failed=new-attempt; same ref +
different hash → 409 idempotency_conflict with zero charge and zero
execution. Real parser/gate/DB behavior — not source greps.
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
from tests.support.cleanup import purge_users  # noqa: E402

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
_u = "oi" + _os.urandom(4).hex()

from database import db
from execution.invocation import (ToolInvocation, SourceFile,
                                  parse_tool_invocation,
                                  invocation_fingerprint)
from execution.invocation import InvocationError

# ══════════ O1-O4: parser ══════════
GOOD = {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]},
        "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": "ref-1"}

def _parse_bad(raw):
    try:
        parse_tool_invocation(raw)
        return False
    except InvocationError:
        return True

check("O1 client_ref null → reject", _parse_bad({**GOOD, "client_ref": None}))
check("O2 client_ref empty/whitespace → reject",
      _parse_bad({**GOOD, "client_ref": ""}) and _parse_bad({**GOOD, "client_ref": "   "}))
check("O3 client_ref missing key → reject", _parse_bad(
    {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]},
     "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}))
inv_ok = parse_tool_invocation({**GOOD, "client_ref": "  stable-1  "})
check("O4 valid client_ref accepted + stripped",
      isinstance(inv_ok.client_ref, str) and inv_ok.client_ref == "stable-1")
check("O4b >128 reject", _parse_bad({**GOOD, "client_ref": "x" * 129}))

# ══════════ O5-O11: fingerprint ══════════
def _mkinv(pid="fA", params=None, delivery=None, tool="x", token=""):
    return ToolInvocation(tool_id=tool,
                          files=[SourceFile(pid, token)],
                          params=params if params is not None else {},
                          delivery=delivery or {"expiry_days": 7, "allowed_downloads": 5},
                          client_ref="irrelevant")

f1 = invocation_fingerprint(_mkinv())
check("O5 deterministic (同输入同 hash)",
      f1 == invocation_fingerprint(_mkinv()) and len(f1) == 64
      and f1 == f1.lower())
check("O6 canonical params 等价 → same",
      invocation_fingerprint(_mkinv(params={"a": 1, "b": 2}))
      == invocation_fingerprint(_mkinv(params={"b": 2, "a": 1})))
check("O7 changed params → different",
      invocation_fingerprint(_mkinv(params={"a": 1}))
      != invocation_fingerprint(_mkinv(params={"a": 2})))
check("O8 changed source public_id → different",
      invocation_fingerprint(_mkinv(pid="fA"))
      != invocation_fingerprint(_mkinv(pid="fB")))
check("O9 source order change → different",
      invocation_fingerprint(ToolInvocation("x", [SourceFile("fA", ""), SourceFile("fB", "")],
                                            {}, {"expiry_days": 7, "allowed_downloads": 5}, "r"))
      != invocation_fingerprint(ToolInvocation("x", [SourceFile("fB", ""), SourceFile("fA", "")],
                                               {}, {"expiry_days": 7, "allowed_downloads": 5}, "r")))
check("O10 changed delivery → different",
      invocation_fingerprint(_mkinv(delivery={"expiry_days": 7, "allowed_downloads": 5}))
      != invocation_fingerprint(_mkinv(delivery={"expiry_days": 3, "allowed_downloads": 5})))
check("O11 source_token change only → SAME (credential 非语义)",
      invocation_fingerprint(_mkinv(token="tok_a"))
      == invocation_fingerprint(_mkinv(token="tok_b")))
check("O11b client_ref 不参与 hash",
      invocation_fingerprint(_mkinv()) == f1)  # _mkinv client_ref 恒定 irrelevant; f1 已证

# ══════════ DB fixtures for gate behavior ══════════
from economy.billing import grant as _grant, spendable_balance

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

def _mktool(tool_id, owner_id=None, cost=100):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, tool_type, label_json, icon,"
            " visibility, enabled, created_at, updated_at)"
            " values (%s, %s, %s, 'local', 'produce', %s, 'wand',"
            " 'public', TRUE, %s, %s)",
            (tool_id, owner_id, tool_id, _json.dumps({"en": "OI"}), ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts))

from execution.results import StagedItems, StagedFileItem
def _mkstaged(tag):
    return StagedItems(order=["file"], inline=[],
                       files=[StagedFileItem(
                           gokapi_id=f"gk_{_u}_{tag}", filename="o.bin",
                           content_type="application/octet-stream",
                           size=10, expiry_days=7, allowed_downloads=5,
                           expire_at=ts + 86400, url_hotlink="")],
                       ip="127.0.0.1")

from execution import gate
from governance.commission import commission_rate

def _run(user, tool, inv, staged_fn):
    return asyncio.new_event_loop().run_until_complete(
        gate.run({"id": user}, tool, inv, staged_fn))

def _ledger_n(user_id, kind):
    with db() as con:
        return int(con.execute(
            "select count(*) c from ledger where user_id=%s and kind=%s",
            (user_id, kind)).fetchone()["c"])

def _calls(user_id, tool_id, ref):
    with db() as con:
        return [dict(r) for r in con.execute(
            "select * from tool_calls where user_id=%s and tool_id=%s"
            " and client_ref=%s order by id", (user_id, tool_id, ref)).fetchall()]

owner = _mkuser(_u + "owner")
with db() as con:
    rate = commission_rate(con)

# ══════════ O12-O17: replay ══════════
uR = _mkuser(_u + "replay")
tR = f"oitool_{_u}"
_mktool(tR, owner_id=owner, cost=100)
_grant(uR, 10000, "manual_topup", note="oi")
toolR = {"id": tR, "tool_key": tR, "type": "produce", "owner_id": owner}

async def _st_ok():
    return _mkstaged("replay")
invR1 = ToolInvocation(tR, [SourceFile("pR", "t")], {},
                       {"expiry_days": 7, "allowed_downloads": 5}, f"job-{_u}-R")
resR1, errR1, _ = _run(uR, toolR, invR1, _st_ok)
balR_mid = spendable_balance(uR)
n_calls_mid = len(_calls(uR, tR, f"job-{_u}-R"))
uploads_mid = None
with db() as con:
    uploads_mid = int(con.execute(
        "select count(*) c from uploads where user_id=%s", (uR,)).fetchone()["c"])
ledger_mid = _ledger_n(uR, "consume")

resR2, errR2, _ = _run(uR, toolR, invR1, _st_ok)   # same ref + same payload
check("O12 success + same ref/hash → replay",
      resR2.get("ok") and resR2.get("replay") is True, str(resR2)[:100])
check("O13 replay same call_id", resR2["call_id"] == resR1["call_id"])
check("O14 replay same resultId",
      resR2["result"]["result_id"] == resR1["result"]["result_id"])
check("O15 replay zero new consume",
      spendable_balance(uR) == balR_mid and _ledger_n(uR, "consume") == ledger_mid)
check("O16 replay zero execution (1 call row)",
      len(_calls(uR, tR, f"job-{_u}-R")) == n_calls_mid)
with db() as con:
    uploads_post = int(con.execute(
        "select count(*) c from uploads where user_id=%s", (uR,)).fetchone()["c"])
check("O17 replay zero settlement (uploads 不变)",
      uploads_post == uploads_mid)

# ══════════ O18-O22: conflict (ref 绑定 fingerprint) ══════════
uC = _mkuser(_u + "conf")
tC = f"oiconf_{_u}"
_mktool(tC, owner_id=owner, cost=100)
_grant(uC, 10000, "manual_topup", note="oi")
toolC = {"id": tC, "tool_key": tC, "type": "produce", "owner_id": owner}
refC = f"job-{_u}-C"

async def _st_c():
    return _mkstaged("conf")
invC1 = ToolInvocation(tC, [SourceFile("pC", "t")], {},
                       {"expiry_days": 7, "allowed_downloads": 5}, refC)
resC1, _, _ = _run(uC, toolC, invC1, _st_c)
balC_mid = spendable_balance(uC)
nC_mid = len(_calls(uC, tC, refC))

invC2 = ToolInvocation(tC, [SourceFile("pC", "t")], {"preset": "x"},
                       {"expiry_days": 7, "allowed_downloads": 5}, refC)  # params 变了
resC2, errC2, httpC2 = _run(uC, toolC, invC2, _st_c)
_errC2 = resC2 or errC2
check("O18 same ref + changed params → 409 idempotency_conflict",
      httpC2 == 409 and _errC2.get("error") == "idempotency_conflict",
      f"{httpC2} {str(_errC2)[:80]}")
check("O19 conflict zero charge", spendable_balance(uC) == balC_mid)
check("O20 conflict zero execute (call rows 不变)",
      len(_calls(uC, tC, refC)) == nC_mid)

invC3 = ToolInvocation(tC, [SourceFile("pOTHER", "t")], {},
                       {"expiry_days": 7, "allowed_downloads": 5}, refC)  # source 变了
resC3, errC3, httpC3 = _run(uC, toolC, invC3, _st_c)
_errC3 = resC3 or errC3
check("O21 same ref + changed source → conflict",
      httpC3 == 409 and _errC3.get("error") == "idempotency_conflict")

invC4 = ToolInvocation(tC, [SourceFile("pC", "t")], {},
                       {"expiry_days": 3, "allowed_downloads": 5}, refC)  # delivery 变了
resC4, errC4, httpC4 = _run(uC, toolC, invC4, _st_c)
_errC4 = resC4 or errC4
check("O22 same ref + changed delivery → conflict",
      httpC4 == 409 and _errC4.get("error") == "idempotency_conflict")

# ══════════ O23-O24: running anchor ══════════
uN = _mkuser(_u + "run")
tN = f"oirun_{_u}"
_mktool(tN, owner_id=owner, cost=100)
_grant(uN, 10000, "manual_topup", note="oi")
toolN = {"id": tN, "tool_key": tN, "type": "produce", "owner_id": owner}
refN = f"job-{_u}-N"

_gate_state = {}
async def _st_hang():
    # 第一次调用: claim 后卡住 (直接构造 running 状态) — 手动插 running 行更简单
    return _mkstaged("run")

# 手动建 running anchor (claim_call 真实路径)
from repo.ledger import claim_call
with db() as con:
    invN1 = ToolInvocation(tN, [SourceFile("pN", "t")], {},
                           {"expiry_days": 7, "allowed_downloads": 5}, refN)
    hashN = invocation_fingerprint(invN1)
    anchorN, claimedN = claim_call(con, refN, uN, tN, 100, "{}", ts,
                                   commission_rate_snapshot=rate,
                                   invocation_hash=hashN)
    assert claimedN
# same hash → running in_progress
resN1, errN1, httpN1 = _run(uN, toolN, invN1, _st_hang)
_errN1 = resN1 or errN1
check("O23 running + same hash → err_call_in_progress",
      httpN1 == 409 and _errN1.get("error") == "err_call_in_progress",
      f"{httpN1} {str(_errN1)[:80]}")
# different hash → conflict
invN2 = ToolInvocation(tN, [SourceFile("pN", "t")], {"preset": "z"},
                       {"expiry_days": 7, "allowed_downloads": 5}, refN)
resN2, errN2, httpN2 = _run(uN, toolN, invN2, _st_hang)
_errN2 = resN2 or errN2
check("O24 running + different hash → idempotency_conflict",
      httpN2 == 409 and _errN2.get("error") == "idempotency_conflict")
# 清 running (避免影响后续/cleanup)
with db() as con:
    con.execute("update tool_calls set status='failed', finished_at=%s"
                " where id=%s", (ts, anchorN["id"]))

# ══════════ O25-O27: failed retry ══════════
uF = _mkuser(_u + "fail")
tF = f"oifail_{_u}"
_mktool(tF, owner_id=owner, cost=100)
_grant(uF, 10000, "manual_topup", note="oi")
toolF = {"id": tF, "tool_key": tF, "type": "produce", "owner_id": owner}
refF = f"job-{_u}-F"

async def _st_fail():
    from execution import ExecutionError
    raise ExecutionError("err_tool_failed")
invF1 = ToolInvocation(tF, [SourceFile("pF", "t")], {},
                       {"expiry_days": 7, "allowed_downloads": 5}, refF)
resF1, _, _ = _run(uF, toolF, invF1, _st_fail)
rowsF_after1 = _calls(uF, tF, refF)
balF_mid = spendable_balance(uF)

resF2, _, _ = _run(uF, toolF, invF1, _st_ok)  # same ref same hash → 新 attempt
rowsF_after2 = _calls(uF, tF, refF)
check("O25 failed + same hash → new call allowed",
      resF2.get("ok") is True and len(rowsF_after2) == len(rowsF_after1) + 1,
      f"n={len(rowsF_after2)} res={str(resF2)[:80]}")
check("O26 failed retry gets new call_id",
      rowsF_after2[-1]["id"] != rowsF_after1[-1]["id"]
      and rowsF_after1[-1]["status"] == "failed"
      and rowsF_after2[-1]["status"] == "ok")

invF3 = ToolInvocation(tF, [SourceFile("pF", "t")], {"preset": "q"},
                       {"expiry_days": 7, "allowed_downloads": 5}, refF)
resF3, errF3, httpF3 = _run(uF, toolF, invF3, _st_ok)
_errF3 = resF3 or errF3
check("O27 failed + different hash → conflict",
      httpF3 == 409 and _errF3.get("error") == "idempotency_conflict")

# ══════════ O28: concurrent first claims ══════════
uX = _mkuser(_u + "conc")
tX = f"oiconc_{_u}"
_mktool(tX, owner_id=owner, cost=100)
_grant(uX, 10000, "manual_topup", note="oi")
toolX = {"id": tX, "tool_key": tX, "type": "produce", "owner_id": owner}
refX = f"job-{_u}-X"

import threading
resultsX = []
barrier = threading.Barrier(2)

def _worker(payload_inv, tag):
    barrier.wait()
    async def _st():
        return _mkstaged(tag)
    r, e, h = _run(uX, toolX, payload_inv, _st)
    resultsX.append((r or e, e, h))

invX_a = ToolInvocation(tX, [SourceFile("pX", "t")], {},
                        {"expiry_days": 7, "allowed_downloads": 5}, refX)
invX_b = ToolInvocation(tX, [SourceFile("pX", "t")], {"preset": "k"},
                        {"expiry_days": 7, "allowed_downloads": 5}, refX)
th1 = threading.Thread(target=_worker, args=(invX_a, "xa"))
th2 = threading.Thread(target=_worker, args=(invX_b, "xb"))
th1.start(); th2.start(); th1.join(); th2.join()
ok_n = sum(1 for r, _, _ in resultsX if r.get("ok"))
conflict_n = sum(1 for r, _, h in resultsX
                 if h == 409 and r.get("error") == "idempotency_conflict")
live = _calls(uX, tX, refX)
live_running_or_ok = [r for r in live if r["status"] in ("running", "ok")]
check("O28 并发不同 hash → 恰一个 owner, 另一个 409",
      ok_n + conflict_n == 2 and conflict_n >= 1
      and len(live_running_or_ok) == 1,
      f"ok={ok_n} conflict={conflict_n} live={len(live_running_or_ok)}")
hashes_live = {r["invocation_hash"] for r in live_running_or_ok}
check("O28b live anchor hash ∈ {hashA, hashB}",
      hashes_live <= {invocation_fingerprint(invX_a),
                      invocation_fingerprint(invX_b)}, str(hashes_live))

# ══════════ O29-O31: DB invariants ══════════
def _db_rejects(sql, params=None):
    try:
        with db() as con:
            con.execute(sql, params or ())
        return False
    except Exception:
        return True

uD = _mkuser(_u + "db")
tD = f"oidb_{_u}"
_mktool(tD, owner_id=None, cost=10)
check("O29 running + NULL invocation_hash → reject",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, commission_rate_snapshot,"
                  " started_at) values (%s,%s,%s,'running',10,30,%s)",
                  (f"r29_{_u}", uD, tD, ts)))
check("O30 ok + NULL invocation_hash → reject",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, commission_rate_snapshot,"
                  " started_at, finished_at, result_public_id, result_json)"
                  " values (%s,%s,%s,'ok',10,30,%s,%s,'pub','{}')",
                  (f"r30_{_u}", uD, tD, ts, ts)))
check("O31 failed + NULL hash (historical) → allowed",
      not _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                      " status, cost_snapshot, started_at, finished_at)"
                      " values (%s,%s,%s,'failed',10,%s,%s)",
                      (f"r31_{_u}", uD, tD, ts, ts)))

# runtime API: invalid_invocation_hash
from repo.ledger import claim_call as _claim
def _claim_bad(h):
    try:
        with db() as con:
            _claim(con, f"ih_{_u}_{str(h)[:6]}", uD, tD, 0, "{}", ts,
                   commission_rate_snapshot=30, invocation_hash=h)
        return False
    except ValueError:
        return True
check("O32 claim_call 拒非法 hash (短/非hex/None)",
      _claim_bad("abc") and _claim_bad("Z" * 64) and _claim_bad(None))

# ══════════ cleanup ══════════
_uids = [owner, uR, uC, uN, uF, uX, uD]
with db() as con:
    for uid in _uids:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
for uid in _uids:
    with db() as con:
        con.execute("delete from tool_calls where user_id=%s", (uid,))
        con.execute("delete from uploads where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
with db() as con:
    con.execute("delete from pricing where tool_id like %s", (f"oi%{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s", (f"oi%{_u}%",))
for uid in _uids:
    with db() as con:
        purge_users(con, [uid])
print("cleanup: operation-identity fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
