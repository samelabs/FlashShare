#!/usr/bin/env python3
"""Ingress boundary + entitlement invariants — 平台硬边界 + 身份权益.

长期 baseline: 把 entitlement 映射 (24h ingress quota + delivery 值域)
与 authoritative enforcement 锁进 canonical 体系。遵循 canonical
fail-closed 规则: 必须显式提供 isolated port + isolated TEST_DB_URL,
禁止 production DB/port。业务契约断言, 不 snapshot 实现细节。
"""
import os as _os, sys as _sys
import sys
import json
import base64
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
from governance.entitlements import resolve, check_delivery, FREE, _PLANS
from governance.delivery import (validate_delivery, EXPIRY_DAYS,
                                 DOWNLOAD_LIMITS, INTERMEDIATE_TTL_DAYS,
                                 INTERMEDIATE_DOWNLOAD_LIMIT)
from economy.subscription import plan_of
from sharing.uploads import reserve_ingress, release_ingress, check_quota
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
check("R1 无 plan → Free (含 delivery 值域 = Free 档)",
      r["plan_code"] == "free"
      and r["ingress_files_24h"] == FREE["ingress_files_24h"]
      and r["ingress_bytes_24h"] == FREE["ingress_bytes_24h"]
      and r["expiry_days"] == (1, 3, 7)
      and r["download_limits"] == (1, 10, 50, 100, 200), str(r))

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
check("R3b member+plus → 500 files / 5 GiB / 全值域 delivery",
      r == {"plan_code": "plus", "ingress_files_24h": 500,
            "ingress_bytes_24h": PLUS_BYTES,
            "expiry_days": (1, 3, 7, 30),
            "download_limits": (1, 10, 50, 100, 200, 1000)}, str(r))

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

# ═══ 2. Free quota 边界 + 结构化 denial contract ═══
ip = "203.0.113.9"
FLIM = FREE["ingress_files_24h"]; BLIM = FREE["ingress_bytes_24h"]

# Free files: used = limit-2, incoming = 3 → 精确六字段
_clear_usage(id_f)
with db() as c:
    usage_repo.insert_many(c, id_f, ip, [1] * (FLIM - 2), int(time.time()))
ok, r, _ = reserve_ingress(id_f, ip, [1] * 3)
check("F3 Free files denial 精确结构 {error,limit,used,incoming,remaining,plan_code}",
      (not ok) and r == {"error": "err_ingress_files_24h", "limit": FLIM,
                         "used": FLIM - 2, "incoming": 3, "remaining": 2,
                         "plan_code": "free"}, str(r))
check("F3b incoming 不截断为 remaining (3 > 2 仍报 3)",
      (not ok) and r["incoming"] == 3 and r["remaining"] == 2)

# Free bytes: 同形 (单行大 size 即 sum 语义, 无需十亿行)
_clear_usage(id_f)
with db() as c:
    usage_repo.insert_many(c, id_f, ip, [BLIM - 100], int(time.time()))
ok, r, _ = reserve_ingress(id_f, ip, [101])
check("F5 Free bytes denial 精确结构",
      (not ok) and r == {"error": "err_ingress_bytes_24h", "limit": BLIM,
                         "used": BLIM - 100, "incoming": 101,
                         "remaining": 100, "plan_code": "free"}, str(r))

# 边界通过性 (既有行为不回归)
_clear_usage(id_f)
ok, r, _ = reserve_ingress(id_f, ip, [1] * (FLIM - 1))
check("F1 Free limit-1 files 通过", ok)
ok, r, _ = reserve_ingress(id_f, ip, [1])
check("F2 Free 第 limit 个通过", ok)
_clear_usage(id_f)
ok, r, _ = reserve_ingress(id_f, ip, [BLIM])
check("F4 Free 恰 bytes-limit 通过", ok)

# ═══ 3. PLUS quota 边界 (结构值来自 effective entitlement, 非 Free 常量) ═══
PLIM = _PLANS["plus"]["ingress_files_24h"]; PBLIM = _PLANS["plus"]["ingress_bytes_24h"]
_clear_usage(id_p)
with db() as c:
    usage_repo.insert_many(c, id_p, ip, [1] * (PLIM - 2), int(time.time()))
ok, r, _ = reserve_ingress(id_p, ip, [1] * 3)
check("P3 PLUS files denial: plan_code=plus, limit=500 (effective entitlement)",
      (not ok) and r["error"] == "err_ingress_files_24h" and r["plan_code"] == "plus"
      and r["limit"] == 500 and r["used"] == 498 and r["incoming"] == 3
      and r["remaining"] == 2, str(r))
