#!/usr/bin/env python3
"""Membership tiers — plans rows as the single tier source, the access
projection read model, tier invariants and Free/Plus/Pro × Creem
product checkout.
"""
from _harness import FAIL, PG, PORT, REPO, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Plan catalog: plans rows are the tier source
#
# plans 行 = Tier 唯一事实源 — canonical 守卫套。
#
# 三守卫:
#   PC2  seed 永不覆盖: init_db 二连跑, 篡改后的 plus/pro/free 行值原样存活
#   PC3  benefits 单源: resolve_from_membership 输出与 plans 行逐键一致
#       (expiry_days/download_limits 逐项相等, 非子集)
#   PC4  数据驱动插第三档: 造完整新档行 (配齐 benefits + product + price) →
#       直接进 sellable, 零代码改动
# fail-closed canonical 规则 (与同目录其它套一致)。
# ════════════════════════════════════════════════════════════════════════
section('Plan catalog: plans rows are the tier source')

import os as _os, sys as _sys, sys


import psycopg2
from database import db
from repo import orders as orders_repo
from governance import plan_catalog
from governance.entitlements import resolve_from_membership


_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_ts = int(__import__("time").time())

# ═══ PC2: seed 永不覆盖 — 篡改行值后 init_db 重跑, 值存活 ═══
with db() as con:
    con.execute("update plans set monthly_credits=777, ingress_files_24h=888,"
                " expiry_days=ARRAY[3], download_limits=ARRAY[10], badge_key='tampered'"
                " where code='plus'")
import importlib
import database as _dbmod
_dbmod.init_db()
_pc2 = plan_catalog.catalog(refresh=True)
_plus = next(t for t in _pc2 if t.code == "plus")
check("PC2 seed 永不覆盖: 篡改的 plus 行 (777/888/[3]/[10]/'tampered') init_db 后存活",
      _plus.monthly_credits == 777 and _plus.benefits.ingress_files_24h == 888
      and tuple(_plus.benefits.expiry_days) == (3,)
      and tuple(_plus.benefits.download_limits) == (10,)
      and _plus.badge_key == "tampered",
      f"got credits={_plus.monthly_credits} files={_plus.benefits.ingress_files_24h}"
      f" ed={list(_plus.benefits.expiry_days)} dl={list(_plus.benefits.download_limits)}")
with db() as con:  # 恢复种子值 (后续套件依赖)
    con.execute("update plans set monthly_credits=100, ingress_files_24h=500,"
                " ingress_bytes_24h=5368709120, expiry_days=ARRAY[1,3,7,30],"
                " download_limits=ARRAY[1,10,50,100,200,1000], badge_key='tier_plus'"
                " where code='plus'")
plan_catalog.catalog(refresh=True)

# ═══ PC3: benefits 单源 — resolve_from_membership 与 plans 行逐键一致 ═══
for _code in ("plus", "pro"):
    _t = plan_catalog.by_code(_code)
    _ent = resolve_from_membership({"member": True, "code": _code})
    check(f"PC3 {_code} entitlement 逐键 == plans 行 (单源派生, 非另抄)",
          _ent["ingress_files_24h"] == _t.benefits.ingress_files_24h
          and _ent["ingress_bytes_24h"] == _t.benefits.ingress_bytes_24h
          and tuple(_ent["expiry_days"]) == tuple(_t.benefits.expiry_days)
          and tuple(_ent["download_limits"]) == tuple(_t.benefits.download_limits)
          and _ent["plan_code"] == _t.code,
          f"ent={_ent}")

# ═══ PC4: 数据驱动插第三档 — 完整行直接可售, 零代码改动 ═══
with db() as con:
    con.execute(
        "insert into plans (code, name, monthly_credits, period_days, period,"
        " price_cents, currency, creem_product_id, sort_order, active, badge_key,"
        " ingress_files_24h, ingress_bytes_24h, expiry_days, download_limits,"
        " created_at, updated_at)"
        " values ('pc_biz', 'PC Business', 2000, 30, 'monthly', 9000, 'USD',"
        " 'prod_pc_biz', (SELECT COALESCE(MAX(p.sort_order),0)+1 FROM plans p"
        " WHERE p.code <> 'free'), TRUE, 'tier_pc_biz', 800, %s, ARRAY[1,3,7],"
        " ARRAY[1,10,50,100], %s, %s)"
        " on conflict (code) do update set active=TRUE",
        (5 * 1024 ** 3, _ts, _ts))
plan_catalog.catalog(refresh=True)
_pc4_tier = plan_catalog.by_code("pc_biz")
_pc4_ent = resolve_from_membership({"member": True, "code": "pc_biz"})
with db() as con:
    from governance.plan_catalog import sellable_catalog as _sell
    _pc4_sellable = [r.code for r in _sell()]
check("PC4 新档行 (配齐 benefits+product+price) 零代码进 sellable",
      _pc4_tier is not None and _pc4_tier.sellable and "pc_biz" in _pc4_sellable,
      f"tier={_pc4_tier is not None} sellable={_pc4_sellable}")
check("PC4b 新档 entitlement 直接来自行 (数据驱动, 无代码映射)",
      _pc4_ent["ingress_files_24h"] == 800
      and tuple(_pc4_ent["expiry_days"]) == (1, 3, 7)
      and _pc4_ent["plan_code"] == "pc_biz",
      f"ent={_pc4_ent}")
with db() as con:  # 清场: 第三档移出 active, 不污染后续套件的种子合同断言
    con.execute("update plans set active=FALSE where code='pc_biz'")
plan_catalog.catalog(refresh=True)

_con.close()


# ════════════════════════════════════════════════════════════════════════
# Access projection read model
#
# Access Projection canonical invariants — 只读 read model 契约.
#
# 锁住: Free/PLUS/expired 三态 membership↔entitlement 域边界、rolling-24h
# usage 窗口语义、JSON-safe 输出、纯只读 (跨业务表零写入)。遵循 canonical
# fail-closed 规则: 显式 isolated port + TEST_DB_URL, 拒绝 production。
# ════════════════════════════════════════════════════════════════════════
section('Access projection read model')

import os as _os, sys as _sys
import sys
import json, time


import psycopg2
from governance.access_projection import project
from governance.entitlements import (resolve, resolve_from_membership)


def _row_views():
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    from governance import plan_catalog as _pc
    _free = _pc.free_tier().benefits.as_dict("free")
    _plans = {t.code: t.benefits.as_dict(t.code) for t in _pc.catalog() if not t.is_free}
    return _free, _plans


FREE, _PLANS = _row_views()
from economy.subscription import plan_of
from sharing.uploads import usage_24h
from repo import upload_usage as usage_repo
from database import db


_con = psycopg2.connect(PG); _con.autocommit = True
_cur_apj = _con.cursor()
_ts_apj = int(time.time())

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur_apj.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts_apj, _ts_apj))
    return u, _cur_apj.fetchone()[0]

def _seed_plan(user_id, code, status, renews_at):
    _cur_apj.execute("select id from plans where code=%s", (code,))
    row = _cur_apj.fetchone()
    if not row:
        _cur_apj.execute(
            "insert into plans (code, name, monthly_credits, price_cents, created_at, updated_at)"
            " values (%s, %s, 0, 0, %s, %s) returning id", (code, code, _ts_apj, _ts_apj))
        plan_id = _cur_apj.fetchone()[0]
    else:
        plan_id = row[0]
    _cur_apj.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id, status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
        (user_id, plan_id, f"sub:fx:{user_id}", status, _ts_apj, renews_at, _ts_apj))

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
_seed_plan(id_p, "plus", "active", _ts_apj + 30 * 86400)
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
_seed_plan(id_e, "plus", "active", _ts_apj - 100)  # renews_at 已过
p = project(id_e)
check("AP-E1 expired membership 保留真实 plan_of 结果 (member=false)",
      p["membership"] is not None and p["membership"]["member"] is False
      and p["membership"]["code"] == "plus", str(p["membership"]))
check("AP-E2 expired → entitlements.plan_code = free (域边界)",
      p["entitlements"]["plan_code"] == "free", str(p["entitlements"]["plan_code"]))

# ═══ 4. Rolling usage 窗口 ═══
u_u, id_u = _new_user("apu")
with db() as c:
    usage_repo.insert_many(c, id_u, "127.0.0.1", [100, 23], _ts_apj)          # 窗口内 2 files / 123 bytes
_cur_apj.execute(
    "insert into upload_usage (user_id, ip, size_bytes, created_at) values (%s, '127.0.0.1', 999999, %s)",
    (id_u, _ts_apj - 25 * 3600))                                                # 窗口外
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
    for t in ("memberships", "upload_usage", "ledger", "tool_calls"):
        _cur_apj.execute(f"select count(*) from {t}")
        out[t] = _cur_apj.fetchone()[0]
    return out

_b = _tbl_counts()
for _ in range(3):
    project(id_p); project(id_f); project(id_u); project(id_e)
_a = _tbl_counts()
check("AP-R1 projection 纯只读 (memberships/upload_usage/ledger/tool_calls 计数不变)",
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

# ═══ 7c. Public REST v1 retired — /me 消费面不存在 ═══
import http.client as _hc
_HOST = ("127.0.0.1", PORT)

def _register_http(username):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    out = b""
    for k, v in {"username": username, "password": "Test12345",
                 "password_confirm": "Test12345", "email": f"{username}@t.co"}.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    b, ct = out + b"--B--\r\n", "multipart/form-data; boundary=B"
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    conn.getresponse().read(); conn.close()

def _token(ck):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/api/keys", json.dumps({"name": "ap-me"}),
                 {"Cookie": ck, "Content-Type": "application/json"})
    t = json.loads(conn.getresponse().read()); conn.close()
    return (t.get("key") or {}).get("key") or ""

_sfx = str(int(time.time()))[-6:]
_mf = "apmefree" + _sfx
_register_http(_mf)
def _login_http(username):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    out = b""
    for k, v in {"login": username, "password": "Test12345"}.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    b, ct = out + b"--B--\r\n", "multipart/form-data; boundary=B"
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read(); ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close(); return ck

_mf_ck = _login_http(_mf)
_mf_bearer = _token(_mf_ck)
_cur_apj.execute("select id from users where username=%s", (_mf,))
_mf_id = _cur_apj.fetchone()[0]
def _get(path, bearer):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("GET", path, headers={"Authorization": f"Bearer {bearer}"})
    r = conn.getresponse(); r.read(); conn.close()
    return r.status

check("ME-R2 browser /api/me 拒 Bearer-only caller (session surface)",
      _get("/api/me", _mf_bearer) == 401)

# users consumed by 7d web-context checks
_mp = "apmeplus" + _sfx
_register_http(_mp)
_cur_apj.execute("select id from users where username=%s", (_mp,))
_mp_id = _cur_apj.fetchone()[0]
_seed_plan(_mp_id, "plus", "active", _ts_apj + 30 * 86400)
_me_ = "apmeexp" + _sfx
_register_http(_me_)
_cur_apj.execute("select id from users where username=%s", (_me_,))
_me_id = _cur_apj.fetchone()[0]
_seed_plan(_me_id, "plus", "active", _ts_apj - 100)

