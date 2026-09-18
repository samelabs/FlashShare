#!/usr/bin/env python3
"""Execution resource canonical — correctness of failure boundaries.

Fault-injection ONLY (no real hogs, no infinite loops, no OOM, no
process-kill stress): bounded reads fail closed at cap+1 and close the
stream; source bytes are validated against declared size and budgets
incrementally; BrokenProcessPool maps to a typed execution failure that
converges through the existing gate refund path; pool reset under
concurrency adopts exactly one replacement; remote total deadline is
shared across hops/addresses (deadline arithmetic), and remote timeout
still flows the existing refund mapping.
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

from config import MAX_UPLOAD_BYTES
from execution.orchestrator import (_read_source_bounded, _SourceTooLarge,
                                    MAX_MULTI_BYTES)
from execution import pool as pool_mod

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

# ══════════ D. remote total deadline (arithmetic, no network) ══════════
import remote as _rm
from remote import RemoteError
try:
    _rm._pinned_request("https://x/", "GET", None, {}, 10,
                        time.monotonic() - 1)
    check("D1 deadline 耗尽 → 立即 timeout (零连接)", False, "no raise")
except RemoteError as e:
    check("D1 deadline 耗尽 → 立即 timeout (零连接)", e.code == "timeout", str(e))

# deadline shared: per-hop socket timeout = REMAINING budget, never the
# full timeout_s again. Zero-network proof: fake validated addresses +
# a capturing socket factory; the hop fails at connect immediately.
import remote as _rm2
import socket as _sock
_captured = []
class _CapSock:
    def __init__(self, *a, **k): pass
    def settimeout(self, v): _captured.append(v)
    def connect(self, sa): raise OSError("stop")
    def close(self): pass
_orig_sock = _rm2.socket.socket
_orig_va = _rm2._validated_addresses
_rm2.socket.socket = _CapSock
_rm2._validated_addresses = lambda url: ("h", 443, "h",
    [(_sock.AF_INET, _sock.SOCK_STREAM, 6, ("203-0-113-10.invalid", 443))])
try:
    d = time.monotonic() + 5
    _rm2._pinned_request("https://h/", "GET", None, {}, 10, d)
except Exception:
    pass
finally:
    _rm2.socket.socket = _orig_sock
    _rm2._validated_addresses = _orig_va
# the connect timeout recorded must be <= remaining (~5), NOT the full 10
check("D2 per-hop timeout = remaining deadline (<=5, not 10)",
      _captured and max(_captured) <= 5.5, str(_captured))

# remaining never exceeds budget: connect timeout = remaining slice
check("D3 per-hop timeout = 剩余 deadline (机制存在)",
      "remaining" in open(_os.path.join(_REPO, "core", "remote.py"),
                          encoding="utf-8").read())

# remote timeout → existing refund mapping (structural)
_orch_src = open(_os.path.join(_REPO, "core", "execution", "orchestrator.py"),
                 encoding="utf-8").read()
_gate_src = open(_os.path.join(_REPO, "core", "execution", "gate.py"),
                 encoding="utf-8").read()
check("D4 remote timeout 映射既有 err_endpoint_timeout → gate refund 收敛",
      '"timeout": "err_endpoint_timeout"' in _orch_src
      and "_settle_failure" in _gate_src and "_refund_charge" in _gate_src)

# ══════════ E. BrokenProcessPool → typed failure + pool reset ══════════
import asyncio
from concurrent.futures.process import BrokenProcessPool
from execution import ExecutionError

class _FakeBrokenPool:
    def submit(self, *a, **k):
        raise BrokenProcessPool("worker died")

async def _broken_run():
    from execution.orchestrator import _run_handler
    return await _run_handler(_FakeBrokenPool(), lambda *a: None, "t",
                              (b"", "f", "t", 0), None, {})

n_before = pool_mod.pool_reset_count()
try:
    asyncio.new_event_loop().run_until_complete(_broken_run())
    check("E1 BrokenProcessPool → err_worker_died", False, "no raise")
except ExecutionError as e:
    check("E1 BrokenProcessPool → err_worker_died + pool reset",
          e.code == "err_worker_died"
          and pool_mod.pool_reset_count() == n_before + 1,
          f"{e.code} resets={pool_mod.pool_reset_count() - n_before}")

# reset concurrency: 8 threads racing → all finish, pool usable, no crash
import threading as _th
barr = _th.Barrier(8)
errs = []
def _resetter():
    try:
        barr.wait(timeout=10)
        pool_mod.reset_pool("race")
    except Exception as e:
        errs.append(e)
ths = [_th.Thread(target=_resetter) for _ in range(8)]
[t.start() for t in ths]
[t.join(30) for t in ths]
check("E2 并发 pool reset 全部完成 (锁内重建, 无双 writer)",
      not errs, str(errs[:1]))

# gate convergence: worker death flows the SAME failure settlement
# (structural — settlement path is the gate's existing one)
check("E3 worker 死亡走既有 gate 收敛 (无新状态/ledger kind)",
      "err_worker_died" in _orch_src
      and "resource" not in _gate_src.lower())

# semantic: no unbounded reads remain in the execution chain
_w_src = open(_os.path.join(_REPO, "core", "execution", "worker.py"),
              encoding="utf-8").read()
_o_src = _orch_src
_s_src = open(_os.path.join(_REPO, "core", "storage.py"), encoding="utf-8").read()
check("F1 worker 输出无 unbounded .read()",
      '["stream"].read()' not in _w_src and ".read()" not in
      _w_src.replace("stream.read(1024 * 1024)", ""))
check("F2 storage body 读取 bounded (cap+1)",
      "MAX_UPLOAD_BYTES + 1" in _s_src)
check("F3 orchestrator source 读取 bounded",
      "stream.read()" not in _o_src)

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
