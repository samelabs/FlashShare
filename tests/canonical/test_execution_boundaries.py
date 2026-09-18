#!/usr/bin/env python3
"""Execution resource canonical — correctness of failure boundaries.

Fault-injection ONLY (no real hogs, no infinite loops, no OOM, no
process-kill stress): bounded reads fail closed at cap+1 and close the
stream; source bytes are validated against declared size and budgets
incrementally; BrokenProcessPool maps to a typed execution failure that
converges through the existing gate refund path; pool reset under
concurrency adopts exactly one replacement; a worker-death failure
converges through the REAL gate (exact charge refund, terminal failed,
no duplicate refund, no running call, balance restored).
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

import io
import time
ts = int(time.time())

from config import MAX_UPLOAD_BYTES
from database import db
from execution.orchestrator import (_read_source_bounded, _SourceTooLarge,
                                    MAX_MULTI_BYTES)
from execution import pool as pool_mod

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

_u = "eb" + _os.urandom(4).hex()

# ══════════ A. bounded source read (fault injection) ══════════
class _Stream:
    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.closed = False
    def read(self, n=-1):
        return self._buf.read(n)
    def close(self):
        self.closed = True

s = _Stream(b"x" * 100)
data = _read_source_bounded(s, 100, MAX_UPLOAD_BYTES)
check("A1 bounded read 正常读全 + 关闭", data == b"x" * 100 and s.closed)

s = _Stream(b"x" * 50)
try:
    _read_source_bounded(s, 50, 30)
    check("A2 实际体超 budget → fail closed + 关闭", False, "no raise")
except _SourceTooLarge:
    check("A2 实际体超 budget → fail closed + 关闭", s.closed)

class _NeverRead:
    def read(self, n=-1):
        raise AssertionError("body must not be read")
    def close(self):
        self.closed = True
s2 = _NeverRead()
try:
    _read_source_bounded(s2, MAX_UPLOAD_BYTES + 1, MAX_UPLOAD_BYTES)
    check("A3 声明 size 超 cap → 零读取早拒 + 关闭", False, "no raise")
except _SourceTooLarge:
    check("A3 声明 size 超 cap → 零读取早拒 + 关闭", getattr(s2, "closed", False))

# 实际体大 / 声明小 (DB 谎报) → fail closed
s3 = _Stream(b"x" * 40)
try:
    _read_source_bounded(s3, 5, 30)   # declared 5, actual 40, budget 30
    check("A4 实际体>声明且超 budget → fail closed", False, "no raise")
except _SourceTooLarge:
    check("A4 实际体>声明且超 budget → fail closed", True)

# multi aggregate incremental: (4,4,9) budget 10 → stops before 3rd body
stops = None
total = 0
for i, size in enumerate((4, 4, 9)):
    st = _Stream(b"y" * size)
    try:
        d = _read_source_bounded(st, size, 10 - total)
    except _SourceTooLarge:
        stops = i
        break
    total += len(d)
check("A5 multi 聚合 incremental — 第三个文件前停止", stops == 2, f"stopped_at={stops}")

# ══════════ B. worker bounded output (real worker fn, tiny caps) ══════════
from execution import worker as worker_mod

def _handler_returning(n):
    def _h(tool_input, params):
        return {"stream": io.BytesIO(b"z" * n), "content_type": "t/plain",
                "filename": "o.bin"}
    return _h

import tools as _tools
_tools._registry.append({"id": "zz_at_cap", "handler": _handler_returning(8),
                         "type": "produce", "multi": False, "pass_through": False})
_tools._registry.append({"id": "zz_cap1", "handler": _handler_returning(9),
                         "type": "produce", "multi": False, "pass_through": False})
import config as _cfg
_orig_cap = _cfg.MAX_UPLOAD_BYTES
try:
    _cfg.MAX_UPLOAD_BYTES = 8
    r = worker_mod.process_tool("zz_at_cap", b"", "i", "t", 0, {}, None)
    check("B1 output 恰在 cap (8) 成功", r["size"] == 8, str(r["size"]))
    try:
        worker_mod.process_tool("zz_cap1", b"", "i", "t", 0, {}, None)
        check("B2 output cap+1 → ValueError fail closed", False, "no raise")
    except ValueError as e:
        check("B2 output cap+1 → ValueError fail closed",
              "output_too_large" in str(e), str(e))
finally:
    _cfg.MAX_UPLOAD_BYTES = _orig_cap
    _tools._registry[:] = [t for t in _tools._registry
                           if t["id"] not in ("zz_at_cap", "zz_cap1")]

# ══════════ C. storage bounded (fake oversized body) ══════════
import storage as _storage_mod
class _FakeRaw:
    def __init__(self, data): self._d = data
    def read(self, n=-1): return self._d[:n] if n and n > 0 else self._d
    def close(self): self.closed = True
_status, _hdrs, _body = None, None, None
def _fake_open(file_id):
    return 200, {}, _FakeRaw(b"q" * (MAX_UPLOAD_BYTES + 1))
_orig_open = _storage_mod.open_gokapi_download
_storage_mod.open_gokapi_download = _fake_open
try:
    _storage_mod.read("whatever")
    check("C1 storage 实际体>平台 cap → ValueError", False, "no raise")
except ValueError:
    check("C1 storage 实际体>平台 cap → ValueError", True)
finally:
    _storage_mod.open_gokapi_download = _orig_open

# ══════════ D. BrokenProcessPool: submit / await 注入 + CAS 重建 ══════════
import asyncio
from concurrent.futures.process import BrokenProcessPool
from execution import pool as pool_mod

async def _run_with(pool):
    from execution.orchestrator import _run_handler
    return await _run_handler(pool, lambda *a: None, "t",
                              (b"", "f", "t", 0), None, {})

class _SubmitBroken:          # raises at submit (run_in_executor entry)
    def submit(self, *a, **k):
        raise BrokenProcessPool("died at submit")

class _AwaitBroken:           # submit ok, failure surfaces at await
    def submit(self, *a, **k):
        import concurrent.futures as _cf
        fut = _cf.Future()
        fut.set_exception(BrokenProcessPool("died at await"))
        return fut

n0 = pool_mod.pool_reset_count()
try:
    asyncio.new_event_loop().run_until_complete(_run_with(_SubmitBroken()))
    check("D1 submit 阶段 BrokenProcessPool → RuntimeError(worker_pool_broken)",
          False, "no raise")
except RuntimeError as e:
    check("D1 submit 阶段 BrokenProcessPool → RuntimeError(worker_pool_broken)",
          "worker_pool_broken" in str(e), str(e))

n1 = pool_mod.pool_reset_count()
try:
    asyncio.new_event_loop().run_until_complete(_run_with(_AwaitBroken()))
    check("D2 await 阶段 BrokenProcessPool → RuntimeError(worker_pool_broken)",
          False, "no raise")
except RuntimeError as e:
    check("D2 await 阶段 BrokenProcessPool → RuntimeError(worker_pool_broken)",
          "worker_pool_broken" in str(e), str(e))

# D3: CAS single replacement — N callers race reset_pool(observed_old)
class _FakePool:
    def __init__(self, tag): self.tag = tag; self.shut = False
    _broken = False
    def shutdown(self, **k): self.shut = True
_old_new = pool_mod._new_pool
_seq = iter(f"P{i}" for i in range(10))
pool_mod._new_pool = lambda: _FakePool(next(_seq))
try:
    observed = pool_mod._pool
    import threading as _th
    barr = _th.Barrier(8)
    results = []
    def _racer():
        barr.wait(timeout=10)
        results.append(pool_mod.reset_pool(observed, "race"))
    ths = [_th.Thread(target=_racer) for _ in range(8)]
    [t.start() for t in ths]
    [t.join(20) for t in ths]
    reps = {id(r) for r in results}
    check("D3 CAS: 8 并发 reset 同一 observed → 全部拿到同一 replacement",
          len(results) == 8 and len(reps) == 1, f"distinct={len(reps)}")
    cnt0 = pool_mod.pool_reset_count()
    r_stale = pool_mod.reset_pool(observed, "stale")
    check("D3b 迟到 caller → 复用既有 replacement, 零新建",
          r_stale is results[0] and pool_mod.pool_reset_count() == cnt0)
    check("D3c get_pool() == replacement", pool_mod.get_pool() is results[0])
finally:
    pool_mod._new_pool = _old_new
    real = pool_mod._new_pool()
    with pool_mod._pool_lock:
        pool_mod._pool = real

# ══════════ E. 真实 gate 收敛 (worker_pool_broken → tool_failed/refund) ══════════
from economy.billing import grant as _grant, spendable_balance
from repo.ledger import by_idem_key

import json as _json
user_id = _mkuser(_u + "u")
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, multi, pass_through, visibility,"
        " enabled, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', 'produce', %s,"
        " 'wand', FALSE, FALSE, 'public', TRUE, %s, %s)",
        (f"ebtool_{_u}", f"ebtool_{_u}",
         _json.dumps({"en": "EB"}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 25, TRUE, %s)", (f"ebtool_{_u}", ts))
_grant(user_id, 1000, "manual_topup", note="eb fixture")

from execution.invocation import ToolInvocation, SourceFile

inv = ToolInvocation(
    tool_id=f"ebtool_{_u}",
    files=[SourceFile("pub_x", "tok_x")],
    params={}, delivery={"expiry_days": 7, "allowed_downloads": 10},
    client_ref=None)

async def _boom():
    raise RuntimeError("worker_pool_broken")

from execution import gate
bal_before = spendable_balance(user_id)
result, err, status = asyncio.new_event_loop().run_until_complete(gate.run(
    {"id": user_id},
    {"id": f"ebtool_{_u}", "tool_key": f"ebtool_{_u}",
     "type": "produce", "owner_id": None},
    inv, _boom))
check("E1 gate 响应: tool_failed / 500",
      err is not None and err.get("error") == "tool_failed" and status == 500,
      f"{err} {status}")

with db() as con:
    call = con.execute(
        "select id, status, finished_at from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (user_id, f"ebtool_{_u}")).fetchone()
check("E2 tool_call terminal failed + finished_at 非空",
      call is not None and call["status"] == "failed"
      and call["finished_at"] is not None, str(dict(call) if call else None))

call_id = int(call["id"])
with db() as con:
    charge = by_idem_key(con, user_id, f"call:{call_id}")
    refund = by_idem_key(con, user_id, f"refund:ledger:{charge['id']}")
check("E3 exact consume 存在 (-25)",
      charge is not None and int(charge["delta"]) == -25,
      str(dict(charge) if charge else None))
check("E4 exact refund 存在且等额 (+25, 同一 charge 引用)",
      refund is not None and int(refund["delta"]) == 25
      and refund["ref_id"] == str(charge["id"]),
      str(dict(refund) if refund else None))
check("E5 最终余额恢复 (1000)",
      spendable_balance(user_id) == bal_before,
      f"{bal_before} → {spendable_balance(user_id)}")
with db() as con:
    n_refunds = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'"
        " and ref_id=%s", (user_id, str(charge["id"]))).fetchone()["c"])
    running = int(con.execute(
        "select count(*) c from tool_calls where user_id=%s"
        " and status='running'", (user_id,)).fetchone()["c"])
check("E6 无重复 refund / 无遗留 running",
      n_refunds == 1 and running == 0, f"refunds={n_refunds} running={running}")

# ══════════ F. semantic ══════════
_orch_src = open(_os.path.join(_REPO, "core", "execution", "orchestrator.py"),
                 encoding="utf-8").read()
_gate_src = open(_os.path.join(_REPO, "core", "execution", "gate.py"),
                 encoding="utf-8").read()
check("F1 无 err_worker_died / 无新经济状态",
      "err_worker_died" not in _orch_src and "resource" not in _gate_src.lower())

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