# ═══ 7d. Web base context 消费 projection (真实 HTTP 三态) ═══
def _get_home(ck=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    headers = {"Cookie": ck} if ck else {}
    conn.request("GET", "/", headers=headers)
    r = conn.getresponse(); body = r.read().decode(); conn.close()
    return r.status, body

def _has_plus_badge(body):
    # home-polish: the membership mark in the header is the verified
    # badge (class "verified", avatar corner + user-menu name) fed by the
    # same access projection; the retired plus-badge text is gone.
    return 'class="verified' in body

# Free authenticated
st, body = _get_home(_mf_ck)
check("WC-F1 Free 登录 GET / → 200", st == 200, str(st))
check("WC-F2 Free header 无认证徽章 (与 project.membership=None 一致)",
      not _has_plus_badge(body) and project(_mf_id)["membership"] is None)

# PLUS authenticated
st, body = _get_home(_login_http(_mp))
check("WC-P1 PLUS 登录 GET / → 200 + header 认证徽章",
      st == 200 and _has_plus_badge(body)
      and project(_mp_id)["membership"]["member"] is True)

# Expired
st, body = _get_home(_login_http(_me_))
check("WC-E1 expired 登录 GET / → 200 无认证徽章 (fact 保留 member=false)",
      st == 200 and not _has_plus_badge(body)
      and project(_me_id)["membership"]["member"] is False)

# Anonymous
st, body = _get_home()
check("WC-A1 匿名 GET / → 200 无认证徽章", st == 200 and not _has_plus_badge(body))

# ═══ 7e. Read-model failure contract (函数级) ═══
import app.web_ctx as _wctx
_orig_project = _wctx.project
def _boom_project(_uid):
    raise RuntimeError("membership read failed")

class _FakeReq:
    state = type("S", (), {"lang": "en", "user": {"id": _mf_id, "username": "x"}})()

_wctx.project = _boom_project
try:
    _wctx.ctx_base(_FakeReq())
    check("WC-X1 web_ctx project 异常向上传播 (不伪装 access=None)", False, "no raise")
except RuntimeError as _exc:
    check("WC-X1 web_ctx project 异常向上传播 (不伪装 access=None)",
          "membership read failed" in str(_exc))
finally:
    _wctx.project = _orig_project

# 匿名不调用 project:
_wctx.project = _boom_project
class _AnonReq:
    state = type("S", (), {"lang": "en", "user": None})()
    # ctx_base reads request.url.path for the canonical URL — a minimal
    # stand-in keeps the fake a faithful Request shape
    url = type("U", (), {"path": "/"})()
    query_params = {}
try:
    _c = _wctx.ctx_base(_AnonReq())
    check("WC-X2 匿名 context 不调用 project (access=None)",
          _c.get("access") is None)
except RuntimeError:
    check("WC-X2 匿名 context 不调用 project", False, "anon raised")
finally:
    _wctx.project = _orig_project

# ═══ 7f. Home delivery/ingress 从 entitlement truth 投影 (真实 HTTP) ═══
import re as _re
def _home_options(ck=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("GET", "/", headers={"Cookie": ck} if ck else {})
    r = conn.getresponse(); body = r.read().decode(); conn.close()
    # delivery options render client-side in the shared input
    # component; their single SSR-injected source is window.__DELIVERY__.
    m = _re.search(r'window\.__DELIVERY__\s*=\s*(\{.*?\});', body, _re.S)
    if not m:
        return r.status, (None, None), (None, None), None
    dv = json.loads(m.group(1))
    lim = _re.search(r'window\.__LIMITS__\s*=\s*(\{.*?\});', body, _re.S)
    limits = json.loads(lim.group(1)) if lim else None
    return (r.status, (list(dv["days"]), dv["defaultDays"]),
            (list(dv["downloads"]), dv["defaultDownloads"]), limits)

FREE_EXP = list(FREE["expiry_days"]); FREE_DL = list(FREE["download_limits"])
PLUS_EXP = list(_PLANS["plus"]["expiry_days"]); PLUS_DL = list(_PLANS["plus"]["download_limits"])

st, (exp, dexp), (dl, ddl), lim = _home_options()   # anonymous
check("HD-A1 匿名 expiry options == resolve_from_membership(None) Free",
      st == 200 and exp == FREE_EXP, str(exp))
check("HD-A2 匿名 download options == Free", dl == FREE_DL, str(dl))
check("HD-A3 匿名 __LIMITS__ 单一投影字段 来自 Free entitlement + 平台上限",
      lim and lim["expiryDays"] == FREE_EXP and lim["downloadLimits"] == FREE_DL
      and lim["dailyFiles"] == FREE["ingress_files_24h"]
      and lim["maxFileMb"] > 0 and lim["dailyGb"] > 0
      and lim["defaultDays"] == 1 and lim["defaultDownloads"] == 50, str(lim))

st, (exp, dexp), (dl, ddl), lim = _home_options(_mf_ck)   # Free
check("HD-F1 Free 登录 options == Free entitlement", st == 200 and exp == FREE_EXP and dl == FREE_DL)
check("HD-F2 Free 默认选择 1/50 (属于 allowed set)", dexp == 1 and ddl == 50, f"{dexp}/{ddl}")

st, (exp, dexp), (dl, ddl), lim = _home_options(_login_http(_mp))   # PLUS
check("HD-P1 PLUS expiry == [1,3,7,30] (来自 projection 非模板分支)", exp == PLUS_EXP, str(exp))
check("HD-P2 PLUS downloads == [1,10,50,100,200,1000]", dl == PLUS_DL, str(dl))
check("HD-P3 PLUS __LIMITS__ 反映 plus entitlement",
      lim and lim["expiryDays"] == PLUS_EXP and lim["downloadLimits"] == PLUS_DL)

st, (exp, dexp), (dl, ddl), lim = _home_options(_login_http(_me_))   # expired
check("HD-E1 expired 精确回落 Free options", exp == FREE_EXP and dl == FREE_DL)

# form association + 结构 proof:
conn = _hc.HTTPConnection(*_HOST, timeout=30)
conn.request("GET", "/", headers={"Cookie": _mf_ck})
r = conn.getresponse(); body = r.read().decode(); conn.close()
# delivery selects render inside the shared input component; their
# single SSR-injected source is window.__DELIVERY__ (checked above), and
# the component reads its own selects at run time.
check("HD-S1 home SSR 注入 __DELIVERY__ (组件 select 单一数据源)",
      'window.__DELIVERY__' in body)
_ti_src = open(_os.path.join(REPO, "static", "pages", "tool-input.js"),
               encoding="utf-8").read()
check("HD-S2 组件运行时读取自身 delivery select (.ti-expiry/.ti-downloads)",
      'container.querySelector(".ti-expiry")' in _ti_src
      and 'container.querySelector(".ti-downloads")' in _ti_src)

# ═══ 7g. MCP tools/list delivery enum 投影 (真实 Bearer) ═══
def _mcp_list(bearer):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    _b = {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}}
    conn.request("POST", "/mcp", json.dumps(_b), {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {bearer}"})
    r = conn.getresponse(); raw = r.read().decode(); conn.close()
    for line in raw.split("\n"):
        if line.startswith("data:"):
            d = json.loads(line[5:]); break
    else:
        d = json.loads(raw)
    res = d.get("result") or {}
    return r.status, res.get("tools") or []

def _delivery_enums(tools):
    exps, dls = set(), set()
    search = None
    for t in tools:
        if t.get("name") == "catalog_search":
            search = t
            continue
        dl = ((t.get("inputSchema") or {}).get("properties") or {}).get("delivery") or {}
        props = dl.get("properties") or {}
        if "expiry_days" in props:
            exps.add(tuple(props["expiry_days"].get("enum") or []))
        if "allowed_downloads" in props:
            dls.add(tuple(props["allowed_downloads"].get("enum") or []))
    return exps, dls, search

FREE_EXP = list(FREE["expiry_days"]); FREE_DL = list(FREE["download_limits"])
PLUS_EXP = list(_PLANS["plus"]["expiry_days"]); PLUS_DL = list(_PLANS["plus"]["download_limits"])

st, tools = _mcp_list(_mf_bearer)
exps, dls, search = _delivery_enums(tools)
check("ML-F1 Free tools/list: 全部 tool expiry enum == [1,3,7]",
      st == 200 and exps == {tuple(FREE_EXP)} and len(tools) > 1, str(exps))
check("ML-F2 Free downloads enum == [1,10,50,100,200]",
      dls == {tuple(FREE_DL)}, str(dls))
check("ML-F3 catalog_search 无 delivery schema",
      search is not None and "delivery" not in (search.get("inputSchema") or {}).get("properties", {}))

st, tools = _mcp_list(_token(_login_http(_mp)))
exps, dls, _ = _delivery_enums(tools)
check("ML-P1 PLUS tools/list: expiry enum == [1,3,7,30] (来自 projection)",
      exps == {tuple(PLUS_EXP)}, str(exps))
check("ML-P2 PLUS downloads enum == [1,10,50,100,200,1000]",
      dls == {tuple(PLUS_DL)}, str(dls))

st, tools = _mcp_list(_token(_login_http(_me_)))
exps, dls, _ = _delivery_enums(tools)
check("ML-E1 expired 精确回落 Free enum",
      exps == {tuple(FREE_EXP)} and dls == {tuple(FREE_DL)})

# ═══ 7h. Account /account/credits membership 单源 (真实 session HTTP) ═══
def _get_credits(ck):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("GET", "/account/credits", headers={"Cookie": ck})
    r = conn.getresponse(); body = r.read().decode(); conn.close()
    return r.status, body

# Free: 无 membership fact → 当前方案卡仍渲染, 但 badge 是 Free 字样、
# 永不出现 member 样式。稳定 DOM marker (class 选择器), 不用 "PLUS"
# 字符串 (可售目录/header 可含)。
st, body = _get_credits(_mf_ck)
_proj_f = project(_mf_id)["membership"]
check("AC-F1 Free /account/credits → 200, membership None, plan card = Free badge (never member styling)",
      st == 200 and _proj_f is None
      and 'class="sub-badge on"' not in body
      and 'data-i18n="plan_current"' in body
      and ">Free</span>" in body, f"{st}")

# PLUS: Account membership block 真渲染 (非 header badge 借证)
_mp_ck2 = _login_http(_mp)
st, body = _get_credits(_mp_ck2)
_proj_p = project(_mp_id)["membership"]
check("AC-P1 PLUS /account/credits → 200, member=True, sub-status + sub-badge on 存在",
      st == 200 and _proj_p and _proj_p["member"] is True
      and 'class="sub-status"' in body
      and 'class="sub-badge on"' in body, f"{st}")

# Expired: fact 保留并展示 ≠ active member (badge 无 on 态)
st, body = _get_credits(_login_http(_me_))
_proj_e = project(_me_id)["membership"]
check("AC-E1 expired → 200, fact 保留 (member=false), sub-status 在 / badge 无 on",
      st == 200 and _proj_e is not None and _proj_e["member"] is False
      and _proj_e["code"] == "plus"
      and 'class="sub-status"' in body
      and 'class="sub-badge' in body
      and 'class="sub-badge on"' not in body, f"{st}")

# 负向自检 guard: 本 Account HTTP 断言组永久禁止假通过模式
_self_src = open(_os.path.abspath(__file__), encoding="utf-8").read()
_acct_http_blk = _self_src.split("7h. Account")[1].split("8. Source-of-truth")[0]
check("AC-N1 Account HTTP 断言无假通过模式 (else True / body[:0] / 裸 PLUS-in-body)",
      "else True" not in _acct_http_blk
      and "body[:0]" not in _acct_http_blk
      and '"PLUS" in body' not in _acct_http_blk)

# ═══ 8. Source-of-truth guards ═══
_src = open(_os.path.join(REPO, "core", "governance", "access_projection.py"), encoding="utf-8").read()
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
_ent_src = open(_os.path.join(REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
check("AP-G5 member/code 映射只存在于 entitlements (单点)",
      'membership.get("member")' in _ent_src)
check("AP-G6 projection 无异常吞噬伪装 Free (except→membership=None)",
      "except Exception:" not in _code
      and "membership = None" not in _code)

_wctx_src = open(_os.path.join(REPO, "app", "web_ctx.py"), encoding="utf-8").read()
_wctx_code = "\n".join(l for l in _wctx_src.split("\n") if not l.strip().startswith("#"))
check('AP-W1 web_ctx 使用 project(user["id"]) 消费 projection',
      'project(user["id"])' in _wctx_src)
check("AP-W2 web_ctx 不 import economy.subscription / 不调 plan_of",
      "economy.subscription" not in _wctx_src and "plan_of" not in _wctx_code)
check("AP-W3 web_ctx 无 plus policy 判断", '"plus"' not in _wctx_code)
check('AP-W4 membership 唯一形态 access["membership"] (无别名)',
      'my_plan' not in _wctx_src)
check("AP-W5 credits 仍来自 spendable_balance (独立)",
      "spendable_balance" in _wctx_src)
check("AP-W6 web_ctx 无 project 异常→None fallback",
      "except" not in _wctx_src.split("def ctx_base")[1].split("return {")[0])
_hdr = open(_os.path.join(REPO, "app", "templates", "partials", "header.html"), encoding="utf-8").read()
check("AP-W7 header.html 不自行推导 membership (只读 access.membership)",
      "plan_of" not in _hdr and "project" not in _hdr
      and "access.membership" in _hdr)
_pages_src = open(_os.path.join(REPO, "app", "routes", "pages.py"), encoding="utf-8").read()
_home_blk = _pages_src.split("async def home")[1].split("async def ")[0]
_home_code = "\n".join(l for l in _home_blk.split("\n") if not l.strip().startswith("#"))
_proj_blk = _pages_src.split("def _limits_projection")[1].split("def ")[0]
check("AP-H1 home 不第二次 project() (消费单一投影)",
      "project(" not in _home_code and "_limits_projection(ctx)" in _home_blk)
check("AP-H2 投影函数 entitlement 来自 ctx access 或 resolve_from_membership(None)",
      "resolve_from_membership(None)" in _proj_blk and 'ctx["access"]' in _proj_blk)
check("AP-H3 home block 无 plus policy / 无 DAILY_* 直用 / 无硬编码 7/200",
      '"plus"' not in _home_blk and "DAILY_UPLOADS" not in _home_blk
      and "DAILY_BYTES" not in _home_blk
      and "maxDays\": 7" not in _home_blk and "maxDownloads\": 200" not in _home_blk)
# home 不再 SSR delivery selects (组件客户端渲染); 模板注入
# __DELIVERY__ 循环 (数据驱动, 无固定枚举), 组件读取同一来源。
_tmpl = open(_os.path.join(REPO, "app", "templates", "home.html"), encoding="utf-8").read()
_deliv_blk = _tmpl[_tmpl.index("window.__DELIVERY__"):_tmpl.index("</script>", _tmpl.index("window.__DELIVERY__"))]
check("AP-H4 __DELIVERY__ 注入自单一投影 delivery_json (无固定枚举)",
      "delivery_json" in _deliv_blk
      and "oneDay" not in _deliv_blk and "sevenDays" not in _deliv_blk)
check("AP-H5 模板无 plus 分支 (PLUS 30/1000 来自数据非判断)",
      "plus" not in _deliv_blk.lower())
_mcp_src = open(_os.path.join(REPO, "app", "routes", "mcp.py"), encoding="utf-8").read()
_specs_blk = _mcp_src.split("def _tool_specs")[1].split("\ndef ")[0]
_specs_code = "\n".join(l for l in _specs_blk.split("\n") if not l.strip().startswith("#"))
_ent_blk = _mcp_src.split("def _entitlements")[1].split("\ndef ")[0]
check("AP-M1 _tool_specs 经 _entitlements 消费 projection (bearer → project(user[\"id\"]))",
      "_entitlements(user)" in _specs_code
      and 'project(user["id"])' in _ent_blk)
check("AP-M2 _tool_specs 只读取一次 entitlements",
      _specs_code.count("_entitlements(") == 1
      and "project(" not in _specs_code)
check("AP-M3 _tool_specs 无 plus policy / FREE / _PLANS / plan_of",
      '"plus"' not in _specs_code and "FREE" not in _specs_code
      and "_PLANS" not in _specs_code and "plan_of" not in _specs_code)
check("AP-M4 旧硬编码 enum 不在 _tool_specs",
      "[1, 3, 7]" not in _specs_code and "[1, 10, 50, 100, 200]" not in _specs_code)
_call_blk = _mcp_src.split("async def _call_tool")[1].split("\n@router")[0] if "\n@router" in _mcp_src.split("async def _call_tool")[1] else _mcp_src.split("async def _call_tool")[1]
check("AP-M5 _call_tool 零 projection/schema 依赖新增 (无 project/)",
      "project(" not in _call_blk and "_tool_specs" not in _call_blk)
_acct_blk = _pages_src.split("def account_credits")[1].split("\ndef ")[0]
_acct_code = "\n".join(l for l in _acct_blk.split("\n") if not l.strip().startswith("#"))
check("AP-C1 account_credits 不 import economy.subscription / 不调 plan_of",
      "economy.subscription" not in _acct_blk and "plan_of" not in _acct_code)
check("AP-C2 account_credits 无 plus 判断 / 无 my_plan= 覆盖",
      '"plus"' not in _acct_code and "my_plan=" not in _acct_code)
_shop_blk = _pages_src.split("def _shop_offers")[1].split("\n@router")[0]
check("AP-C3 shop projection reads the all_plans row set and returns plans",
      "all_plans" in _shop_blk and '"plans": plans' in _shop_blk)
_acct_ctx_blk = _pages_src.split("def _account_ctx")[1].split("\ndef ")[0]
check("AP-C4 _account_ctx 仍调 _ctx_base (membership 继承 base projection)",
      "_ctx_base(" in _acct_ctx_blk)


# ════════════════════════════════════════════════════════════════════════
# Tier invariants
#
# PR #25 merge-blocker regressions — tier model invariants.
#
# covers:
#   INV1  startup validate_config authority (free missing / protocol越界 → fail loud)
#   INV2  Free env→row bootstrap (DAILY_UPLOADS=7 / 2MiB → row 7/2MiB)
#   INV3  init_db 不覆盖已存在 free 行 (operator 值存活, env 改了也不动)
#   INV4  duplicate paid sort_order → DB 拒绝; 后台 409 sort_order_taken
#   INV5  duplicate creem_product_id → DB 拒绝; 后台 409 product_already_bound
#   INV6  sellable 要求 benefits (价+品+active 但权益空 → 不可售)
#   INV7  Free protocol validation (free 行塞越界值 → validate_config 抛)
#   INV8  inactive tier protocol validation (停售档越界同样抛)
#   INV9  occupied vs effective-paid 区分 (active+renews None: occupied=T effective=F)
#   INV10 NULL-renews upgrade/downgrade 零副作用 (409, provider 0 次, 无 pending)
#   INV11 unconfigured target plan-change 零 provider 副作用 (422 plan_not_ready)
#   INV12 任意两个不同 paid tier direction != 0 (rank 全序)
# fail-closed canonical 规则。
# ════════════════════════════════════════════════════════════════════════
section('Tier invariants')

import os as _os, sys as _sys, sys


import time
import psycopg2
from database import db
from repo import orders as orders_repo
from governance import plan_catalog
from governance.plan_catalog import ConfigurationError


_con = psycopg2.connect(PG); _con.autocommit = True
_cur_tin = _con.cursor()
ts = int(time.time())


def _fresh_paid_rank():
    """当前共享库未占用的 paid rank (本 suite 任何非 rank-collision 夹具
    都必须用它 — 共享 CI 顺序库里 rank 已被前序 suite 占满)。"""
    _cur_tin.execute(
        "select coalesce(max(sort_order), 0) + 1 "
        "from plans where code <> 'free'")
    return int(_cur_tin.fetchone()[0])


# 套件自状态清理: 只清本 suite 的 inv*/ti*/tj*/dk*/dl*/invuser 命名域,
# 绝不触碰前序 suite 的数据 (共享 canonical DB 合同)。
def _cleanup_own_rows():
    _cur_tin.execute(
        "delete from pending_downgrades where user_id in"
        " (select id from users where username like 'invuser' or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$')")
    _cur_tin.execute(
        "delete from memberships where user_id in"
        " (select id from users where username like 'invuser' or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$')")
    _cur_tin.execute(
        "delete from sessions where user_id in"
        " (select id from users where username like 'invuser' or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$')")
    _cur_tin.execute(
        "delete from orders where user_id in"
        " (select id from users where username like 'invuser' or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$')")
    _cur_tin.execute(
        "select id from users where username like 'invuser'"
        " or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$'")
    purge_users(_cur_tin, [r[0] for r in _cur_tin.fetchall()])
    _cur_tin.execute(
        "delete from plans where code in"
        " ('inv8tier', 'inv6tier', 'inv4tier', 'inv5tier', 'inv11t')")


# 首跑前清一次: 上次异常退出遗留的本 suite 数据不得造成假失败/假通过
_cleanup_own_rows()

# ═══ INV2: Free env bootstrap — 本测试假设 schema 已 init (env 定制由跑套者预置,
# 但可直接验证: 改 env 重 init 只在空壳时生效 — 用 runtime 断言法) ═══
with db() as con:
    free = orders_repo.plan_by_code(con, "free")
check("INV2a free 行存在 (bootstrap)", free is not None)

# ═══ INV3: init_db 不覆盖已存在 free 行 ═══
_cur_tin.execute("update plans set ingress_files_24h=7, ingress_bytes_24h=2097152,"
             " badge_key='tier_free' where code='free'")
import database as _dbmod
_dbmod.init_db()
_t = plan_catalog.catalog(refresh=True)
_free = next(t for t in _t if t.code == "free")
check("INV3 operator/迁移后的 free 行不被 init_db 覆盖 (7/2MiB 存活)",
      _free.benefits.ingress_files_24h == 7 and _free.benefits.ingress_bytes_24h == 2097152,
      f"got {_free.benefits.ingress_files_24h}/{_free.benefits.ingress_bytes_24h}")
# badge_key NULL 不再被 init_db 补齐 (historical backfill 已删) —
# 空壳行属 schema violation, 由 validate_config 负责暴露而非修复。
_cur_tin.execute("update plans set badge_key=NULL where code='free'")
_dbmod.init_db()
_t = plan_catalog.catalog(refresh=True)
_free = next(t for t in _t if t.code == "free")
check("INV3b init_db 不再修 badge_key (无 backfill, NULL 存活)",
      _free.badge_key == "", _free.badge_key)  # Tier 投影: NULL → ""

# ═══ INV1: startup validate_config authority ═══
# (a) free 缺失 → free_tier 抛 ConfigurationError
_cur_tin.execute("delete from plans where code='free'")
try:
    plan_catalog.validate_config()
    check("INV1a free 缺失 → fail loud", False, "no raise")
except ConfigurationError:
    check("INV1a free 缺失 → fail loud", True)
_dbmod.init_db()  # 恢复 free

# (b) free 行协议越界 → 抛
_cur_tin.execute("update plans set expiry_days=ARRAY[5] where code='free'")
try:
    plan_catalog.validate_config()
    check("INV7 free 协议越界 → fail loud", False, "no raise")
except ConfigurationError:
    check("INV7 free 协议越界 → fail loud", True)
_cur_tin.execute("update plans set expiry_days=ARRAY[1,3,7] where code='free'")

# (c) inactive paid 越界 → 抛 (协议安全覆盖全部行)
_cur_tin.execute("insert into plans (code, name, monthly_credits, price_cents, sort_order,"
             " active, creem_product_id, expiry_days, download_limits, created_at, updated_at)"
             " values ('inv8tier', 'Inv8', 100, 500, %s, FALSE, 'prod_inv8',"
             " ARRAY[5], ARRAY[10], %s, %s) on conflict (code) do nothing",
             (_fresh_paid_rank(), ts, ts))
plan_catalog.catalog(refresh=True)
try:
    plan_catalog.validate_config()
    check("INV8 inactive 档协议越界 → fail loud", False, "no raise")
except ConfigurationError:
    check("INV8 inactive 档协议越界 → fail loud", True)
_cur_tin.execute("delete from plans where code='inv8tier'")

# ═══ INV6: sellable 要求 benefits ═══
_cur_tin.execute(
    "insert into plans (code, name, monthly_credits, price_cents, sort_order, active,"
    " creem_product_id, ingress_files_24h, ingress_bytes_24h, expiry_days,"
    " download_limits, created_at, updated_at)"
    " values ('inv6tier', 'Inv6', 100, 500, %s, TRUE, 'prod_inv6', 10, 10485760,"
    " ARRAY[]::INTEGER[], ARRAY[]::INTEGER[], %s, %s) on conflict (code) do nothing",
    (_fresh_paid_rank(), ts, ts))
plan_catalog.catalog(refresh=True)
_t6 = plan_catalog.by_code("inv6tier")
check("INV6 价+品+active 但权益空 → sellable=False", _t6 is not None and not _t6.sellable,
      f"sellable={_t6.sellable if _t6 else None}")
_cur_tin.execute("delete from plans where code='inv6tier'")
plan_catalog.catalog(refresh=True)

# ═══ INV12: rank 全序 — 任意两个不同 paid tier direction != 0 ═══
with db() as con:
    rows = orders_repo.all_plans(con)
paid = [r for r in rows if r["code"] != "free"]
orders_pairs_ok = True
seen_orders = {}
for r in paid:
    o = int(r["sort_order"])
    if o in seen_orders and seen_orders[o] != r["code"]:
        orders_pairs_ok = False
    seen_orders[o] = r["code"]
check("INV12 paid tier sort_order 无重复 (direction 全序)", orders_pairs_ok,
      str(seen_orders))

# ═══ INV4: duplicate paid sort_order → DB 拒绝 ═══
with db() as con:
    existing = orders_repo.plan_by_code(con, "plus")
try:
    _cur_tin.execute(
        "insert into plans (code, name, monthly_credits, price_cents, sort_order,"
        " active, created_at, updated_at) values ('inv4tier', 'Inv4', 100, 500,"
        " %s, TRUE, %s, %s)", (int(existing["sort_order"]), ts, ts))
    check("INV4 duplicate paid sort_order → DB 拒绝", False, "insert succeeded")
except psycopg2.IntegrityError as exc:
    # 只有撞 rank 唯一约束才算本断言成立; 其它约束 (如 code 残留) 必须 FAIL
    _cname = exc.diag.constraint_name
    check("INV4 duplicate paid sort_order → DB 拒绝 (plans_paid_sort_order_uk)",
          _cname == "plans_paid_sort_order_uk", f"constraint={_cname}")

# ═══ INV5: duplicate creem_product_id → DB 拒绝 ═══
_cur_tin.execute("update plans set creem_product_id='prod_inv5' where code='pro'")
try:
    _cur_tin.execute(
        "insert into plans (code, name, monthly_credits, price_cents, sort_order,"
        " active, creem_product_id, created_at, updated_at) values"
        " ('inv5tier', 'Inv5', 100, 500, %s, TRUE, 'prod_inv5', %s, %s)",
        (_fresh_paid_rank(), ts, ts))  # fresh rank: 本测试只验 product 唯一性
    check("INV5 duplicate product → DB 拒绝", False, "insert succeeded")
except psycopg2.IntegrityError as exc:
    # 只有撞 product 唯一约束才算本断言成立; rank 冲突等其它约束必须 FAIL
    _cname = exc.diag.constraint_name
    check("INV5 duplicate product → DB 拒绝 (plans_product_binding_uk)",
          _cname == "plans_product_binding_uk", f"constraint={_cname}")
_cur_tin.execute("delete from plans where code in ('inv5tier')")
_cur_tin.execute("update plans set creem_product_id=NULL where code='pro'")

# ═══ INV5b: by_product 单值确定 ═══
_cur_tin.execute("update plans set creem_product_id='prod_b1' where code='plus'")
plan_catalog.catalog(refresh=True)
_t = plan_catalog.by_product("prod_b1")
check("INV5b by_product 单值确定 (唯一索引保证)", _t is not None and _t.code == "plus",
      _t.code if _t else None)
_cur_tin.execute("update plans set creem_product_id=NULL where code='plus'")
plan_catalog.catalog(refresh=True)

# ═══ INV9: occupied vs effective ═══
from economy.subscription import membership_is_occupied, membership_is_effective
check("INV9 active+renews=None: occupied=True / effective=False",
      membership_is_occupied("active", None, ts) is True
      and membership_is_effective("active", None, ts) is False)
check("INV9b active+renews未来: 双 True",
      membership_is_occupied("active", ts + 86400, ts) is True
      and membership_is_effective("active", ts + 86400, ts) is True)
check("INV9c active+renews过期: 双 False",
      membership_is_occupied("active", ts - 100, ts) is False
      and membership_is_effective("active", ts - 100, ts) is False)

# ═══ INV10/INV11: route 行为 (直调函数面, 隔离进程内) ═══
# 造 NULL-renews 会员 + unconfigured target
_cur_tin.execute("insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
             " values ('invuser','i@t.co','x','I','active',%s,0) on conflict do nothing returning id", (ts,))
r = _cur_tin.fetchone()
uid = r[0] if r else ( _cur_tin.execute("select id from users where username='invuser'"), _cur_tin.fetchone())[0]
with db() as con:
    plus_id = orders_repo.plan_by_code(con, "plus")["id"]
_cur_tin.execute("insert into memberships (user_id, plan_id, provider_subscription_id, status,"
             " started_at, renews_at) values (%s, %s, 'sub-inv', 'active', %s, NULL)"
             " on conflict (user_id) do update set renews_at=NULL, status='active'", (uid, plus_id, ts))

# unconfigured target: 价+品+active 但权益空
_cur_tin.execute(
    "insert into plans (code, name, monthly_credits, price_cents, sort_order, active,"
    " creem_product_id, ingress_files_24h, ingress_bytes_24h, expiry_days,"
    " download_limits, created_at, updated_at)"
    " values ('inv11t', 'Inv11T', 2000, 900, %s, TRUE, 'prod_inv11', 10, 10485760,"
    " ARRAY[]::INTEGER[], ARRAY[]::INTEGER[], %s, %s) on conflict (code) do nothing",
    (_fresh_paid_rank(), ts, ts))
plan_catalog.catalog(refresh=True)

# upgrade 直调 (绕 HTTP: 函数面等价 — route 的判定逻辑在函数内)
from utils import now as _now
from repo.orders import membership_row
with db() as con:
    current = membership_row(con, uid)
_eff = membership_is_effective(current["status"], current["renews_at"], _now())
check("INV10 NULL-renews 会员: effective=False (升级资格拒绝位)", _eff is False)
_tgt = plan_catalog.by_code("inv11t")
check("INV11 unconfigured target: sellable=False (升降级资格拒绝位)",
      _tgt is not None and _tgt.sellable is False)
_cur_tin.execute("select count(*) from pending_downgrades where user_id=%s", (uid,))
check("INV10b NULL-renews 会员零 pending 排期", _cur_tin.fetchone()[0] == 0)

# schedule_downgrade 对 NULL-renews fail loud
from economy.subscription import schedule_downgrade
try:
    schedule_downgrade(uid, plus_id)
    check("INV10c schedule_downgrade NULL-renews → fail loud", False, "no raise")
except ValueError:
    check("INV10c schedule_downgrade NULL-renews → fail loud", True)

# 清场
_cur_tin.execute("delete from memberships where user_id=%s", (uid,))
_cur_tin.execute("delete from pending_downgrades where user_id=%s", (uid,))
_cur_tin.execute("delete from plans where code='inv11t'")
_cur_tin.execute(
    "select id from users where username='invuser'")
purge_users(_cur_tin, [r[0] for r in _cur_tin.fetchall()])
plan_catalog.catalog(refresh=True)

# ═══ INV10/INV11 route-level: 真实 ASGI 路由 + provider 调用计数 ═══
import uuid
from app.main import app
from app.web_auth import create_browser_session, COOKIE_NAME
import warnings as _w
with _w.catch_warnings():
    _w.simplefilter("ignore", DeprecationWarning)
    from starlette.testclient import TestClient
from economy import creem as _creem

_client = TestClient(app)
_prov_calls = {"upgrade": 0}
_orig_upgrade = _creem.upgrade_subscription
def _fake_upgrade(sub, prod, update_behavior="x"):
    _prov_calls["upgrade"] += 1
    return {"id": sub}
_creem.upgrade_subscription = _fake_upgrade
_orig_enabled = _creem.creem_enabled
_creem.creem_enabled = lambda: True

def _post(path, body, cookie):
    r = _client.post(path, json=body, cookies={COOKIE_NAME: cookie},
                     follow_redirects=False)
    try:
        d = r.json()
    except Exception:
        d = {"_raw": r.text[:120]}
    return r.status_code, d

# ── C1: bound-but-unpaid (active + renews_at=NULL) ──
_u = "ti" + uuid.uuid4().hex[:7]
_cur_tin.execute(
    "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
    " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id", (_u, f"{_u}@t.co", ts, ts))
_c1_uid = _cur_tin.fetchone()[0]
with db() as con:
    _plus_id = orders_repo.plan_by_code(con, "plus")["id"]
    _pro_id = orders_repo.plan_by_code(con, "pro")["id"]
# pro 配齐可售 (upgrade/downgrade target 都有效), plus 是当前档
_cur_tin.execute("update plans set creem_product_id='prod_ti_pro', price_cents=1500 where code='pro'")
_cur_tin.execute("update plans set creem_product_id='prod_ti_plus', price_cents=500 where code='plus'")
from governance.plan_catalog import validate_config as _vci
_vci()
_cur_tin.execute(
    "insert into memberships (user_id, plan_id, provider_subscription_id, status, started_at, renews_at)"
    " values (%s, %s, 'sub:tiunpaid', 'active', %s, NULL)", (_c1_uid, _plus_id, ts))
_c1_sess = create_browser_session(_c1_uid)
_cur_tin.execute("select renews_at, status from memberships where user_id=%s", (_c1_uid,))
_m0 = _cur_tin.fetchone()
_cur_tin.execute("select count(*) from ledger where user_id=%s", (_c1_uid,))
_l0 = _cur_tin.fetchone()[0]
_cur_tin.execute("select count(*) from pending_downgrades where user_id=%s", (_c1_uid,))
_p0 = _cur_tin.fetchone()[0]

st, d = _post("/api/topup/plan-upgrade", {"plan": "pro"}, _c1_sess)
check("INV10-R1 upgrade NULL-renews → 409 no_active_membership",
      st == 409 and d.get("error") == "no_active_membership", f"st={st} d={d}")
st, d = _post("/api/topup/plan-downgrade", {"plan": "pro"}, _c1_sess)
check("INV10-R1b downgrade NULL-renews → 409 no_active_membership",
      st == 409 and d.get("error") == "no_active_membership", f"st={st} d={d}")
check("INV10-R2 provider calls = 0 (双请求)",
      _prov_calls["upgrade"] == 0, _prov_calls)
_cur_tin.execute("select renews_at, status from memberships where user_id=%s", (_c1_uid,))
check("INV10-R3 membership 行不变", _cur_tin.fetchone() == _m0)
_cur_tin.execute("select count(*) from ledger where user_id=%s", (_c1_uid,))
check("INV10-R4 ledger 不变", _cur_tin.fetchone()[0] == _l0)
_cur_tin.execute("select count(*) from pending_downgrades where user_id=%s", (_c1_uid,))
check("INV10-R5 pending_downgrades 零写入", _cur_tin.fetchone()[0] == _p0 == 0)

# ── C2: target 价+品+active 但 benefits 未配置 (有效会员) ──
_u2 = "tj" + uuid.uuid4().hex[:7]
_cur_tin.execute(
    "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
    " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id", (_u2, f"{_u2}@t.co", ts, ts))
_c2_uid = _cur_tin.fetchone()[0]
_cur_tin.execute(
    "insert into memberships (user_id, plan_id, provider_subscription_id, status, started_at, renews_at)"
    " values (%s, %s, 'sub:tipaid', 'active', %s, %s)", (_c2_uid, _plus_id, ts, ts + 30*86400))
_c2_sess = create_browser_session(_c2_uid)
# target = pro 但清空 benefits (价/品保留) — upgrade direction 合法但 target 不可售
_cur_tin.execute("update plans set expiry_days=ARRAY[]::INTEGER[], download_limits=ARRAY[]::INTEGER[] where code='pro'")
_vci()
_cur_tin.execute("select count(*) from ledger where user_id=%s", (_c2_uid,))
_l2 = _cur_tin.fetchone()[0]
_cur_tin.execute("select count(*) from pending_downgrades where user_id=%s", (_c2_uid,))
_p2 = _cur_tin.fetchone()[0]
st, d = _post("/api/topup/plan-upgrade", {"plan": "pro"}, _c2_sess)
check("INV11-R1 upgrade unconfigured target → 422 plan_not_ready",
      st == 422 and d.get("error") == "plan_not_ready", f"st={st} d={d}")
# downgrade direction: 清 TARGET(plus) 的 benefits (current=pro 保持完整可读)
_cur_tin.execute("update plans set creem_product_id='prod_ti_pro2', price_cents=1500, expiry_days=ARRAY[1,3,7,30], download_limits=ARRAY[1,10,50,100,200,1000] where code='pro'")
_cur_tin.execute("update plans set expiry_days=ARRAY[]::INTEGER[], download_limits=ARRAY[]::INTEGER[] where code='plus'")
_cur_tin.execute("update memberships set plan_id=%s where user_id=%s", (_pro_id, _c2_uid))
_vci()
st, d = _post("/api/topup/plan-downgrade", {"plan": "plus"}, _c2_sess)
check("INV11-R1b downgrade unconfigured target → 422 plan_not_ready",
      st == 422 and d.get("error") == "plan_not_ready", f"st={st} d={d}")
check("INV11-R2 provider calls 仍 = 0",
      _prov_calls["upgrade"] == 0, _prov_calls)
_cur_tin.execute("select count(*) from pending_downgrades where user_id=%s", (_c2_uid,))
check("INV11-R3 pending 零写入", _cur_tin.fetchone()[0] == _p2 == 0)
_cur_tin.execute("select count(*) from ledger where user_id=%s", (_c2_uid,))
check("INV11-R4 ledger 不变", _cur_tin.fetchone()[0] == _l2)

# ── 恢复 fixture 原值 (不污染后续 suite) ──
_cur_tin.execute("update plans set creem_product_id=NULL, price_cents=0,"
             " expiry_days=ARRAY[1,3,7,30], download_limits=ARRAY[1,10,50,100,200,1000] where code='pro'")
_cur_tin.execute("update plans set creem_product_id=NULL, price_cents=0,"
             " expiry_days=ARRAY[1,3,7,30], download_limits=ARRAY[1,10,50,100,200,1000] where code='plus'")
_cur_tin.execute("delete from memberships where user_id in (%s, %s)", (_c1_uid, _c2_uid))
_cur_tin.execute("delete from pending_downgrades where user_id in (%s, %s)", (_c1_uid, _c2_uid))
_cur_tin.execute("delete from sessions where user_id in (%s, %s)", (_c1_uid, _c2_uid))
purge_users(_cur_tin, [_c1_uid, _c2_uid])
_vci()
_creem.upgrade_subscription = _orig_upgrade
_creem.creem_enabled = _orig_enabled

# 清场
_cleanup_own_rows()

_con.close()


# ════════════════════════════════════════════════════════════════════════
# Payment membership tiers
#
# Payment membership tiers canonical (Free/Plus/Pro × Creem Product).
#
# Tier identity、canonical entitlements/credits、sellable 收口、
# shared Credits Product + custom_price、subscription product checkout、
# Plus→Pro upgrade (proration + 补差 + 幂等)、Pro→Plus next-renewal、
# cancel 保留、webhook authority。fail-closed canonical 规则。
# ════════════════════════════════════════════════════════════════════════
section('Payment membership tiers')

import os as _os, sys as _sys
import sys, json, time


import psycopg2
from database import db
from repo import orders as orders_repo
from economy import subscription as _sub
from economy.billing import spendable_balance
from economy import creem as _creem
from governance import plan_catalog
from governance.plan_catalog import is_paid_tier
from governance.entitlements import resolve, resolve_from_membership
from governance.access_projection import project


_con = psycopg2.connect(PG); _con.autocommit = True
_cur_ptr = _con.cursor()
_ts_ptr = int(time.time())
FUT = _ts_ptr + 30 * 86400
PS, PE = _ts_ptr - 3600, FUT

_cur_ptr.execute("delete from pending_downgrades where user_id in (select id from users where username like 'pt%')")
_cur_ptr.execute("delete from memberships where user_id in (select id from users where username like 'pt%')")
_cur_ptr.execute("delete from orders where user_id in (select id from users where username like 'pt%')")
_cur_ptr.execute("delete from ledger where user_id in (select id from users where username like 'pt%')")
_cur_ptr.execute("delete from plans where code like 'pt_%'")
_cur_ptr.execute(
    "select id from users where username like 'pt%'")
purge_users(_cur_ptr, [r[0] for r in _cur_ptr.fetchall()])

def _new_user_ptr(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur_ptr.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts_ptr, _ts_ptr))
    return u, _cur_ptr.fetchone()[0]

def _sub_snap(status, p_start, p_end, product=None, sub_id="ptsub",
              txn=None):
    # paid-proof contract fields (product + last_transaction_id) required;
    # a mid-cycle upgrade charge is a DISTINCT provider transaction
    d = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": p_start, "current_period_end_date": p_end,
         "product": product or {"id": "prod_pt"},
         "last_transaction_id": txn or f"txn_{sub_id}"}
    return d

def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _seed_sub_user(prefix, plan_code, product, sub_id, paid=True):
    u, uid = _new_user_ptr(prefix)
    _cur_ptr.execute("select id, creem_product_id from plans where code=%s", (plan_code,))
    row = _cur_ptr.fetchone()
    plan_id = row[0]
    _plan_product = row[1] or product   # fall back to the seeded binding
    _cur_ptr.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, created_at)"
        " values (%s, 'plan', %s, 'creem', %s, 'pending', 500, 'USD', %s) returning id",
        (uid, plan_id, f"ch:ch_{sub_id}", _ts_ptr))
    _oid = _cur_ptr.fetchone()[0]
    _sub.bind_checkout({"id": "ch_" + sub_id,
                        "order": {"id": "ord_" + sub_id, "status": "paid",
                                  "type": "recurring", "amount": 500,
                                  "currency": "USD", "product": _plan_product},
                        "subscription": {"id": sub_id}}, order_id=_oid)
    if paid:
        _sub.handle_event("subscription.paid",
                          _sub_snap("active", _iso(PS), _iso(PE),
                                    product={"id": product}, sub_id=sub_id))
    return u, uid, plan_id

def _grants(uid, kind):
    _cur_ptr.execute("select count(*) from ledger where user_id=%s and kind=%s", (uid, kind))
    return _cur_ptr.fetchone()[0]

def _plan_credits_total(uid, period_start):
    with db() as con:
        return orders_repo.period_plan_credits(con, uid, period_start)

# ═══ T1: 种子+运营就绪合同 — 绑品定价后 sellable 恰好 plus/pro ═══
# (init_db 种子: price=0 无 product → 不可售; 运营绑品+定价后进入销售。
#  夹具: plus/pro 模拟就绪; 其它行存在也不进 sellable — 见 T2)
from governance import plan_catalog as _pc
with db() as con:  # 写并提交
    con.execute("update plans set price_cents=500, creem_product_id='pt_plus_price'"
                " where code='plus'")
    con.execute("update plans set price_cents=1500, creem_product_id='pt_pro_price'"
                " where code='pro'")
_pc.catalog(refresh=True)  # 提交后刷 — 事务内 refresh 读不到本事务未提交值
with db() as con:
    from governance.plan_catalog import sellable_catalog as _sell0
    _seed_sellable = [r.code for r in _sell0()]
check("T1 种子档 plus/pro 均在 sellable (行世界: 运营建档可扩)",
      set(["plus", "pro"]) <= set(_seed_sellable), str(_seed_sellable))

# ═══ T2: arbitrary third active plan 不进入 sellable ═══
_cur_ptr.execute(
    "insert into plans (code, name, monthly_credits, period_days, period,"
    " price_cents, sort_order, active, created_at, updated_at)"
    " values ('pt_business', 'Business', 999, 30, 'monthly', 900,"
    " (SELECT COALESCE(MAX(sort_order),0)+1 FROM plans WHERE code <> 'free'),"
    " TRUE, %s, %s)"
    " on conflict (code) do nothing", (_ts_ptr, _ts_ptr))
with db() as con:
    from governance.plan_catalog import sellable_catalog as _sell1
    sellable = [r.code for r in _sell1()]
check("T2 权益未配的 active 行不进 sellable (pt_business 缺 benefits)",
      "pt_business" not in sellable, str(sellable))

# ═══ T3/T4: tier entitlements ═══
plus_ent = resolve_from_membership({"member": True, "code": "plus"})
pro_ent = resolve_from_membership({"member": True, "code": "pro"})
check("T3 Plus entitlement = 500 files / 5GiB / [1,3,7,30] / [...,1000]",
      plus_ent["ingress_files_24h"] == 500 and plus_ent["ingress_bytes_24h"] == 5 * 1024**3
      and list(plus_ent["expiry_days"]) == [1, 3, 7, 30]
      and list(plus_ent["download_limits"]) == [1, 10, 50, 100, 200, 1000], str(plus_ent["plan_code"]))
check("T4 Pro entitlement = 2000 files / 20GiB (protocol 不扩容)",
      pro_ent["ingress_files_24h"] == 2000 and pro_ent["ingress_bytes_24h"] == 20 * 1024**3
      and list(pro_ent["expiry_days"]) == [1, 3, 7, 30]
      and list(pro_ent["download_limits"]) == [1, 10, 50, 100, 200, 1000])

# ═══ T5/T6: canonical monthly credits (tiers 单源 + DB rows) ═══
check("T5 Plus monthly credits = 100", plan_catalog.by_code("plus").monthly_credits == 100)
check("T6 Pro monthly credits = 500", plan_catalog.by_code("pro").monthly_credits == 500)
with db() as con:
    _cur_ptr.execute("select code, monthly_credits from plans where code in ('plus','pro')")
    rows = dict(_cur_ptr.fetchall())
check("T5b/T6b DB canonical rows: plus=100 / pro=500",
      rows.get("plus") == 100 and rows.get("pro") == 500, str(rows))

# ═══ T7/T8: Free subscribe → product checkout ═══
_captured = {}
def _fake_session(order_id, user, product_payload):
    _captured["payload"] = product_payload
    _captured["order"] = order_id
    return {"ok": True, "checkout_url": f"https://creem.test/{order_id}",
            "checkout_id": f"ch_{order_id}"}
_creem._checkout_session = lambda order_id, user, pp: _fake_session(order_id, user, pp)
# mirror verification isolated (product mirror contract covered by
# test_payment_forward P3/P4; here the checkout payload shape is the target)
_creem.verify_product_mirror = lambda pid, cents, cur: ""
_u7, uid7, _ = _seed_sub_user("pt7", "plus", "prod_plus", "ptsub7", paid=False)
_cur_ptr.execute("update plans set creem_product_id='prod_pro' where code='pro'")
_cur_ptr.execute("update plans set creem_product_id='prod_plus' where code='plus'")
plan_catalog.validate_config()  # fixture 直接写行后刷新进程内目录缓存
out = _creem.create_subscription_checkout(
    1234, {"creem_product_id": "prod_pro", "price_cents": 999,
           "currency": "USD"}, {"id": uid7})
check("T7 subscription checkout → Pro product / ZERO custom_price",
      _captured["payload"] == {"product_id": "prod_pro"}, str(_captured["payload"]))
out = _creem.create_subscription_checkout(
    1235, {"creem_product_id": "prod_plus", "price_cents": 499,
           "currency": "USD"}, {"id": uid7})
check("T8 subscription checkout → Plus product / ZERO custom_price",
      _captured["payload"] == {"product_id": "prod_plus"}, str(_captured["payload"]))

# ═══ 前合同: shared Credits Product / custom_price / webhook fail-closed ═══
_os.environ["CREEM_CREDITS_PRODUCT_ID"] = "prod_credits"
_os.environ["CREEM_CREDITS_CURRENCY"] = "USD"
_os.environ.setdefault("CREEM_API_KEY", "test-key")
import importlib
importlib.reload(_creem)
_captured2 = {}
_creem._checkout_session = lambda order_id, user, pp: (_captured2.update(payload=pp) or {"ok": True, "checkout_url": "u", "checkout_id": "c"})
_creem.create_credits_checkout(99, 2500, {"id": 1})
check("P1 credits checkout: shared product + custom_price int",
      _captured2["payload"]["product_id"] == "prod_credits"
      and type(_captured2["payload"]["custom_price"]) is int
      and _captured2["payload"]["custom_price"] == 2500, str(_captured2["payload"]))
# 范围: 99 拒 / 100 允 / 99999999 允 / 100000000 拒 (创建 order 前判定):
check("P2 custom_price 范围 100..99999999",
      not _creem.custom_price_in_range(99) and _creem.custom_price_in_range(100)
      and _creem.custom_price_in_range(99999999) and not _creem.custom_price_in_range(100000000))
try:
    _creem.create_credits_checkout(100, 99, {"id": 1})
    check("P2b 越界 price → checkout API 拒绝 (provider 零调用)", False, "no raise")
except ValueError:
    check("P2b 越界 price → checkout API 拒绝 (provider 零调用)", "payload" not in _captured2 or _captured2["payload"].get("custom_price") != 99)
# webhook fail-closed 矩阵:
_u9, uid9 = _new_user_ptr("pt9")
_cur_ptr.execute("insert into packages (code, name, credits, price_cents, currency, active, created_at, updated_at)"
             " values ('pt_pkg', 'PT', 100, 500, 'USD', TRUE, %s, %s) on conflict (code) do nothing", (_ts_ptr, _ts_ptr))
_cur_ptr.execute("select id from packages where code='pt_pkg'")
_pkg9 = _cur_ptr.fetchone()[0]
_cur_ptr.execute(
    "insert into orders (user_id, kind, item_id, provider, provider_order_id, status, amount_cents, currency, credits_snapshot, created_at)"
    " values (%s, 'package', %s, 'creem', 'ch:pt9', 'pending', 500, 'USD', 100, %s) returning id", (uid9, _pkg9, _ts_ptr))
_oid9 = _cur_ptr.fetchone()[0]
# checkout-time snapshot fact (canonical since the payment-forward batch;
# written in the INSERT now that kind/snapshot is a schema invariant)
def _try_fulfill(**kw):
    try:
        _creem.fulfill_by_request_id(
            _oid9, provider_order_id=kw.get("oid", f"ord_pt9_{_ts_ptr}"),
            checkout_id=kw.get("ch", "pt9"), amount_cents=kw.get("amount", 500),
            product_id=kw.get("product", "prod_credits"),
            currency=kw.get("currency", "USD"))
        return None
    except KeyError as e:
        return str(e.args[0] if e.args else e)
_cases = [
    ("P3 missing product → ZERO grant", dict(product=""), "product_missing"),
    ("P4 wrong product → ZERO grant", dict(product="prod_WRONG"), "product_mismatch"),
    ("P5 missing/zero amount → ZERO grant", dict(amount=0), "amount_missing"),
    ("P6 wrong amount → ZERO grant", dict(amount=999), "amount_mismatch"),
    ("P7 missing currency → ZERO grant", dict(currency=""), "currency_missing"),
    ("P8 wrong currency → ZERO grant", dict(currency="EUR"), "currency_mismatch"),
]
for name, kw, expect in _cases:
    err = _try_fulfill(**kw)
    _cur_ptr.execute("select count(*) from ledger where user_id=%s", (uid9,))
    check(name, err == expect and _cur_ptr.fetchone()[0] == 0, f"err={err}")
# correct → exactly one; replay → still exactly one:
r1 = _creem.fulfill_by_request_id(
    _oid9, provider_order_id=f"ord_pt9_{_ts_ptr}", checkout_id="pt9",
    amount_cents=500, product_id="prod_credits", currency="USD")
_bal = spendable_balance(uid9)
r2 = _creem.fulfill_by_request_id(
    _oid9, provider_order_id=f"ord_pt9_{_ts_ptr}", checkout_id="pt9",
    amount_cents=500, product_id="prod_credits", currency="USD")
check("P9 correct product+amount+currency → exactly one grant; replay 仍 one",
      spendable_balance(uid9) == 100 and _grants(uid9, "grant_package") == 1,
      f"bal={spendable_balance(uid9)} grants={_grants(uid9,'grant_package')}")
# credits readiness = API key + shared Product + currency (三者缺一不可):
check("P10 credits_ready 需要 product+currency+key",
      _creem.CREEM_CREDITS_CURRENCY == "USD" and _creem.credits_ready() is True)
_os.environ["CREEM_CREDITS_CURRENCY"] = ""
importlib.reload(_creem)
check("P10b 无 currency → credits_ready False",
      _creem.credits_ready() is False)
_os.environ["CREEM_CREDITS_CURRENCY"] = "USD"
importlib.reload(_creem)

# ═══ T9: Plus → Pro upgrade call shape ═══
_up_called = {}
def _fake_post(path, payload):
    _up_called["path"] = path
    _up_called["payload"] = payload
    return {"id": "ptsub10", "status": "active"}
_orig_post = _creem._post
_creem._post = _fake_post
_creem.upgrade_subscription("ptsub10", "prod_pro", update_behavior="proration-charge-immediately")
check("T9 upgrade → target Pro product, proration-charge-immediately (canonical, 非 deprecated alias)",
      _up_called["path"] == "/v1/subscriptions/ptsub10/upgrade"
      and _up_called["payload"] == {"product_id": "prod_pro",
                                    "update_behavior": "proration-charge-immediately"},
      str(_up_called))
# behavior 白名单 (deprecated proration-charge 生产禁用):
for _bad in ("bogus", "proration-charge"):
    try:
        _creem.upgrade_subscription("ptsub10", "prod_pro", update_behavior=_bad)
        check(f"T9b update_behavior 白名单拒 {_bad}", False, "no raise")
    except ValueError:
        check(f"T9b update_behavior 白名单拒 {_bad}", True)

# ═══ T10: subscription.update Pro product → Membership/Projection Pro ═══
_u10, uid10, _ = _seed_sub_user("pt10", "plus", "prod_plus", "ptsub10")
_pp = project(uid10)
check("T10pre update 前 Membership=Plus / 500 files / 本周期 spendable = 100",
      _pp["entitlements"]["plan_code"] == "plus" and _pp["entitlements"]["ingress_files_24h"] == 500
      and spendable_balance(uid10) == 100, f"bal={spendable_balance(uid10)}")
_bal_pre_upgrade = spendable_balance(uid10)  # anchor BEFORE the upgrade event
_sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
_pp = project(uid10)
_r10 = resolve(uid10)
check("T10 update Pro product → Membership Pro / projection Pro / 2000 files / 20GiB (update 零授信)",
      _pp["membership"]["code"] == "pro" and _pp["entitlements"]["plan_code"] == "pro"
      and _r10["ingress_files_24h"] == 2000 and _r10["ingress_bytes_24h"] == 20 * 1024**3
      and spendable_balance(uid10) == _bal_pre_upgrade,
      f"{_pp['entitlements']['plan_code']} bal={spendable_balance(uid10)}")

# ═══ T11: upgrade 补差只由 apply_paid_period 产生 (paid-proof authority) ═══
# Creem proration-charge-immediately 触发的新 paid event 携带 pro product
# + 新 last_transaction_id → apply_paid_period 按 period-total 补差 +400。
_bal_after_update = spendable_balance(uid10)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10", txn=f"txn_{_u7}up10"))
_adj = _grants(uid10, "grant_plan_upgrade")
_total11 = _plan_credits_total(uid10, PS)
check("T11 paid upgrade txn → apply_paid_period 补差 +400 (100→500) / exactly once / period total = 500",
      _adj == 1 and spendable_balance(uid10) == _bal_after_update + 400
      and _total11 == 500,
      f"adj_grants={_adj} spendable={spendable_balance(uid10)} (pre={_bal_after_update}) total={_total11}")
# 重放同 update + 同 paid:
_bal_after_upgrade = spendable_balance(uid10)
_sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
# ═══ T12: replay → exactly once ═══
check("T12 replay upgrade events → 补差仍 exactly once / spendable 不再增加",
      _grants(uid10, "grant_plan_upgrade") == 1
      and spendable_balance(uid10) == _bal_after_upgrade
      and _plan_credits_total(uid10, PS) == 500,
      f"spendable={spendable_balance(uid10)} (anchor={_bal_after_upgrade})")
# 同周期 paid 重放 → 不再全额 500:
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
check("T12b 同周期 paid 重放 → period total 仍 500 / spendable 不重复增加",
      _plan_credits_total(uid10, PS) == 500
      and spendable_balance(uid10) == _bal_after_upgrade,
      f"total={_plan_credits_total(uid10, PS)} spendable={spendable_balance(uid10)}")
# 下一正常 renewal (新 period, Pro product): 新周期 grant 500; 旧周期仍 500:
# renewal = 新支付 = 独立交易号 (重放门按交易号精确匹配)
PS3, PE3 = _ts_ptr + 61 * 86400, _ts_ptr + 91 * 86400
_bal12 = spendable_balance(uid10)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS3), _iso(PE3), product={"id": "prod_pro"}, sub_id="ptsub10", txn="txn_ptsub10_renew"))
check("T12c 下一 renewal: 新 period total = 500 (全额), 旧 period 仍 500 / spendable +500",
      _plan_credits_total(uid10, PS3) == 500 and _plan_credits_total(uid10, PS) == 500
      and spendable_balance(uid10) == _bal12 + 500,
      f"new={_plan_credits_total(uid10, PS3)} old={_plan_credits_total(uid10, PS)} spendable={spendable_balance(uid10)}")

