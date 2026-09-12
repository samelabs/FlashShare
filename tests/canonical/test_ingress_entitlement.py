#!/usr/bin/env python3
"""Ingress Entitlement invariants — 用户身份 → plan_of → entitlement → atomic quota.

长期 baseline: 把 entitlement 映射与 24h quota 原子性锁进 canonical 体系。
遵循 canonical fail-closed 规则: 必须显式提供 isolated port + isolated
TEST_DB_URL, 禁止 production DB/port。业务契约断言, 不 snapshot 实现细节。
"""
import os as _os, sys as _sys
import sys
import time, threading, re

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

# ── FAIL-CLOSED GATE (与 Stage C 同规则) ─────────────────────────────────
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
from governance.entitlements import resolve, FREE, _PLAN_INGRESS
from economy.subscription import plan_of
from sharing.uploads import reserve_ingress, release_ingress
from repo import upload_usage as usage_repo
from database import db

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()

def _uid(username):
    _cur.execute("select id from users where username=%s", (username,))
    row = _cur.fetchone()
    return row[0] if row else None

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", int(time.time()), int(time.time())))
    return u, _cur.fetchone()[0]

def _seed_plan(user_id, code, status, renews_at):
    _cur.execute("select id from plans where code=%s", (code,))
    row = _cur.fetchone()
    ts = int(time.time())
    if not row:
        _cur.execute(
            "insert into plans (code, name, monthly_credits, price_cents, created_at, updated_at)"
            " values (%s, %s, 0, 0, %s, %s) returning id", (code, code, ts, ts))
        plan_id = _cur.fetchone()[0]
    else:
        plan_id = row[0]
    _cur.execute(
        "insert into user_plans (user_id, plan_id, status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
        (user_id, plan_id, status, ts, renews_at, ts))

def _clear_usage(user_id):
    _cur.execute("delete from upload_usage where user_id=%s", (user_id,))

PLUS_BYTES = 5 * 1024 ** 3
GB = 1024 ** 3

# ═══ 1. Entitlement resolver ═══
u_f, id_f = _new_user("entf")
r = resolve(id_f)
check("R1 无 plan → Free", r == {"plan_code": "free",
      "ingress_files_24h": FREE["ingress_files_24h"],
      "ingress_bytes_24h": FREE["ingress_bytes_24h"]}, str(r))

u_e, id_e = _new_user("ente")
_seed_plan(id_e, "plus", "active", int(time.time()) - 100)  # renews_at 已过 → member=false
p = plan_of(id_e)
check("R2a fixture: 过期 plus 行 plan_of().member == False", p and p["member"] is False, str(p))
r = resolve(id_e)
check("R2b member=false 的 plus → Free", r["plan_code"] == "free", str(r))

u_p, id_p = _new_user("entp")
_seed_plan(id_p, "plus", "active", int(time.time()) + 30 * 86400)
p = plan_of(id_p)
check("R3a fixture: active plus member == True", p and p["member"] is True, str(p))
r = resolve(id_p)
check("R3b member+plus → 500 files / 5 GiB",
      r == {"plan_code": "plus", "ingress_files_24h": 500, "ingress_bytes_24h": PLUS_BYTES}, str(r))

u_x, id_x = _new_user("entx")
_seed_plan(id_x, "future-unknown", "active", int(time.time()) + 30 * 86400)
p = plan_of(id_x)
check("R4a fixture: 未知 code member == True", p and p["member"] is True, str(p))
r = resolve(id_x)
check("R4b member+未知 code → Free", r["plan_code"] == "free", str(r))