_clear_usage(id_p)
with db() as c:
    usage_repo.insert_many(c, id_p, ip, [PBLIM - 50], int(time.time()))
ok, r, _ = reserve_ingress(id_p, ip, [51])
check("P5 PLUS bytes denial: limit=5GiB, plan_code=plus",
      (not ok) and r["error"] == "err_ingress_bytes_24h"
      and r["limit"] == PLUS_BYTES and r["plan_code"] == "plus"
      and r["remaining"] == 50, str(r))
_clear_usage(id_p)
ok, r, _ = reserve_ingress(id_p, ip, [1] * (PLIM - 1))
check("P1 PLUS limit-1 files 通过", ok)
ok, r, _ = reserve_ingress(id_p, ip, [1])
check("P2 PLUS 第 500 个通过", ok)
_clear_usage(id_p)
ok, r, _ = reserve_ingress(id_p, ip, [PLUS_BYTES])
check("P4 PLUS 恰 5 GiB 通过", ok)

# check/reserve 同义: 同 snapshot + incoming → denial 字段完全一致
_clear_usage(id_f)
with db() as c:
    usage_repo.insert_many(c, id_f, ip, [1] * (FLIM - 2), int(time.time()))
ok1, d1 = check_quota(id_f, ip, 1, incoming_count=3)
ok2, d2, _ = reserve_ingress(id_f, ip, [1] * 3)
check("Q1 check_quota denial == reserve_ingress denial (逐字段)",
      (not ok1) and (not ok2) and d1 == d2, f"check={d1} reserve={d2}")

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
_s1_denials = [d for okx, d in results if not okx]
check("S1b 失败线程收到 structured quota denial",
      len(_s1_denials) == 1 and _s1_denials[0].get("error") == "err_ingress_files_24h"
      and "limit" in _s1_denials[0] and "remaining" in _s1_denials[0], str(_s1_denials))

_clear_usage(id_f)
ok, r, res = reserve_ingress(id_f, ip, [1] * 10)
release_ingress(id_f, res)
ok2, r2, _ = reserve_ingress(id_f, ip, [1] * FLIM)
check("S2 release_ingress 恢复失败 reservation", ok and ok2 and r2 is None)

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

# ═══ 5b. Delivery entitlement 两层契约 ═══
# platform protocol validity (validate_delivery) vs user entitlement (check_delivery) 分离

def _proto_ok(ed, dl):
    try:
        validate_delivery({"expiry_days": ed, "allowed_downloads": dl})
        return True
    except ValueError:
        return False

check("D-P1 validate(30/1000) protocol PASS (平台枚举已扩展)",
      _proto_ok(30, 1000) and _proto_ok(1, 1000))
check("D-P2 validate(5/999) protocol FAIL",
      not _proto_ok(5, 50) and not _proto_ok(7, 999))
check("D-P3 平台枚举 = (1,3,7,30) / (1,10,50,100,200,1000)",
      EXPIRY_DAYS == (1, 3, 7, 30) and DOWNLOAD_LIMITS == (1, 10, 50, 100, 200, 1000))
check("D-P4 intermediate custody 固定 1 day / 10 downloads (平台策略非权益)",
      INTERMEDIATE_TTL_DAYS == 1 and INTERMEDIATE_DOWNLOAD_LIMIT == 10)

rf = resolve(id_f)
rp = resolve(id_p)
rx = resolve(id_x)
check("D-E1 Free delivery 值域 = 显式 (1,3,7)+(1,10,50,100,200)",
      rf["expiry_days"] == (1, 3, 7)
      and rf["download_limits"] == (1, 10, 50, 100, 200), str(rf))
check("D-E2 PLUS delivery 值域 = 显式 (1,3,7,30)+(1,10,50,100,200,1000)",
      rp["expiry_days"] == (1, 3, 7, 30)
      and rp["download_limits"] == (1, 10, 50, 100, 200, 1000), str(rp))
check("D-E3 未知 plan → Free delivery 值域", rx["expiry_days"] == (1, 3, 7)
      and rx["download_limits"] == (1, 10, 50, 100, 200), str(rx))