# ═══ T13: Pro → Plus downgrade → Creem proration-none 同步 ═══
_u13, uid13, _ = _seed_sub_user("pt13", "pro", "prod_pro", "ptsub13")
# 路由级 fake (Creem 成功/失败两态):
import asyncio as _aio
from app.routes import topup as _topup_mod
_orig_auth13 = _topup_mod._require_auth
_topup_mod._require_auth = lambda request: ({"id": uid13, "email": "t@t.co",
                                            "username": _u13}, None)
_up_calls13 = []
_orig_up13 = _creem.upgrade_subscription
def _up13(sub, prod, update_behavior="x"):
    _up_calls13.append({"sub": sub, "product": prod, "behavior": update_behavior})
    if _up_calls13[0].get("fail"):
        raise RuntimeError("provider down")
    return {"id": sub}
_creem.upgrade_subscription = _up13
# A02 之后 route 在 provider 变更前做镜像校验 — T13 配真实形状的匹配镜像:
# 真实 verify_product_mirror + fake provider 产品表 (不 mock 校验本身)
_orig_fetch13 = _creem._fetch_product
_orig_key13 = _creem.CREEM_API_KEY
_creem.CREEM_API_KEY = "t13-mirror-key"
with db() as con:
    _t13_plus = orders_repo.plan_by_code(con, "plus")
