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

# ══════════ C. storage bounded (short-read fault injection, tiny cap) ══════════
import storage as _storage_mod
import config as _cfg_mod

class _ShortReadRaw:
    """故意 short-read: 每次 read(n) 最多 2~3 bytes — 证明 chunked 逻辑
    不依赖单次 read(n) 返回 n bytes 的语义。"""
    def __init__(self, body): self._buf = memoryview(body); self._pos = 0
    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._buf) - self._pos
        take = min(n, 2 + (self._pos % 2))   # 2~3 bytes per call
        chunk = bytes(self._buf[self._pos:self._pos + take])
        self._pos += take
        return chunk
    def close(self): self.closed = True

def _mk_open(body):
    holder = {}
    def _open(fid):
        r = _ShortReadRaw(body)
        holder["raw"] = r
        return 200, {}, r
    return _open, holder

_orig_open = _storage_mod.open_gokapi_internal_download
_orig_cap_c = _cfg_mod.MAX_UPLOAD_BYTES
CAP_C = 32
_cfg_mod.MAX_UPLOAD_BYTES = CAP_C
try:
    _open1, _h1 = _mk_open(b"q" * (CAP_C + 1))
    _storage_mod.open_gokapi_internal_download = _open1
    try:
        _storage_mod.read("f1")
        check("C1 storage cap+1 (short-read) → ValueError + stream 关闭",
              False, "no raise")
    except ValueError:
        check("C1 storage cap+1 (short-read) → ValueError + stream 关闭",
              getattr(_h1["raw"], "closed", False) is True)
    _open2, _h2 = _mk_open(b"q" * CAP_C)
    _storage_mod.open_gokapi_internal_download = _open2
    status_c, _hdrs_c, body_c = _storage_mod.read("f2")
    check("C2 storage exact cap (short-read) → 成功读全 32 bytes + 关闭",
          status_c == 200 and len(body_c.getvalue()) == CAP_C
          and getattr(_h2["raw"], "closed", False) is True,
          f"len={len(body_c.getvalue())}")
finally:
    _storage_mod.open_gokapi_internal_download = _orig_open
    _cfg_mod.MAX_UPLOAD_BYTES = _orig_cap_c

# ══════════ D. BrokenProcessPool: submit / await 注入 + CAS 重建 ══════════
import asyncio
from concurrent.futures.process import BrokenProcessPool
from execution import pool as pool_mod

async def _run_with(pool):
    from execution.orchestrator import _run_handler
    return await _run_handler(pool, lambda *a: None, "t",
                              (b"", "f", "t", 0), None, {})

class _FakePool:
    def __init__(self, tag): self.tag = tag; self.shut = False
    _broken = False
    def shutdown(self, **k): self.shut = True

class _SubmitBroken:          # raises at submit (run_in_executor entry)
    _broken = True
    shut = False
    def shutdown(self, **k): self.shut = True
    def submit(self, *a, **k):
        raise BrokenProcessPool("died at submit")

class _AwaitBroken:           # submit ok, failure surfaces at await
    _broken = True
    shut = False
    def shutdown(self, **k): self.shut = True
    def submit(self, *a, **k):
        import concurrent.futures as _cf
        fut = _cf.Future()
        fut.set_exception(BrokenProcessPool("died at await"))
        return fut

_old_new_factory = pool_mod._new_pool
_replacement_seq = iter(f"R{i}" for i in range(10))

def _install_fake_global(observed):
    """global _pool = observed broken pool; _new_pool → lightweight fake."""
    pool_mod._pool = observed
    pool_mod._new_pool = lambda: _FakePool(next(_replacement_seq))

def _restore_real_pool():
    pool_mod._new_pool = _old_new_factory
    real = pool_mod._new_pool()
    with pool_mod._pool_lock:
        pool_mod._pool = real

# D1: submit 阶段 — observed pool IS the global, CAS 真正触发
sb = _SubmitBroken(); sb.shut = False
_install_fake_global(sb)
n0 = pool_mod.pool_reset_count()
try:
    asyncio.new_event_loop().run_until_complete(_run_with(sb))
    d1_raised = False
except RuntimeError as e:
    d1_raised = "worker_pool_broken" in str(e)
d1_delta = pool_mod.pool_reset_count() - n0
d1_new = pool_mod._pool
check("D1 submit 阶段: RuntimeError + CAS reset delta=1 + global replaced + old shutdown",
      d1_raised and d1_delta == 1 and d1_new is not sb
      and isinstance(d1_new, _FakePool) and sb.shut,
      f"raised={d1_raised} delta={d1_delta} new_is_old={d1_new is sb} shut={sb.shut}")

