#!/usr/bin/env python3
"""Access Projection canonical invariants — 只读 read model 契约.

锁住: Free/PLUS/expired 三态 membership↔entitlement 域边界、rolling-24h
usage 窗口语义、JSON-safe 输出、纯只读 (跨业务表零写入)。遵循 canonical
fail-closed 规则: 显式 isolated port + TEST_DB_URL, 拒绝 production。
"""
import os as _os, sys as _sys
import sys
import json, time

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

# ── FAIL-CLOSED GATE (同 Stage C 规则) ─────────────────────────────────
_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required (isolated service port, never the production port).\n")
    _sys.exit(2)
try:
    _TEST_PORT_INT = int(_TEST_PORT, 10)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer port number.\n")
    _sys.exit(2)
if _TEST_PORT_INT == 8000:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT=8000 is the production port; canonical tests must use an isolated port.\n")
    _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required (isolated test database DSN).\n")
    _sys.exit(2)

sys.path.insert(0, _REPO)
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")  # config 导入守卫; 本测试不签发 session
sys.path.insert(0, _os.path.join(_REPO, "core"))

import psycopg2
from governance.access_projection import project
from governance.entitlements import FREE, _PLANS, resolve, resolve_from_membership
from economy.subscription import plan_of
from sharing.uploads import usage_24h
from repo import upload_usage as usage_repo
from database import db

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_ts = int(time.time())

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts, _ts))
    return u, _cur.fetchone()[0]

def _seed_plan(user_id, code, status, renews_at):
    _cur.execute("select id from plans where code=%s", (code,))
    row = _cur.fetchone()
    if not row:
        _cur.execute(
            "insert into plans (code, name, monthly_credits, price_cents, created_at, updated_at)"
            " values (%s, %s, 0, 0, %s, %s) returning id", (code, code, _ts, _ts))
        plan_id = _cur.fetchone()[0]
    else:
        plan_id = row[0]
    _cur.execute(
        "insert into user_plans (user_id, plan_id, status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
        (user_id, plan_id, status, _ts, renews_at, _ts))

# ═══ 1. Free: 无 membership ═══
u_f, id_f = _new_user("apf")
p = project(id_f)
check("AP-F1 Free membership is None (不伪造会员记录)",
      p["membership"] is None, str(p["membership"]))
check("AP-F2 Free entitlements 精确等于 FREE contract (JSON list 形)",
      p["entitlements"] == {
          "plan_code": "free",
          "ingress_files_24h": FREE["ingress_files_24h"],
          "ingress_bytes_24h": FREE["ingress_bytes_24h"],
          "expiry_days": list(FREE["expiry_days"]),
          "download_limits": list(FREE["download_limits"])}, str(p["entitlements"]))
check("AP-F3 Free 初始 usage 0/0",
      p["usage"] == {"ingress_files_24h": 0, "ingress_bytes_24h": 0}, str(p["usage"]))

# ═══ 2. PLUS: 有效会员 ═══
u_p, id_p = _new_user("app")
_seed_plan(id_p, "plus", "active", _ts + 30 * 86400)
p = project(id_p)
check("AP-P1 PLUS membership 原样透出 (member=true, code=plus)",
      p["membership"] and p["membership"]["member"] is True
      and p["membership"]["code"] == "plus", str(p["membership"]))
check("AP-P2 PLUS entitlements 精确值来自 resolver",
      p["entitlements"] == {
          "plan_code": "plus",
          "ingress_files_24h": _PLANS["plus"]["ingress_files_24h"],
          "ingress_bytes_24h": _PLANS["plus"]["ingress_bytes_24h"],
          "expiry_days": list(_PLANS["plus"]["expiry_days"]),
          "download_limits": list(_PLANS["plus"]["download_limits"])}, str(p["entitlements"]))

# ═══ 3. Expired: membership fact ≠ effective entitlement ═══
u_e, id_e = _new_user("ape")
_seed_plan(id_e, "plus", "active", _ts - 100)  # renews_at 已过
p = project(id_e)
check("AP-E1 expired membership 保留真实 plan_of 结果 (member=false)",
      p["membership"] is not None and p["membership"]["member"] is False
      and p["membership"]["code"] == "plus", str(p["membership"]))
check("AP-E2 expired → entitlements.plan_code = free (域边界)",
      p["entitlements"]["plan_code"] == "free", str(p["entitlements"]["plan_code"]))

# ═══ 4. Rolling usage 窗口 ═══
u_u, id_u = _new_user("apu")
with db() as c:
    usage_repo.insert_many(c, id_u, "127.0.0.1", [100, 23], _ts)          # 窗口内 2 files / 123 bytes