# Entitlement 显式声明 guard: 不得从 platform enum 推导
ent_src = open(_os.path.join(_REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_defs = ent_src.split('def _validate_against_protocol')[0]  # 定义区 (FREE/_PLANS)
check("D-X1 entitlement 源无枚举推导 (comprehension/filter/slice)",
      "tuple(d for d in" not in _defs
      and "for d in EXPIRY_DAYS" not in _defs
      and "for d in DOWNLOAD_LIMITS" not in _defs
      and "EXPIRY_DAYS[:-" not in _defs and "DOWNLOAD_LIMITS[:-" not in _defs
      and "EXPIRY_DAYS[" not in _defs and "DOWNLOAD_LIMITS[" not in _defs)
check("D-X2 plan 定义不直接赋值 platform 全集",
      '"expiry_days": EXPIRY_DAYS' not in _defs
      and '"download_limits": DOWNLOAD_LIMITS' not in _defs)
check("D-X3 fail-closed protocol-subset 校验存在 (显式→被支持, 不反向生成)",
      "_validate_against_protocol" in ent_src
      and "ConfigurationError" in ent_src)
# 未来协议扩容 ≠ 自动授权 (独立构造验证, 不 monkeypatch production):
from governance.entitlements import _validate_against_protocol, ConfigurationError
_fut = dict(FREE)
_fut["expiry_days"] = FREE["expiry_days"] + (60,)      # 假设平台未来加了 60 天
_fut["download_limits"] = FREE["download_limits"] + (5000,)
try:
    _validate_against_protocol(_fut, "future-spec")
    check("D-X4 协议未支持的 entitlement 值 → ConfigurationError", False, "no raise")
except ConfigurationError:
    check("D-X4 协议未支持的 entitlement 值 → ConfigurationError (fail closed)", True)
# 若平台真扩了 enum, 既有 entitlement 档不变 (不自动获得):
check("D-X5 Free/PLUS 声明独立于 protocol 全集 (重叠但非派生)",
      FREE["expiry_days"] != EXPIRY_DAYS      # Free ⊂ 全集
      and _PLANS["plus"]["expiry_days"] == EXPIRY_DAYS  # PLUS 恰等, 但显式字面声明 (D-X2 锁)
      and set(FREE["download_limits"]) < set(DOWNLOAD_LIMITS))
# 所有 entitlement 值 ⊆ platform enum (模块 import 成功即已过 fail-closed;
# 此处显式断言一次):
check("D-X6 全部 entitlement 值 ⊆ platform enum",
      set(FREE["expiry_days"]) <= set(EXPIRY_DAYS)
      and set(FREE["download_limits"]) <= set(DOWNLOAD_LIMITS)
      and set(_PLANS["plus"]["expiry_days"]) <= set(EXPIRY_DAYS)
      and set(_PLANS["plus"]["download_limits"]) <= set(DOWNLOAD_LIMITS))
# resolver 契约完整形状:
check("D-E4 resolve 返回五键 (ingress×2 + delivery×2 + plan_code)",
      set(rf) == {"plan_code", "ingress_files_24h", "ingress_bytes_24h",
                  "expiry_days", "download_limits"}, str(sorted(rf)))

dn = check_delivery(id_f, {"expiry_days": 30, "allowed_downloads": 200})
check("D-C1 Free 30 → 精确 err_expiry_not_entitled + 上下文",
      dn and dn["error"] == "err_expiry_not_entitled" and dn["field"] == "expiry_days"
      and dn["requested"] == 30 and dn["allowed"] == [1, 3, 7] and dn["plan_code"] == "free", str(dn))
dn = check_delivery(id_f, {"expiry_days": 7, "allowed_downloads": 1000})
check("D-C2 Free 1000 → err_download_limit_not_entitled + 上下文",
      dn and dn["error"] == "err_download_limit_not_entitled"
      and dn["field"] == "allowed_downloads" and dn["requested"] == 1000
      and dn["allowed"] == [1, 10, 50, 100, 200], str(dn))
check("D-C3 PLUS 30/1000 → None (有权)",
      check_delivery(id_p, {"expiry_days": 30, "allowed_downloads": 1000}) is None)
check("D-C4 Free 7/200 → None (有权)",
      check_delivery(id_f, {"expiry_days": 7, "allowed_downloads": 200}) is None)
# check_delivery 不自读 plan_of (静态: 源码只 import resolve)
_ent_src = open(_os.path.join(_REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_check_fn = _ent_src.split("def check_delivery")[1]
_import_face = [l for l in _check_fn.split("\n") if l.startswith(("import ", "from "))]
check("D-C5 check_delivery 只消费 resolve() 不直读 plan_of()",
      not _import_face and "resolve(user_id)" in _check_fn)

# ═══ 5c. Authoritative enforcement (HTTP, 与 service 同栈) ═══
import http.client as _hc
_HOST = ("127.0.0.1", _TEST_PORT_INT)

def _login(username):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    out = b""
    for k, v in {"login": username, "password": "Test12345"}.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    b, ct = out + b"--B--\r\n", "multipart/form-data; boundary=B"
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read(); ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close(); return ck

def _execute(ck, body):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    conn.request("POST", "/api/tools/execute", json.dumps(body),
                 {"Content-Type": "application/json", "Cookie": ck})
    r = conn.getresponse(); d = json.loads(r.read()); conn.close()
    return r.status, d

def _share(ck, ed, dl):
    import base64 as _b64
    png = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    body = b""
    for k, v in {"tool": "share", "expiryDays": str(ed), "allowedDownloads": str(dl)}.items():
        body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += b'--B\r\nContent-Disposition: form-data; name="file"; filename="d.png\r\nContent-Type: image/png\r\n\r\n' + png + b"\r\n--B--\r\n"
    conn.request("POST", "/api/uploads", body,
                 {"Content-Type": "multipart/form-data; boundary=B", "Cookie": ck})
    r = conn.getresponse(); d = json.loads(r.read()); conn.close()
    return r.status, d

def _register(username):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    out = b""
    for k, v in {"username": username, "password": "Test12345",
                 "password_confirm": "Test12345", "email": f"{username}@t.co"}.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    b, ct = out + b"--B--\r\n", "multipart/form-data; boundary=B"
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read(); conn.close()
    _cur.execute("select id from users where username=%s", (username,))
    return username, _cur.fetchone()[0]

_suffix = str(int(time.time()))[-5:]
_hf_name, _hf_id2 = _register("delfree" + _suffix)
_hp_name, _hp_id2 = _register("delplus" + _suffix)
_seed_plan(_hp_id2, "plus", "active", int(time.time()) + 30 * 86400)
assert resolve(_hf_id2)["plan_code"] == "free" and resolve(_hp_id2)["plan_code"] == "plus"
_ckf = _login(_hf_name); _ckp = _login(_hp_name)
def _inv(ed, dl):
    return {"tool_id": "share", "source": {"files": [{"public_id": "nonexistent", "source_token": ""}]},
            "params": {}, "delivery": {"expiry_days": ed, "allowed_downloads": dl}, "client_ref": None}

st, d = _execute(_ckf, _inv(5, 50))
check("H1 Free execute expiry=5 → 422 invalid_request (protocol 面)",
      st == 422 and d.get("error") == "invalid_request", f"{st} {d.get('error')}")
st, d = _execute(_ckf, _inv(30, 200))
check("H2 Free execute 30 → 403 err_expiry_not_entitled (entitlement 面)",
      st == 403 and d.get("error") == "err_expiry_not_entitled"
      and d.get("field") == "expiry_days" and d.get("plan_code") == "free", f"{st} {d.get('error')}")
st, d = _execute(_ckf, _inv(7, 1000))
check("H3 Free execute 1000 → 403 err_download_limit_not_entitled",
      st == 403 and d.get("error") == "err_download_limit_not_entitled", f"{st}")
st, d = _execute(_ckp, _inv(30, 1000))
check("H4 PLUS execute 30/1000 → 非 403/422 (entitlement 放行)",
      st not in (403, 422), f"{st} {d.get('error')}")
st, d = _share(_ckf, 30, 200)
check("H5 Free direct share 30 → 403 (storage 前拒)",
      st == 403 and d.get("error") == "err_expiry_not_entitled", f"{st}")
st, d = _share(_ckf, 7, 1000)
check("H6 Free direct share 1000 → 403 (storage 前拒)",
      st == 403 and d.get("error") == "err_download_limit_not_entitled", f"{st}")
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
_all_usage = _cur.fetchone()[0]
st, d = _share(_ckf, 30, 200)  # 再拒一次
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
check("H7 被拒 direct share 不产生 upload_usage",
      _cur.fetchone()[0] == _all_usage, f"{_all_usage}")

# ═══ 5d. Platform ingress hard boundaries (20/50/100) ═══
from governance.ingress import validate_batch, MAX_FILES_PER_REQUEST
from config import MAX_UPLOAD_BYTES as _CFG_MAX_UP, MAX_BATCH_BYTES as _CFG_MAX_BAT

check("B-S1 validate_batch 20 files PASS (读实际 config)",
      validate_batch([1] * MAX_FILES_PER_REQUEST) is None)
d = validate_batch([1] * (MAX_FILES_PER_REQUEST + 1))
check("B-S2 21 files → err_too_many_files {limit:20, incoming:21}",
      d == {"error": "err_too_many_files", "limit": 20, "incoming": 21}, str(d))
check("B-S3 恰 MAX_UPLOAD_BYTES PASS (env 可覆盖, 不写死)",
      validate_batch([_CFG_MAX_UP]) is None)
d = validate_batch([_CFG_MAX_UP + 1])
check("B-S4 单文件 +1 → err_file_too_large + limit/incoming",
      d and d["error"] == "err_file_too_large" and d["limit"] == _CFG_MAX_UP
      and d["incoming"] == _CFG_MAX_UP + 1, str(d))
check("B-S5 恰 MAX_BATCH_BYTES PASS",
      validate_batch([_CFG_MAX_UP, _CFG_MAX_BAT - _CFG_MAX_UP]) is None)
d = validate_batch([_CFG_MAX_UP, _CFG_MAX_BAT - _CFG_MAX_UP, 1])
check("B-S6 batch +1 → err_batch_too_large (不冒充 file_too_large; 每项 ≤ 单限)",
      d and d["error"] == "err_batch_too_large" and d["limit"] == _CFG_MAX_BAT
      and d["incoming"] == _CFG_MAX_BAT + 1, str(d))

# invocation._MAX_FILES 与 shared 常量同一来源:
import execution.invocation as _inv_mod
check("B-S7 invocation files 上限 = shared MAX_FILES_PER_REQUEST",
      _inv_mod._MAX_FILES == MAX_FILES_PER_REQUEST == 20)

# ═══ 5e. Web / REST / MCP 三面 hard-boundary 实测 (低限由环境设定或用真实边界单点文件) ═══
# 单文件越界构造: max(1, MAX_UPLOAD+1) bytes 的最小 multipart — 不实际传 50MB:
def _web_upload(ck, size, count=1, name="b.png"):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    body = b""
    for k, v in {"tool": "share"}.items():
        body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for i in range(count):
        body += f'--B\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n'.encode() + (b"x" * 0) + b"\r\n"
    body += b"--B--\r\n"
    # size 字节计数无法伪造 — 用 21 个真文件测 files 边界 (字节边界走 REST 单元):
    conn.request("POST", "/api/uploads", body,
                 {"Content-Type": "multipart/form-data; boundary=B", "Cookie": ck})
    r = conn.getresponse(); raw = r.read(); conn.close()
    try: return r.status, json.loads(raw)
    except Exception: return r.status, {"raw": raw[:80].decode(errors="replace")}

# Web 21 files (真实小文件):
conn = _hc.HTTPConnection(*_HOST, timeout=30)
body = b""
for k, v in {"tool": "share"}.items():
    body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
_png1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
for i in range(21):
    body += f'--B\r\nContent-Disposition: form-data; name="file"; filename="f{i}.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + _png1 + b"\r\n"
body += b"--B--\r\n"
conn.request("POST", "/api/uploads", body,
             {"Content-Type": "multipart/form-data; boundary=B", "Cookie": _ckf})
r = conn.getresponse(); d = json.loads(r.read()); conn.close()
check("B-W1 Web 21 files → 400 err_too_many_files {limit:20, incoming:21}",
      r.status == 400 and d.get("error") == "err_too_many_files"
      and d.get("limit") == 20 and d.get("incoming") == 21, f"{r.status} {str(d)[:100]}")
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
check("B-W2 拒绝后 upload_usage 零新增", _cur.fetchone()[0] == _all_usage)

# REST oversized single: v1 的 declared/body 双检查均调 validate_batch (源 guard G12);
# 行为面经共享 pipeline 单测覆盖 (B-M2 同函数), HTTP 面等价。
_apiv1 = open(_os.path.join(_REPO, "app", "routes", "apiv1.py"), encoding="utf-8").read()
check("B-R1 REST v1 经 shared validate_batch (import + declared + body)",
      _apiv1.count("validate_batch") >= 3
      and "MAX_UPLOAD_BYTES" not in _apiv1)

# MCP: 需要 bearer token — 注册用户 + token? MCP 走 /mcp (bearer). 简化: 直接调 pipeline 层 (MCP 同一函数):
from execution.pipeline import upload_sources as _us, IngressBoundaryError as _IBE
_u = {"id": _hf_id2, "username": _hf_name}
try:
    _us(_u, [("a.png", "image/png", _png1)] * 21, "127.0.0.1")
    check("B-M1 pipeline 21 files → IngressBoundaryError (storage 前)", False, "no raise")
except _IBE as e:
    check("B-M1 pipeline 21 files → IngressBoundaryError (storage 前, MCP 同路径)",
          e.denial == {"error": "err_too_many_files", "limit": 20, "incoming": 21}, str(e.denial))
_big = b"\x89PNG\r\n\x1a\n" + b"0" * (_CFG_MAX_UP + 1 - 8)   # png 魔数 + 填充
try:
    _us(_u, [("a.png", "image/png", _big)], "127.0.0.1")
    check("B-M2 pipeline oversized single → 拒", False, "no raise")
except _IBE as e:
    check("B-M2 pipeline oversized single → 拒 (storage 前)",
          e.denial["error"] == "err_file_too_large", str(e.denial))
_cur.execute("select count(*) from upload_usage uu join users u on u.id=uu.user_id where u.username=%s", (_hf_name,))
check("B-M3 被拒 batch 零 usage 残留 (storage 前)", _cur.fetchone()[0] == _all_usage)
# MCP 端到端 isError 语义 (bearer): 用 apiv1 token? MCP 走同 bearer — 走真实 /mcp:
# (token 创建入口属 identity 面, 此处以 pipeline 层契约 + dispatch 无 -32602 回归覆盖;
#  HTTP 层 -32602 分流逻辑见 mcp.py except IngressBoundaryError → isError result)

# ═══ 5f. 真实 /mcp HTTP E2E (create user → API bearer token → tools/call) ═══
_eu, _euid = _register("mcpe2e" + str(int(time.time()))[-6:])
_eck = _login(_eu)
conn = _hc.HTTPConnection(*_HOST, timeout=30)
conn.request("POST", "/api/tokens", json.dumps({"name": "canonical-e2e"}),
             {"Cookie": _eck, "Content-Type": "application/json"})
_tk = json.loads(conn.getresponse().read()); conn.close()
_bearer = (_tk.get("token") or {}).get("token") or ""
check("ME-P0 fixture: 真实 API token 创建成功", bool(_bearer),
      f"cookie={'yes' if _eck else 'NO'} resp={str(_tk)[:80]}")

def _mcp(arguments):
    _body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "share", "arguments": arguments}}
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    conn.request("POST", "/mcp", json.dumps(_body), {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {_bearer}"})
    r = conn.getresponse(); raw = r.read().decode(); conn.close()
    parsed = None
    for line in raw.split("\n"):
        if line.startswith("data:"):
            parsed = json.loads(line[5:]); break
    if parsed is None:
        parsed = json.loads(raw)
    return r.status, parsed

# A. hard boundary business error (真实 21 files)
_files21 = [{"b64": base64.b64encode(_png1).decode(), "filename": f"e{i}.png"}
            for i in range(MAX_FILES_PER_REQUEST + 1)]
st, d = _mcp({"files": _files21})
_res = d.get("result") or {}
_content = ((_res.get("content") or [{}])[0].get("text") or "")
_cd = json.loads(_content) if _content.startswith("{") else {}
check("ME-A1 MCP E2E 21 files → HTTP 200, 无 top-level jsonrpc error",
      st == 200 and "error" not in d, f"{st} {str(d.get('error'))[:60]}")
check("ME-A2 result.isError == true + err_too_many_files {limit:20, incoming:21}",
      _res.get("isError") is True and _cd.get("error") == "err_too_many_files"
      and _cd.get("limit") == 20 and _cd.get("incoming") == 21, _content[:90])
check("ME-A3 非 -32602 (业务错误/协议错误分层)",
      not (d.get("error") or {}).get("code") == -32602)

# B. malformed request 仍是 protocol error
st, d = _mcp({"files": [{"b64": "!!!not-base64!!!", "filename": "x.png"}]})
check("ME-B 非法 base64 → JSON-RPC -32602",
      (d.get("error") or {}).get("code") == -32602, str(d.get("error"))[:80])

# C. 真实 MCP rolling-quota denial: 填满 E2E 用户 usage 再 tools/call
_cur.execute("select id from users where username=%s", (_eu,))
_euid = _cur.fetchone()[0]
_cur.execute("delete from upload_usage where user_id=%s", (_euid,))
with db() as c:
    usage_repo.insert_many(c, _euid, "127.0.0.1", [1] * FREE["ingress_files_24h"], int(time.time()))
_files_q = [{"b64": base64.b64encode(_png1).decode(), "filename": "q.png"}]
st, d = _mcp({"files": _files_q})
_res = d.get("result") or {}
_content = ((_res.get("content") or [{}])[0].get("text") or "")
_qd = json.loads(_content) if _content.startswith("{") else {}
check("MQ-1 MCP quota denial: HTTP 200 + isError + 完整结构",
      st == 200 and _res.get("isError") is True
      and _qd.get("error") == "err_ingress_files_24h"
      and _qd.get("limit") == FREE["ingress_files_24h"]
      and _qd.get("used") == FREE["ingress_files_24h"]
      and _qd.get("incoming") == 1 and _qd.get("remaining") == 0
      and _qd.get("plan_code") == "free", _content[:110])
check("MQ-2 无 top-level JSON-RPC error (非 -32602)", "error" not in d)

# Web 真实: 同用户 (或新用户填满) → /api/uploads 429 六字段
_wu = "webq" + str(int(time.time()))[-6:]
_wu_id = None
conn = _hc.HTTPConnection(*_HOST, timeout=30)
_out = b""
for k, v in {"username": _wu, "password": "Test12345", "password_confirm": "Test12345", "email": f"{_wu}@t.co"}.items():
    _out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
b, ct = _out + b"--B--\r\n", "multipart/form-data; boundary=B"
conn.request("POST", "/auth/register", b, {"Content-Type": ct}); conn.getresponse().read(); conn.close()
_wck = _login(_wu)
_cur.execute("select id from users where username=%s", (_wu,))
_wu_id = _cur.fetchone()[0]
with db() as c:
    usage_repo.insert_many(c, _wu_id, "127.0.0.1", [1] * FREE["ingress_files_24h"], int(time.time()))
conn = _hc.HTTPConnection(*_HOST, timeout=30)
body = b""
for k, v in {"tool": "share"}.items():
    body += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
body += f'--B\r\nContent-Disposition: form-data; name="file"; filename="q.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + _png1 + b"\r\n--B--\r\n"
conn.request("POST", "/api/uploads", body, {"Content-Type": "multipart/form-data; boundary=B", "Cookie": _wck})
r = conn.getresponse(); wd = json.loads(r.read()); conn.close()
check("WQ-1 Web quota denial: 429 + 六字段完整",
      r.status == 429 and wd.get("error") == "err_ingress_files_24h"
      and wd.get("limit") == FREE["ingress_files_24h"]
      and wd.get("used") == FREE["ingress_files_24h"]
      and wd.get("incoming") == 1 and wd.get("remaining") == 0
      and wd.get("plan_code") == "free", f"{r.status} {str(wd)[:110]}")

# REST 真实: bearer token 上传 → 429 同结构
conn = _hc.HTTPConnection(*_HOST, timeout=30)
conn.request("POST", "/api/tokens", json.dumps({"name": "quota-e2e"}),
             {"Cookie": _wck, "Content-Type": "application/json"})
_wtk = json.loads(conn.getresponse().read()); conn.close()
_wbearer = (_wtk.get("token") or {}).get("token") or ""
conn = _hc.HTTPConnection(*_HOST, timeout=30)
conn.request("POST", "/api/v1/uploads", _png1, {
    "Content-Type": "image/png", "x-filename": "q.png",
    "Content-Length": str(len(_png1)), "Authorization": f"Bearer {_wbearer}"})
r = conn.getresponse(); rd = json.loads(r.read()); conn.close()
check("RQ-1 REST quota denial: 429 + 同一结构 contract",
      r.status == 429 and rd.get("error") == "err_ingress_files_24h"
      and rd.get("limit") == FREE["ingress_files_24h"]
      and rd.get("remaining") == 0 and rd.get("plan_code") == "free", f"{r.status} {str(rd)[:110]}")

# denial 无残留 (upload_usage 不增; Gokapi 面由 pipeline 顺序保证)
_cur.execute("select count(*) from upload_usage where user_id=%s", (_wu_id,))
check("NQ-1 quota denial 后 upload_usage 零新增",
      _cur.fetchone()[0] == FREE["ingress_files_24h"])

# ═══ 6. Source-of-truth guard ═══
_up = open(_os.path.join(_REPO, "core", "sharing", "uploads.py"), encoding="utf-8").read()
check("G1 sharing/uploads.py 不出现 plus (不认识会员名)",
      "plus" not in _up)
check("G2 quota decision 无 err_quota_ip", "err_quota_ip" not in _up)
check("G3 quota decision 不调用 ip_usage_count", "ip_usage_count" not in _up)
check("G4 uploads.py 经 entitlements resolver 取限",
      "entitlements" in _up and "DAILY_UPLOADS" not in _up and "DAILY_BYTES" not in _up)
_disp = open(_os.path.join(_REPO, "core", "execution", "dispatch.py"), encoding="utf-8").read()
_apir = open(_os.path.join(_REPO, "app", "routes", "api.py"), encoding="utf-8").read()
check("G5 dispatch 无 plus 业务判断, 经 check_delivery",
      "plus" not in _disp and "check_delivery" in _disp)
check("G6 api.py 无 plus 业务判断, direct share 经 check_delivery",
      "plus" not in _apir and "check_delivery" in _apir)
check("G7 validate_delivery 仍是唯一 protocol 单点 (无第二枚举)",
      _disp.count("EXPIRY_DAYS") == 0 and "EXPIRY_DAYS" not in _apir)
_inv_src = open(_os.path.join(_REPO, "core", "execution", "invocation.py"), encoding="utf-8").read()
check("G8 invocation 不再字面定义 _MAX_FILES = 20 (引 shared 常量)",
      "_MAX_FILES = 20" not in _inv_src
      and "MAX_FILES_PER_REQUEST" in _inv_src)
_pipe_src = open(_os.path.join(_REPO, "core", "execution", "pipeline.py"), encoding="utf-8").read()
check("G9 pipeline 调用 shared ingress validator",
      "validate_batch" in _pipe_src)
_mcp_src = open(_os.path.join(_REPO, "app", "routes", "mcp.py"), encoding="utf-8").read()
check("G10 MCP 不自建 batch byte sum 规则 (业务限制→isError)",
      "MAX_BATCH_BYTES" not in _mcp_src and "IngressBoundaryError" in _mcp_src)
check("G11 Web api.py 不维护第二份 authoritative 判定 (无 files>20 字面/无逐文件 sum 拒)",
      "len(uploads) > 20" not in _apir and "total_incoming" not in _apir
      and "validate_batch" in _apir)
_mcp_up = _mcp_src.split("infos = _upload_batch")[1].split("raw = {")[0] if "infos = _upload_batch" in _mcp_src else ""
check("G12 MCP upload 区保留 boundary/quota 专门分支 + generic Exception fallback",
      "except (IngressBoundaryError, IngressQuotaError)" in _mcp_up
      and _mcp_up.count("except Exception as exc") >= 1, "")
# G13: Web transport early-reject 与 authoritative validate_batch 输出同构
# (ok/error/limit/incoming 四键; incoming=declared transport bytes)
_er = _apir.split("Transport safety only")[1].split("pending = []")[0] if "Transport safety only" in _apir else ""
check("G13 Web early-reject 结构完整 {ok,error,limit,incoming} 且不早于 validate_batch 定义第二算法",
      '"error": "err_batch_too_large"' in _er and '"limit": MAX_BATCH_BYTES' in _er
      and '"incoming": declared' in _er and "MAX_BATCH_BYTES +" in _er
      and "validate_batch" in _apir)
_ups = open(_os.path.join(_REPO, "core", "sharing", "uploads.py"), encoding="utf-8").read()
check("G14 sharing/uploads.py 是 quota denial 唯一构造点 (_quota_denial 单点)",
      "_quota_denial" in _ups and "err_ingress_files_24h" not in _apir
      and "err_ingress_files_24h" not in _mcp_src, "")
_apiv1_src = open(_os.path.join(_REPO, "app", "routes", "apiv1.py"), encoding="utf-8").read()
def _code_lines(src):
    return "\n".join(l for l in src.split("\n")
                     if not l.strip().startswith("#") and "recomputation" not in l)
check("G15 三面不自行算 remaining (透传 denial, 排除注释)",
      "remaining" not in _code_lines(_apir)
      and "remaining" not in _code_lines(_apiv1_src)
      and "remaining" not in _code_lines(_mcp_src))
check("G16 旧 key 零 active hit (err_quota_count/size)",
      "err_quota_count" not in _apir + _apiv1_src + _mcp_src + _ups
      and "err_quota_size" not in _apir + _apiv1_src + _mcp_src + _ups)
check("G17 MCP IngressQuotaError → isError 分支存在 + generic fallback 仍在",
      "IngressQuotaError" in _mcp_src
      and _mcp_up.count("except Exception as exc") >= 1)
check("G18 pipeline quota 走 IngressQuotaError (非普通 ValueError)",
      "IngressQuotaError(denial)" in _pipe_src)

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