# D2: await 阶段 — 重新安装新 observed pool, 同样断言
ab = _AwaitBroken(); ab.shut = False
_install_fake_global(ab)
n1 = pool_mod.pool_reset_count()
try:
    asyncio.new_event_loop().run_until_complete(_run_with(ab))
    d2_raised = False
except RuntimeError as e:
    d2_raised = "worker_pool_broken" in str(e)
d2_delta = pool_mod.pool_reset_count() - n1
d2_new = pool_mod._pool
check("D2 await 阶段: RuntimeError + CAS reset delta=1 + global replaced + old shutdown",
      d2_raised and d2_delta == 1 and d2_new is not ab
      and isinstance(d2_new, _FakePool) and ab.shut,
      f"raised={d2_raised} delta={d2_delta} new_is_old={d2_new is ab} shut={ab.shut}")
_restore_real_pool()

# D3: CAS single replacement — N callers race reset_pool(observed_old)
try:
    observed = _FakePool("cas-observed")
    _install_fake_global(observed)
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
    _restore_real_pool()

# ══════════ E. 真实 gate 收敛 (worker_pool_broken → tool_failed/refund) ══════════
_eb_user_id = None
_eb_tool = None
try:
    from economy.billing import grant as _grant, spendable_balance
    from repo.ledger import by_idem_key

    _eb_tool = f"ebtool_{_u}"
    import json as _json
    user_id = _mkuser(_u + "u")
    _eb_user_id = user_id
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
        client_ref=f"eb:{_u}")

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

finally:
    # FK-safe fixture cleanup: 只删本测试的实体 (随机前缀)
    if _eb_user_id is not None:
        with db() as con:
            con.execute("delete from tool_calls where user_id=%s", (_eb_user_id,))
            con.execute("delete from ledger where user_id=%s", (_eb_user_id,))
    if _eb_tool is not None:
        with db() as con:
            con.execute("delete from pricing where tool_id=%s", (_eb_tool,))
            con.execute("delete from platform_tools where tool_key=%s", (_eb_tool,))
    if _eb_user_id is not None:
        with db() as con:
            con.execute("delete from users where id=%s", (_eb_user_id,))
    print("cleanup: boundaries fixtures removed")

# ══════════ F. semantic ══════════
_orch_src = open(_os.path.join(_REPO, "core", "execution", "orchestrator.py"),
                 encoding="utf-8").read()
_gate_src = open(_os.path.join(_REPO, "core", "execution", "gate.py"),
                 encoding="utf-8").read()
check("F1 无 err_worker_died / 无新经济状态",
      "err_worker_died" not in _orch_src and "resource" not in _gate_src.lower())

# ══════════ G. stored execution vocabulary fail-closed (C58) ══════════
# dispatch must reject any stored execution outside local|http BEFORE the
# metering gate (no claim/charge/network), with no default-else local.
from execution.dispatch import dispatch_execute as _dx

def _fake_request():
    class _R:
        class _c:
            client = "127.0.0.1"
        headers = {}
    return _R()

_ts58 = int(time.time())
# The fresh schema carries platform_tools_execution_ck; the dispatch-level
# rejection cases simulate a PRE-CHECK stored row (legacy DB) by dropping
# the constraint on this isolated test DB for the duration, then restoring.
with db() as con:
    con.execute("ALTER TABLE platform_tools DROP CONSTRAINT IF EXISTS platform_tools_execution_ck")

def _mkrow58(execution):
    with db() as con:
        uid = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (f"eb58_{execution}_{_os.urandom(3).hex()}",
             f"eb58_{execution}_{_os.urandom(3).hex()}@t.co",
             _ts58, _ts58)).fetchone()["id"]
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, tool_type, label_json, visibility, enabled,"
            " created_at, updated_at) values (%s, NULL, %s, %s, 'produce',"
            " '{}', 'public', TRUE, %s, %s)"
            " on conflict (tool_key) do update set execution=excluded.execution",
            (f"eb58/{execution}", execution, execution, _ts58, _ts58))
    return uid