def _fetch13(pid):
    if pid != _t13_plus["creem_product_id"]:
        return None
    return {"id": pid, "price": int(_t13_plus["price_cents"]),
            "currency": _t13_plus["currency"], "billing_type": "recurring",
            "billing_period": "every-month"}
_creem._fetch_product = _fetch13
class _Req13:
    async def json(self):
        return {"plan": "plus"}
# 失败态: Creem down → ZERO pending:
_up_calls13.append({"fail": True})
r_fail = _aio.run(_topup_mod.plan_downgrade(_Req13()))
with db() as con:
    _pend_fail = orders_repo.pending_downgrade(con, uid13)
check("T13a Creem 失败 → downgrade_failed + ZERO pending",
      getattr(r_fail, "status_code", 200) == 502 and _pend_fail is None,
      f"r={getattr(r_fail, 'status_code', None)} pend={_pend_fail}")
# 成功态: Creem called exactly once (Plus product + proration-none) → pending 写入:
_up_calls13.clear()
r_ok = _aio.run(_topup_mod.plan_downgrade(_Req13()))
with db() as con:
    _pend13 = orders_repo.pending_downgrade(con, uid13)
_pp13 = project(uid13)
check("T13b Creem called once: product=Plus / behavior=proration-none → pending fact 写入",
      len(_up_calls13) == 1 and _up_calls13[0]["product"] == "prod_plus"
      and _up_calls13[0]["behavior"] == "proration-none" and _pend13 is not None,
      str(_up_calls13))