_cur.execute(
    "insert into upload_usage (user_id, ip, size_bytes, created_at) values (%s, '127.0.0.1', 999999, %s)",
    (id_u, _ts - 25 * 3600))                                                # 窗口外
p = project(id_u)
check("AP-U1 projection 只统计 24h 窗口内 usage (2 files / 123 bytes)",
      p["usage"] == {"ingress_files_24h": 2, "ingress_bytes_24h": 123}, str(p["usage"]))
check("AP-U2 usage_24h public reader 同口径",
      usage_24h(id_u) == {"ingress_files_24h": 2, "ingress_bytes_24h": 123})

# ═══ 5. JSON-safe ═══
try:
    json.dumps(project(id_p)); json.dumps(project(id_e)); json.dumps(project(id_f))
    check("AP-J1 json.dumps(project(...)) 直接成功 (三态)", True)
except (TypeError, ValueError) as exc:
    check("AP-J1 json.dumps(project(...)) 直接成功 (三态)", False, str(exc))

# ═══ 6. Read-only (跨业务表计数前后不变) ═══
def _tbl_counts():
    out = {}
    for t in ("user_plans", "upload_usage", "ledger", "tool_calls"):
        _cur.execute(f"select count(*) from {t}")
        out[t] = _cur.fetchone()[0]
    return out

_b = _tbl_counts()
for _ in range(3):
    project(id_p); project(id_f); project(id_u); project(id_e)
_a = _tbl_counts()
check("AP-R1 projection 纯只读 (user_plans/upload_usage/ledger/tool_calls 计数不变)",
      _a == _b, f"before={_b} after={_a}")

# ═══ 7. resolve 与 projection 同消费 resolve_from_membership (行为等价) ═══
check("AP-S1 resolve(user_id) == resolve_from_membership(plan_of(user_id)) (三个用户)",
      resolve(id_p) == resolve_from_membership(plan_of(id_p))
      and resolve(id_f) == resolve_from_membership(plan_of(id_f))
      and resolve(id_e) == resolve_from_membership(plan_of(id_e)))

# ═══ 7b. Fact-read failure propagates (read model ≠ fail-safe 层) ═══
import governance.access_projection as _apmod
_orig_plan_of = _apmod.plan_of
def _boom_plan_of(_uid):
    raise RuntimeError("membership read failed")
_apmod.plan_of = _boom_plan_of
try:
    project(id_f)
    check("AP-X1 plan_of 异常向上传播 (不伪装 membership=null)", False, "no raise")
except RuntimeError as _exc:
    check("AP-X1 plan_of 异常向上传播 (不伪装 membership=null)",
          "membership read failed" in str(_exc))
finally:
    _apmod.plan_of = _orig_plan_of
# 恢复后正常:
check("AP-X2 monkeypatch 恢复后 projection 正常",
      project(id_f)["membership"] is None)

# ═══ 8. Source-of-truth guards ═══
_src = open(_os.path.join(_REPO, "core", "governance", "access_projection.py"), encoding="utf-8").read()
import ast as _ast
_ast_mod = _ast.parse(_src)
_doc = _ast.get_docstring(_ast_mod) or ""
_code = _src.replace(_doc, "")  # 剔除模块 docstring 后只查可执行代码
_code = "\n".join(l for l in _code.split("\n")
                  if not l.strip().startswith(("#", '"', "'")))
_imports = [l for l in _src.split("\n") if l.startswith(("import ", "from "))]
check("AP-G1 projection 无 plus 业务判断 (代码行, docstring 提及禁令本身除外)",
      '"plus"' not in _code and "'plus'" not in _code)
check("AP-G2 不 import config/pricing/billing/daily/Creem",
      not any(x in l for l in _imports for x in
              ("config", "pricing", "billing", "daily", "creem", "economy.billing")))
check("AP-G3 无直接 SQL / repo 查询",
      "SELECT" not in _src.upper() and "execute(" not in _code
      and "repo" not in _src)
check("AP-G4 消费 resolve_from_membership + usage_24h + plan_of",
      "resolve_from_membership" in _src and "usage_24h" in _src
      and "plan_of" in _src)
_ent_src = open(_os.path.join(_REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
check("AP-G5 member/code 映射只存在于 entitlements (单点)",
      'membership.get("member")' in _ent_src)
check("AP-G6 projection 无异常吞噬伪装 Free (except→membership=None)",
      "except Exception:" not in _code
      and "membership = None" not in _code)

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