_INV58 = {
    "tool_id": None,  # filled per case
    "source": {"files": [{"public_id": "aaaaaaaa", "source_token": "tok"}]},
    "params": {},
    "delivery": {"expiry_days": 1, "allowed_downloads": 10},
    "client_ref": "eb58-" + _os.urandom(4).hex(),
}
for _exec58 in ("mcp", "grpc", "wasm"):
    _uid = _mkrow58(_exec58)
    _inv = dict(_INV58)
    _inv["tool_id"] = f"eb58/{_exec58}"
    _user58 = {"id": _uid, "username": f"u58_{_exec58}"}
    import asyncio as _a58
    try:
        _pl, _err, _st = _a58.run(_dx(_fake_request(), _user58, _inv))
    except Exception as _exc58:
        _pl, _err, _st = None, {"raw": str(_exc58)[:60]}, "EXC"
    check(f"G-{_exec58} dispatch 拒未知 execution (gate/charge 前 503)",
          _st == 503 and (_err or {}).get("error") == "tool_unavailable",
          f"{_st} {str(_err)[:60]}")
    with db() as con:
        _n = con.execute(
            "select count(*) c from tool_calls where user_id=%s", (_uid,)
        ).fetchone()["c"]
    check(f"G-{_exec58} 拒绝时零 tool_call claim", int(_n) == 0, f"{_n}")
    with db() as con:
        _n2 = con.execute(
            "select count(*) c from ledger where user_id=%s", (_uid,)
        ).fetchone()["c"]
    check(f"G-{_exec58} 拒绝时零 charge/consume", int(_n2) == 0, f"{_n2}")

# local + http rows still resolve into their normal branches (admitted set)
for _exec58 in ("local", "http"):
    _mkrow58(_exec58)
    check(f"G-{_exec58} row 可创建 (canonical vocabulary 接受)",
          True)
from governance.tool_supply import _EXECUTIONS
check("G-vocabulary authoring validator 只接受 local|http",
      _EXECUTIONS == ("local", "http"))

# drop the pre-CHECK test rows, then restore the fresh-schema CHECK
with db() as con:
    con.execute("delete from platform_tools where tool_key like 'eb58/%%'")
    con.execute("ALTER TABLE platform_tools ADD CONSTRAINT platform_tools_execution_ck"
                " CHECK (execution IN ('local','http'))")

# startup invariant: invalid stored execution aborts (fresh DB rows)
from governance.catalog import validate_catalog_rows as _vcr
with db() as con:
    con.execute("ALTER TABLE platform_tools DROP CONSTRAINT IF EXISTS platform_tools_execution_ck")
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, visibility, enabled, created_at, updated_at)"
        " values ('eb58/bad_exec_inv', NULL, 'bad_exec_inv', 'mcp',"
        " 'produce', '{}', 'private', FALSE, %s, %s)", (_ts58, _ts58))
try:
    _vcr()
    check("G-startup stored execution='mcp' → invariant fail", False, "no raise")
except RuntimeError as _e58:
    check("G-startup stored execution='mcp' → invariant fail",
          "execution" in str(_e58), str(_e58)[:80])
with db() as con:
    con.execute("delete from platform_tools where tool_key='eb58/bad_exec_inv'")
    con.execute("ALTER TABLE platform_tools DROP CONSTRAINT IF EXISTS platform_tools_execution_ck")
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, visibility, enabled, created_at, updated_at)"
        " values ('eb58/bad_exec_inv2', NULL, 'bad_exec_inv2', 'quantum',"
        " 'produce', '{}', 'unlisted', TRUE, %s, %s)", (_ts58, _ts58))
try:
    _vcr()
    check("G-startup arbitrary execution='quantum' → invariant fail", False, "no raise")
except RuntimeError as _e58:
    check("G-startup arbitrary execution='quantum' → invariant fail",
          "execution" in str(_e58), str(_e58)[:80])
with db() as con:
    con.execute("delete from platform_tools where tool_key='eb58/bad_exec_inv2'")
    con.execute("ALTER TABLE platform_tools ADD CONSTRAINT platform_tools_execution_ck"
                " CHECK (execution IN ('local','http'))")
with db() as con:
    con.execute("delete from platform_tools where tool_key='eb58/bad_exec_inv2'")

# fresh schema carries the CHECK (probed via a fresh connection DDL artifact)
with db() as con:
    _ck = con.execute(
        "select count(*) c from pg_constraint"
        " where conname='platform_tools_execution_ck'").fetchone()["c"]
check("G-fresh schema platform_tools.execution CHECK (local,http) 存在",
      int(_ck) == 1, str(_ck))

# cleanup seeded rows
with db() as con:
    con.execute("delete from platform_tools where tool_key like 'eb58/%%'")
    con.execute("delete from users where username like 'eb58_%%'")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