# resolver 不读 Creem/payment 内部: 源码静态守卫 (契约: entitlements 只 import plan_of)
src = open(_os.path.join(_REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_import_lines = "\n".join(l for l in src.split("\n") if l.startswith(("import ", "from ")))
check("R5 resolver 不 import creem/orders/payment 内部",
      not re.search(r"creem|orders|payment|webhook", _import_lines, re.I))
check("R6 resolver 只经 economy.subscription.plan_of 取会员事实",
      "plan_of" in src and "user_plans" not in src)

# ═══ 2. Free quota 边界 ═══
ip = "203.0.113.9"
FLIM = FREE["ingress_files_24h"]; BLIM = FREE["ingress_bytes_24h"]
_clear_usage(id_f)
ok, r, ids = reserve_ingress(id_f, ip, [1] * (FLIM - 1))
check("F1 Free limit-1 files 通过", ok)
ok, r, ids2 = reserve_ingress(id_f, ip, [1])
check("F2 Free 第 limit 个通过", ok)
ok, r, _ = reserve_ingress(id_f, ip, [1])
check("F3 Free limit+1 拒 err_quota_count", (not ok) and r == "err_quota_count", r)
_clear_usage(id_f)
ok, r, _ = reserve_ingress(id_f, ip, [BLIM])
check("F4 Free 恰 bytes-limit 通过", ok)
ok, r, _ = reserve_ingress(id_f, ip, [1])
check("F5 Free limit+1 byte 拒 err_quota_size", (not ok) and r == "err_quota_size", r)

# ═══ 3. PLUS quota 边界 ═══
_clear_usage(id_p)
ok, r, _ = reserve_ingress(id_p, ip, [1] * 499)
check("P1 PLUS 499 files 通过", ok)
ok, r, _ = reserve_ingress(id_p, ip, [1])
check("P2 PLUS 第 500 个通过", ok)
ok, r, _ = reserve_ingress(id_p, ip, [1])
check("P3 PLUS 501 拒 err_quota_count", (not ok) and r == "err_quota_count", r)
_clear_usage(id_p)
ok, r, _ = reserve_ingress(id_p, ip, [PLUS_BYTES])
check("P4 PLUS 恰 5 GiB 通过", ok)
ok, r, _ = reserve_ingress(id_p, ip, [1])
check("P5 PLUS 5 GiB+1 拒 err_quota_size", (not ok) and r == "err_quota_size", r)

# ═══ 4. IP 不参与 quota ═══
u_i2, id_i2 = _new_user("enti")
_clear_usage(id_f); _clear_usage(id_i2)
ok1, _, _ = reserve_ingress(id_f, "198.51.100.7", [1] * FLIM)
ok2, _, _ = reserve_ingress(id_i2, "198.51.100.7", [1])
check("I1 同 IP 两用户 usage 相互独立 (Free 满, 另一用户第1个仍过)", ok1 and ok2)

# 测试自身不把 ip_usage_count 当 quota 依据 (静态守卫: 本文件零调用)
_self_src = open(_os.path.abspath(__file__), encoding="utf-8").read()
_probe = "ip_usage_" + "count("  # 动态拼字面量, 避免守卫行自匹配
check("I2 测试自身不调用 ip_usage_count 作 quota decision",
      _probe not in _self_src.replace('"' + "ip_usage_" + '" + "count("', ""))

# ═══ 5. Reservation semantics ═══
_clear_usage(id_f)
with db() as c:
    usage_repo.insert_many(c, id_f, ip, [1] * (FLIM - 1), int(time.time()))
results = []
def _race():
    okx, rx, _ = reserve_ingress(id_f, ip, [1])
    results.append((okx, rx))
t1 = threading.Thread(target=_race); t2 = threading.Thread(target=_race)
t1.start(); t2.start(); t1.join(); t2.join()
granted = sum(1 for okx, _ in results if okx)
check("S1 same-user 并发抢最后名额恰一个成功", granted == 1, str(results))

_clear_usage(id_f)
ok, r, res = reserve_ingress(id_f, ip, [1] * 10)
release_ingress(id_f, res)
ok2, r2, _ = reserve_ingress(id_f, ip, [1] * FLIM)
check("S2 release_ingress 恢复失败 reservation", ok and ok2)

_clear_usage(id_f)
ok, r, res = reserve_ingress(id_f, ip, [1] * 5)
_cur.execute("delete from uploads where user_id=%s", (id_f,))  # 普通 upload 删除
_cur.execute("select count(*) from upload_usage where user_id=%s", (id_f,))
left = _cur.fetchone()[0]
check("S3 upload 行删除不退款 24h usage (usage 仍 5)", left == 5, str(left))

# generated output 不新增 ingress usage: canonical 契约 = reserve 只挂在 source
# 接受路径 (api/pipeline 调用点), output 落盘不产生新 upload_usage。静态守卫:
_pipe = open(_os.path.join(_REPO, "core", "execution", "pipeline.py"), encoding="utf-8").read()
_api = open(_os.path.join(_REPO, "app", "routes", "api.py"), encoding="utf-8").read()
check("S4 reserve_ingress 只出现在 source-accept 路径 (pipeline+api 各一处)",
      _pipe.count("reserve_ingress(") == 1 and _api.count("reserve_ingress(") == 1)
# 引用既有 Stage C E2E: tests/canonical/test_invocation_canonical.py 全链 (upload→execute→output)
# 已证明一次调用 usage 不翻倍; 本文件不重复造 E2E。

# ═══ 6. Source-of-truth guard ═══
_up = open(_os.path.join(_REPO, "core", "sharing", "uploads.py"), encoding="utf-8").read()
check("G1 sharing/uploads.py 不出现 plus (不认识会员名)",
      "plus" not in _up)
check("G2 quota decision 无 err_quota_ip", "err_quota_ip" not in _up)
check("G3 quota decision 不调用 ip_usage_count", "ip_usage_count" not in _up)
check("G4 uploads.py 经 entitlements resolver 取限",
      "entitlements" in _up and "DAILY_UPLOADS" not in _up and "DAILY_BYTES" not in _up)

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