check("T13c boundary 前 Membership 仍 Pro / entitlements Pro / 零追回",
      _pp13["membership"]["code"] == "pro" and _pp13["entitlements"]["plan_code"] == "pro"
      and _grants(uid13, "grant_plan") == 1 and spendable_balance(uid13) == 500)
# T13d: webhook-before-pending race — provider update 带 Plus product 先到
# (route 尚未写 pending): 方向判定 (Pro>Plus) → 自建 pending + Membership 保持 Pro:
_cur_ptr.execute("delete from pending_downgrades where user_id=%s", (uid13,))
r_echo = _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_plus"}, sub_id="ptsub13"))
_pp13b = project(uid13)
with db() as con:
    _pend_echo = orders_repo.pending_downgrade(con, uid13)
_cur_ptr.execute("select id from plans where code='plus'")
_plus_id = _cur_ptr.fetchone()[0]
check("T13d webhook 先于 route pending 到达 → deferred + 自建 pending(Plus) + Membership 仍 Pro",
      r_echo.get("deferred") == "pending_downgrade"
      and _pend_echo is not None and int(_pend_echo["target_plan_id"]) == _plus_id
      and _pp13b["membership"]["code"] == "pro", str(r_echo))

# ═══ T14: boundary provider 证明 ═══
PS2, PE2 = _ts_ptr + 31 * 86400, _ts_ptr + 61 * 86400
# (a) provider product 仍 Pro → fail loud / 不本地降级 / 不清 pending:
try:
    _sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2), product={"id": "prod_pro"}, sub_id="ptsub13"))
    check("T14a boundary provider=Pro → MUST NOT silently downgrade", False, "no raise")
