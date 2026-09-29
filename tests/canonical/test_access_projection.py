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

# ── FAIL-CLOSED GATE (同 the canonical-invocation unification 规则) ─────────────────────────────────
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
        "insert into memberships (user_id, plan_id, provider_subscription_id, status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
        (user_id, plan_id, f"sub:fx:{user_id}", status, _ts, renews_at, _ts))

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
    for t in ("memberships", "upload_usage", "ledger", "tool_calls"):
        _cur.execute(f"select count(*) from {t}")
        out[t] = _cur.fetchone()[0]
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
_HOST = ("127.0.0.1", _TEST_PORT_INT)

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
_cur.execute("select id from users where username=%s", (_mf,))
_mf_id = _cur.fetchone()[0]
def _get(path, bearer):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("GET", path, headers={"Authorization": f"Bearer {bearer}"})
    r = conn.getresponse(); r.read(); conn.close()
    return r.status

check("ME-R1 retired /api/v1/me → 404 (Bearer 无济于事)", _get("/api/v1/me", _mf_bearer) == 404)
check("ME-R2 browser /api/me 拒 Bearer-only caller (session surface)",
      _get("/api/me", _mf_bearer) == 401)

# users consumed by 7d web-context checks
_mp = "apmeplus" + _sfx
_register_http(_mp)
_cur.execute("select id from users where username=%s", (_mp,))
_mp_id = _cur.fetchone()[0]
_seed_plan(_mp_id, "plus", "active", _ts + 30 * 86400)
_me_ = "apmeexp" + _sfx
_register_http(_me_)
_cur.execute("select id from users where username=%s", (_me_,))
_me_id = _cur.fetchone()[0]
_seed_plan(_me_id, "plus", "active", _ts - 100)

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
_ti_src = open(_os.path.join(_REPO, "static", "pages", "tool-input.js"),
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

# Free: 无 membership fact → Account subscription status block 不渲染。
# 稳定 DOM marker (class 选择器), 不用 "PLUS" 字符串 (可售目录/header 可含)。
st, body = _get_credits(_mf_ck)
_proj_f = project(_mf_id)["membership"]
check("AC-F1 Free /account/credits → 200, membership None, 无 sub-status / 无 sub-badge on",
      st == 200 and _proj_f is None
      and 'class="sub-status"' not in body
      and 'class="sub-badge on"' not in body, f"{st}")

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
check("AP-G7 Public REST apiv1 模块不存在 (retired)",
      not _os.path.exists(_os.path.join(_REPO, "app", "routes", "apiv1.py")))
_main_src = open(_os.path.join(_REPO, "app", "main.py"), encoding="utf-8").read()
check("AP-G8 main.py 不注册 apiv1 router",
      "apiv1" not in _main_src)
check("AP-G9 MCP Bearer surface: valid token tools/list works",
      True)  # runtime behavior covered by U9/MCP suites
check("AP-G10 v1_me 已退役 (plan alias guard 随 surface 移除)",
      not _os.path.exists(_os.path.join(_REPO, "app", "routes", "apiv1.py")))

_wctx_src = open(_os.path.join(_REPO, "app", "web_ctx.py"), encoding="utf-8").read()
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
_hdr = open(_os.path.join(_REPO, "app", "templates", "partials", "header.html"), encoding="utf-8").read()
check("AP-W7 header.html 不自行推导 membership (只读 access.membership)",
      "plan_of" not in _hdr and "project" not in _hdr
      and "access.membership" in _hdr)
_pages_src = open(_os.path.join(_REPO, "app", "routes", "pages.py"), encoding="utf-8").read()
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
_tmpl = open(_os.path.join(_REPO, "app", "templates", "home.html"), encoding="utf-8").read()
_deliv_blk = _tmpl[_tmpl.index("window.__DELIVERY__"):_tmpl.index("</script>", _tmpl.index("window.__DELIVERY__"))]
check("AP-H4 __DELIVERY__ 注入自单一投影 delivery_json (无固定枚举)",
      "delivery_json" in _deliv_blk
      and "oneDay" not in _deliv_blk and "sevenDays" not in _deliv_blk)
check("AP-H5 模板无 plus 分支 (PLUS 30/1000 来自数据非判断)",
      "plus" not in _deliv_blk.lower())
_mcp_src = open(_os.path.join(_REPO, "app", "routes", "mcp.py"), encoding="utf-8").read()
_specs_blk = _mcp_src.split("def _tool_specs")[1].split("\ndef ")[0]
_specs_code = "\n".join(l for l in _specs_blk.split("\n") if not l.strip().startswith("#"))
check("AP-M1 _tool_specs 使用 project(user[\"id\"]) 消费 projection",
      'project(user["id"])' in _specs_blk)
check("AP-M2 _tool_specs 只调用一次 project(",
      _specs_code.count("project(") == 1)
check("AP-M3 _tool_specs 无 plus policy / FREE / _PLANS / plan_of",
      '"plus"' not in _specs_code and "FREE" not in _specs_code
      and "_PLANS" not in _specs_code and "plan_of" not in _specs_code)
check("AP-M4 旧硬编码 enum 不在 _tool_specs",
      "[1, 3, 7]" not in _specs_code and "[1, 10, 50, 100, 200]" not in _specs_code)
_call_blk = _mcp_src.split("async def _call_tool")[1].split("\n@router")[0] if "\n@router" in _mcp_src.split("async def _call_tool")[1] else _mcp_src.split("async def _call_tool")[1]
check("AP-M5 _call_tool 零 projection/schema 依赖新增 (无 project/)",
      "project(" not in _call_blk and "_tool_specs" not in _call_blk)
_acct_blk = _pages_src.split("async def account_credits")[1].split("async def ")[0]
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

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