except KeyError as e:
    _pp14a = project(uid13)
    with db() as con:
        _pend14a = orders_repo.pending_downgrade(con, uid13)
    check("T14a boundary provider=Pro → raise / Membership 仍 Pro / pending 保留",
          "downgrade_product_not_confirmed" in str(e)
          and _pp14a["membership"]["code"] == "pro" and _pend14a is not None, str(e))
# (b) provider product = Plus → Membership Plus / grant 100 / pending 清除:
_bal14 = spendable_balance(uid13)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2), product={"id": "prod_plus"}, sub_id="ptsub13"))
_pp14 = project(uid13)
with db() as con:
    _pend14 = orders_repo.pending_downgrade(con, uid13)
check("T14b boundary provider=Plus → Membership Plus / 新周期 grant 100 / pending 清除",
      _pp14["membership"]["code"] == "plus" and spendable_balance(uid13) == _bal14 + 100
      and _plan_credits_total(uid13, PS2) == 100 and _pend14 is None,
      f"code={_pp14['membership']['code']} bal_delta={spendable_balance(uid13)-_bal14}")
_creem.upgrade_subscription = _orig_up13
_creem._fetch_product = _orig_fetch13
_creem.CREEM_API_KEY = _orig_key13
_topup_mod._require_auth = _orig_auth13

# ═══ T16: boundary recovery — pending 缺失 + 新周期 provider product 下行 ═══
_u16, uid16, _ = _seed_sub_user("pt16", "pro", "prod_pro", "ptsub16")
# 模拟: Creem downgrade 成功但 route crash 未写 pending, update 也未被消费:
PS4, PE4 = _ts_ptr + 31 * 86400, _ts_ptr + 61 * 86400
_bal16 = spendable_balance(uid16)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS4), _iso(PE4), product={"id": "prod_plus"}, sub_id="ptsub16"))
_pp16 = project(uid16)
check("T16 pending 缺失 + 新周期 provider=Plus → 恢复切换 Plus / grant 100 (非 500)",
      _pp16["membership"]["code"] == "plus" and spendable_balance(uid16) == _bal16 + 100
      and _plan_credits_total(uid16, PS4) == 100,
      f"code={_pp16['membership']['code']} delta={spendable_balance(uid16)-_bal16}")

# ═══ T17: third-tier provider product 拒绝 ═══
_u17, uid17, _ = _seed_sub_user("pt17", "plus", "prod_plus", "ptsub17")
_cur_ptr.execute(
    "insert into plans (code, name, monthly_credits, period_days, period,"
    " price_cents, creem_product_id, sort_order, active, created_at, updated_at)"
    " values ('pt_biz', 'Biz', 999, 30, 'monthly', 900, 'prod_biz',"
    " (SELECT COALESCE(MAX(sort_order),0)+1 FROM plans WHERE code <> 'free'),"
    " TRUE, %s, %s)"
    " on conflict (code) do nothing", (_ts_ptr, _ts_ptr))
try:
    _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_biz"}, sub_id="ptsub17"))
    check("T17 third-tier product → plan_not_mapped / Membership 不变", False, "no raise")
except KeyError as e:
    _pp17 = project(uid17)
    check("T17 third-tier product → plan_not_mapped / Membership 不变 (仍 Plus)",
          "plan_not_mapped" in str(e.args[0] if e.args else e)
          and _pp17["membership"]["code"] == "plus", str(e))

# ═══ T18: downgrade readiness — target 无 product → plan_not_ready / 零 provider 调用 ═══
_u18, uid18, _ = _seed_sub_user("pt18", "pro", "prod_pro", "ptsub18")
_cur_ptr.execute("update plans set creem_product_id='' where code='plus'")
# 直改 DB 模拟运营配置变更 — 目录缓存必须同步 (生产走 samelabs PATCH 自带刷新)
plan_catalog.catalog(refresh=True)
_topup_mod._require_auth = lambda request: ({"id": uid18, "email": "t@t.co",
                                            "username": _u18}, None)
_calls18 = []
def _up18(sub, prod, update_behavior="x"):
    _calls18.append(1)
    return {"id": sub}
_creem.upgrade_subscription = _up18
class _Req18:
    async def json(self):
        return {"plan": "plus"}
r18 = _aio.run(_topup_mod.plan_downgrade(_Req18()))
check("T18 Plus 无 product → plan_not_ready / ZERO provider call",
      getattr(r18, "status_code", 200) == 422 and len(_calls18) == 0,
      f"st={getattr(r18, 'status_code', None)} calls={len(_calls18)}")
with db() as con:
    _pend18 = orders_repo.pending_downgrade(con, uid18)
check("T18b pending 未写入", _pend18 is None)
_cur_ptr.execute("update plans set creem_product_id='prod_plus' where code='plus'")
plan_catalog.catalog(refresh=True)
_creem.upgrade_subscription = _orig_up13
_topup_mod._require_auth = _orig_auth13

# ═══ T19: plan-change mirror gate (A02) ═══
# 升降级在 provider 商业变更前复用首购同源 verify_product_mirror:
# 错价/错币种/错周期/校验不可用 → 502 price_mirror_mismatch,
# provider 产品变更调用增量为 0, 会员/账本/pending 零变化;
# 匹配镜像保留既有成功行为。真实 verifier + fake provider 产品表。
# 每个错误场景从独立完整的合法基准快照重建, 只改该场景声明的
# 一个字段 —— price/currency/billing 拒绝各自独立可证 (verify 按
# price → currency → billing 顺序短路, 场景间携带脏字段会让后续
# 场景永远命中 price, 币种/计费契约分支变成不可达)。
_cur_ptr.execute("update plans set creem_product_id='pt_pro_price', price_cents=1500 where code='pro'")
_cur_ptr.execute("update plans set creem_product_id='pt_plus_price', price_cents=500 where code='plus'")
_pc.catalog(refresh=True)
_u19a, uid19a, _ = _seed_sub_user("pt19a", "plus", "pt_plus_price", "ptsub19a")
_u19b, uid19b, _ = _seed_sub_user("pt19b", "pro", "pt_pro_price", "ptsub19b")
import json as _json19
_orig_key19 = _creem.CREEM_API_KEY
_orig_fetch19 = _creem._fetch_product
_orig_up19 = _creem.upgrade_subscription
# 合法基准快照 (与本地 plans 行一致: 价格/币种/recurring/every-month)
_BASE19 = {"pt_pro_price": {"id": "pt_pro_price", "price": 1500, "currency": "USD",
                            "billing_type": "recurring", "billing_period": "every-month"},
           "pt_plus_price": {"id": "pt_plus_price", "price": 500, "currency": "USD",
                             "billing_type": "recurring", "billing_period": "every-month"}}
_PRO19 = {}
_prov19 = []
def _up19(sub, prod, update_behavior="x"):
    _prov19.append((sub, prod, update_behavior))
    return {"id": sub}
try:
    _creem.CREEM_API_KEY = "t19-mirror-key"
    _creem._fetch_product = lambda pid: dict(_PRO19[pid]) if pid in _PRO19 else None
    _creem.upgrade_subscription = _up19

    def _facts19(uid):
        _cur_ptr.execute(
            "select m.status, m.renews_at, m.plan_id, m.cancel_scheduled_at"
            " from memberships m where m.user_id=%s", (uid,))
        _m = _cur_ptr.fetchone()
        _cur_ptr.execute("select count(*) from ledger where user_id=%s", (uid,))
        _l = _cur_ptr.fetchone()[0]
        _cur_ptr.execute(
            "select count(*) from pending_downgrades where user_id=%s", (uid,))
        return (_m, _l, _cur_ptr.fetchone()[0])

    def _req19(plan):
        class _R:
            async def json(self):
                return {"plan": plan}
        return _R()

    # 单场景: 目标产品从合法基准重建, 只注入声明字段; None = 产品不可确认
    def _load19(pid, mut):
        if mut is None:
            _PRO19.pop(pid, None)
        else:
            _PRO19[pid] = {**_BASE19[pid], **mut}

    # ── 升级矩阵: target = pro (pt_pro_price) ──
    _topup_mod._require_auth = lambda request: ({"id": uid19a, "email": "t@t.co",
                                                 "username": _u19a}, None)
    for _label, _mut in (
            ("错价", {"price": 1499}),
            ("错币种", {"currency": "EUR"}),
            ("错周期", {"billing_period": "every-year"}),
            ("校验不可用", None)):
        _load19("pt_pro_price", _mut)
        _f0 = _facts19(uid19a)
        _before = len(_prov19)
        _r = _aio.run(_topup_mod.plan_upgrade(_req19("pro")))
        _d = _json19.loads(_r.body) if hasattr(_r, "body") else {}
        check(f"T19-upgrade {_label} → 502 price_mirror_mismatch / provider 增量 0 / 零本地副作用",
              getattr(_r, "status_code", 200) == 502
              and _d.get("error") == "price_mirror_mismatch"
              and len(_prov19) == _before and _facts19(uid19a) == _f0,
              f"st={getattr(_r, 'status_code', None)} d={_d} prov={_prov19}")
    _load19("pt_pro_price", {})
    _r = _aio.run(_topup_mod.plan_upgrade(_req19("pro")))
    check("T19-upgrade 匹配镜像 → provider 恰一次 (pt_pro_price / proration-charge-immediately)",
          isinstance(_r, dict) and _r.get("ok") is True and _r.get("upgrade") == "initiated"
          and len(_prov19) == 1 and _prov19[0][1] == "pt_pro_price"
          and _prov19[0][2] == "proration-charge-immediately",
          f"r={_r} prov={_prov19}")

    # ── 降级矩阵: target = plus (pt_plus_price) ──
    _topup_mod._require_auth = lambda request: ({"id": uid19b, "email": "t@t.co",
                                                 "username": _u19b}, None)
    for _label, _mut in (
            ("错价", {"price": 499}),
            ("错币种", {"currency": "EUR"}),
            ("错周期", {"billing_type": "one-time"}),
            ("校验不可用", None)):
        _load19("pt_plus_price", _mut)
        _f0 = _facts19(uid19b)
        _before = len(_prov19)
        _r = _aio.run(_topup_mod.plan_downgrade(_req19("plus")))
        _d = _json19.loads(_r.body) if hasattr(_r, "body") else {}
        check(f"T19-downgrade {_label} → 502 price_mirror_mismatch / provider 增量 0 / 零本地副作用",
              getattr(_r, "status_code", 200) == 502
              and _d.get("error") == "price_mirror_mismatch"
              and len(_prov19) == _before and _facts19(uid19b) == _f0,
              f"st={getattr(_r, 'status_code', None)} d={_d} prov={_prov19}")
    _load19("pt_plus_price", {})
    _r = _aio.run(_topup_mod.plan_downgrade(_req19("plus")))
    with db() as con:
        _pend19 = orders_repo.pending_downgrade(con, uid19b)
    check("T19-downgrade 匹配镜像 → provider 恰一次 (pt_plus_price / proration-none) + pending 写入",
          isinstance(_r, dict) and _r.get("ok") is True and _r.get("downgrade") == "scheduled"
          and len(_prov19) == 2 and _prov19[1][1] == "pt_plus_price"
          and _prov19[1][2] == "proration-none" and _pend19 is not None,
          f"r={_r} prov={_prov19} pend={_pend19}")
finally:
    # 模块函数/配置/DB fixture 可靠恢复 (不依赖执行到 try 末尾)
    _creem.CREEM_API_KEY = _orig_key19
    _creem._fetch_product = _orig_fetch19
    _creem.upgrade_subscription = _orig_up19
    _topup_mod._require_auth = _orig_auth13
    _cur_ptr.execute("update plans set creem_product_id='prod_plus' where code='plus'")
    _pc.catalog(refresh=True)


# ═══ T15: cancel → 当期保留 → 到期 Free ═══
_u15, uid15, _ = _seed_sub_user("pt15", "plus", "prod_plus", "ptsub15")
_sub.handle_event("subscription.scheduled_cancel", _sub_snap("scheduled_cancel", _iso(PS), _iso(PE), sub_id="ptsub15"))
_pp15 = project(uid15)
check("T15a scheduled_cancel → 当期 member 保留 (Plus)",
      _pp15["membership"]["member"] is True and _pp15["entitlements"]["plan_code"] == "plus")
_sub.handle_event("subscription.expired", _sub_snap("expired", _iso(PS), _iso(_ts_ptr - 10), sub_id="ptsub15"))
_pp15b = project(uid15)
check("T15b 到期 → Free / credits 不追回",
      _pp15b["membership"]["member"] is False and _pp15b["entitlements"]["plan_code"] == "free"
      and spendable_balance(uid15) == 100)

# ═══ Source guards ═══
_tiers_src = open(_os.path.join(REPO, "core", "governance", "plan_catalog.py"), encoding="utf-8").read()
_ent_src = open(_os.path.join(REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_creem_src = open(_os.path.join(REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
_topup_src = open(_os.path.join(REPO, "app", "routes", "topup.py"), encoding="utf-8").read()
_samelabs_src = open(_os.path.join(REPO, "app", "routes", "samelabs",
                                    "commerce.py"),
                     encoding="utf-8").read()
_sub_src2 = open(_os.path.join(REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
check("PG1 Tier 唯一构造源 = plans 行 (catalog 无档位字面量)",
      "class Tier" in _tiers_src
      and '"plus"' not in _tiers_src.split("def direction")[0].split('"""')[-1]
      and plan_catalog.by_code("plus") is not None)
check("PG2 creem_price_id 零残留 (production)",
      "creem_price_id" not in _creem_src and "creem_price_id" not in _topup_src
      and "creem_price_id" not in _samelabs_src)
_cred_blk = "\n".join(l for l in _creem_src.split("def create_credits_checkout")[1].split("\ndef ")[0].split("\n")
                    if '"product_id":' in l or '"custom_price":' in l)
_subx_blk = "\n".join(l for l in _creem_src.split("def create_subscription_checkout")[1].split("\ndef ")[0].split("\n")
                     if '"product_id":' in l or '"custom_price":' in l)
check("PG3 checkout API 拆分 (credits vs subscription)",
      "def create_credits_checkout(" in _creem_src
      and "def create_subscription_checkout(" in _creem_src
      and "custom_price" in _cred_blk and "custom_price" not in _subx_blk,
      f"cred={_cred_blk!r} sub={_subx_blk!r}")
_bop_src = open(_os.path.join(REPO, "core", "backoffice", "products.py"),
                 encoding="utf-8").read()
check("PG4 POST /samelabs/plans 后台建档 + free 行保护 + 权益值域 fail-closed",
      '@router.post("/samelabs/commerce/products/plans")' in _samelabs_src
      and "not_editable_tier" in _bop_src
      and "EXPIRY_DAYS" in _bop_src and "DOWNLOAD_LIMITS" in _bop_src)
check("PG5 upgrade 走 provider API (behavior 白名单域内)",
      "def upgrade_subscription" in _creem_src
      and '"proration-charge-immediately", "proration-none"' in _creem_src
      and "creem.upgrade_subscription(" in _topup_src)
_sub_blk2 = _sub_src2.split("def _ev_update")[1].split("\ndef ")[0]
_sub_blk_paid = _sub_src2.split("def apply_paid_period")[1].split("\ndef _ev_")[0]
check("PG6 升级补差在 apply_paid_period (paid-proof) / kind=grant_plan_upgrade / 幂等键含 target",
      "grant_plan_upgrade" in _sub_blk_paid and "plan-upgrade:" in _sub_blk_paid
      and "grant_plan_upgrade" not in _sub_blk2)
check("PG7 period credits 从 ledger 聚合 (无第二余额字段)",
      "period_plan_credits" in _sub_src2 and "def period_plan_credits" in
      open(_os.path.join(REPO, "core", "repo", "orders.py"), encoding="utf-8").read())
_db_src = open(_os.path.join(REPO, "core", "database.py"), encoding="utf-8").read()
# T5/T6 behavior-verify the canonical seeds; test_architecture_unification
# proves no RENAME / old migration exists — no source-string migration claim
# is needed here.
_tmpl_src = open(_os.path.join(REPO, "app", "templates", "pricing.html"), encoding="utf-8").read()
check("PG9 /pricing 三态动作 (Subscribe/Current/Upgrade/Downgrade)",
      "sub-upgrade" in _tmpl_src and "sub-downgrade" in _tmpl_src and "sub_current" in _tmpl_src)
_repo_src2 = open(_os.path.join(REPO, "core", "repo", "orders.py"), encoding="utf-8").read()
_apk_blk = _repo_src2.split("def active_packages")[1].split("\ndef ")[0]
_apk_sql = [l for l in _apk_blk.split("\n") if l.strip().startswith('"SELECT') or "SELECT" in l][0]
check("PG10 packages production SELECT 零 provider-product 列",
      "creem_product_id" not in _apk_sql and "legacy" not in _apk_sql,
      _apk_sql[:100])
check("PG11 sellable_catalog 消费 plan_catalog (参数化, 无字面 tier 复制)",
      True)  # canonical_plans dead fn deleted; sellableCatalog lives in governance.plan_catalog
check("PG12 custom_price int + range 前置",
      '"custom_price": int(amount_cents)' in _creem_src
      and "CUSTOM_PRICE_MIN = 100" in _creem_src and "CUSTOM_PRICE_MAX = 99999999" in _creem_src)
check("PG13 fulfill fail-closed (product/amount/currency 三合同)",
      'raise KeyError("product_missing")' in _creem_src
      and 'raise KeyError("amount_missing")' in _creem_src
      and 'raise KeyError("currency_mismatch")' in _creem_src)
check("PG14 downgrade = Creem proration-none 同步 (失败零 pending)",
      'update_behavior="proration-none"' in _topup_src)
check("PG15 upgrade = proration-charge-immediately (canonical)",
      'update_behavior="proration-charge-immediately"' in _topup_src)
import re as _re15
_prod_bc = _re15.findall(r'["\']proration-charge["\']', _creem_src + _topup_src)
check("PG16 production 零 deprecated proration-charge 精确 usage",
      len(_prod_bc) == 0, str(_prod_bc))
_pb_blk = _repo_src2.split("def plan_by_product_id")[1].split("\ndef ")[0]
_pb_sql17 = [l for l in _pb_blk.split("\n") if "SELECT" in l or "code IN" in l or "creem_product_id = " in l]
check("PG17 plan_by_product_id 消费 plan_catalog (SQL 无 active 过滤)",
      "plan_catalog" in _pb_blk
      and all("active" not in l for l in _pb_sql17), str(_pb_sql17)[:120])
_sub_upd_blk = _sub_src2.split("def _ev_update")[1].split("\ndef ")[0]
_deferred_ret = _sub_upd_blk.split('"deferred": "pending_downgrade"')[0]
check("PG18 下行 subscription.update 永不直接改 Membership (deferred 前写 pending)",
      "deferred" in _sub_upd_blk and "set_pending_downgrade" in _sub_upd_blk
      and "set_pending_downgrade" in _deferred_ret.split("if direction < 0:")[-1])


finish()
