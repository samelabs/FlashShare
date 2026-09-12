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

# ═══ 7c. REST /api/v1/me 消费 projection (真实 Bearer HTTP) ═══
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

def _login_http(username):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    out = b""
    for k, v in {"login": username, "password": "Test12345"}.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    b, ct = out + b"--B--\r\n", "multipart/form-data; boundary=B"
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read(); ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close(); return ck

def _token(ck):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/api/tokens", json.dumps({"name": "ap-me"}),
                 {"Cookie": ck, "Content-Type": "application/json"})
    t = json.loads(conn.getresponse().read()); conn.close()
    return (t.get("token") or {}).get("token") or ""

def _me(bearer):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("GET", "/api/v1/me", headers={"Authorization": f"Bearer {bearer}"})
    r = conn.getresponse(); d = json.loads(r.read()); conn.close()
    return r.status, d

# Free + credits independence
_sfx = str(int(time.time()))[-6:]
_mf = "apmefree" + _sfx
_register_http(_mf)
_mf_ck = _login_http(_mf)
_mf_bearer = _token(_mf_ck)
from economy.billing import grant as _grant, spendable_balance as _sb
_cur.execute("select id from users where username=%s", (_mf,))
_mf_id = _cur.fetchone()[0]
with db() as c:
    _grant(_mf_id, 42, "grant", idem_key=f"test:apme:{_sfx}", note="canonical")
st, d = _me(_mf_bearer)
check("ME-F1 Free /me → 200", st == 200, str(st))
check("ME-F2 credits == 独立 spendable_balance (42)",
      d.get("credits") == 42 == _sb(_mf_id), str(d.get("credits")))
check("ME-F3 plan is null 且 == access.membership",
      d.get("plan") is None and d.get("access", {}).get("membership") is None
      and d.get("plan") == d.get("access", {}).get("membership"))
_acc = d.get("access", {})
check("ME-F4 access.entitlements.plan_code=free + delivery/ingress 值来自 projection",
      _acc.get("entitlements", {}).get("plan_code") == "free"
      and _acc.get("entitlements", {}).get("expiry_days") == list(FREE["expiry_days"])
      and _acc.get("entitlements", {}).get("ingress_files_24h") == FREE["ingress_files_24h"])
check("ME-F5 access.usage = 当前窗口用量 (reader 同口径)",
      _acc.get("usage") == usage_24h(_mf_id), str(_acc.get("usage")))
check("ME-F6 credits 不在 access 内 (独立顶层)",
      "credits" not in _acc)

# PLUS
_mp = "apmeplus" + _sfx
_register_http(_mp)
_cur.execute("select id from users where username=%s", (_mp,))
_mp_id = _cur.fetchone()[0]
_seed_plan(_mp_id, "plus", "active", _ts + 30 * 86400)
st, d = _me(_token(_login_http(_mp)))
check("ME-P1 PLUS /me: plan.member=true 且 plan == access.membership",
      d.get("plan", {}).get("member") is True and d.get("plan") == d.get("access", {}).get("membership"))
check("ME-P2 PLUS entitlements 来自 projection (非 API 自拼)",
      d["access"]["entitlements"]["plan_code"] == "plus"
      and d["access"]["entitlements"]["expiry_days"] == list(_PLANS["plus"]["expiry_days"]))

# Expired
_me_ = "apmeexp" + _sfx
_register_http(_me_)
_cur.execute("select id from users where username=%s", (_me_,))
_me_id = _cur.fetchone()[0]
_seed_plan(_me_id, "plus", "active", _ts - 100)
st, d = _me(_token(_login_http(_me_)))
check("ME-E1 expired: plan 保留真实 fact (member=false) == access.membership",
      d.get("plan", {}).get("member") is False and d.get("plan") == d.get("access", {}).get("membership"))
check("ME-E2 expired: entitlements.plan_code=free (fact ≠ entitlement)",
      d["access"]["entitlements"]["plan_code"] == "free")

# ═══ 7d. Web base context 消费 projection (真实 HTTP 三态) ═══
def _get_home(ck=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    headers = {"Cookie": ck} if ck else {}
    conn.request("GET", "/", headers=headers)
    r = conn.getresponse(); body = r.read().decode(); conn.close()
    return r.status, body

def _has_plus_badge(body):
    return "plus-badge" in body and "PLUS" in body.split("plus-badge")[0][-200:] + body.split("plus-badge")[1][:20] if "plus-badge" in body else False

# Free authenticated
st, body = _get_home(_mf_ck)
check("WC-F1 Free 登录 GET / → 200", st == 200, str(st))
check("WC-F2 Free header 无 PLUS badge (与 project.membership=None 一致)",
      not _has_plus_badge(body) and project(_mf_id)["membership"] is None)

# PLUS authenticated
st, body = _get_home(_login_http(_mp))
check("WC-P1 PLUS 登录 GET / → 200 + header PLUS badge",
      st == 200 and _has_plus_badge(body)
      and project(_mp_id)["membership"]["member"] is True)

# Expired
st, body = _get_home(_login_http(_me_))
check("WC-E1 expired 登录 GET / → 200 无 PLUS badge (fact 保留 member=false)",
      st == 200 and not _has_plus_badge(body)
      and project(_me_id)["membership"]["member"] is False)

# Anonymous
st, body = _get_home()
check("WC-A1 匿名 GET / → 200 无 PLUS badge", st == 200 and not _has_plus_badge(body))

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
try:
    _c = _wctx.ctx_base(_AnonReq())
    check("WC-X2 匿名 context 不调用 project (access=None, my_plan=None)",
          _c.get("access") is None and _c.get("my_plan") is None)
except RuntimeError:
    check("WC-X2 匿名 context 不调用 project", False, "anon raised")
finally:
    _wctx.project = _orig_project

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
_v1 = open(_os.path.join(_REPO, "app", "routes", "apiv1.py"), encoding="utf-8").read()
_v1_me = _v1.split("async def v1_me")[1].split("async def")[0]
check("AP-G7 v1_me 使用 project( 消费 projection",
      'project(user["id"])' in _v1_me)
_v1_me_code = "\n".join(l for l in _v1_me.split("\n") if not l.strip().startswith("#"))
check("AP-G8 v1_me 无第二次 membership 读取 (代码行无 plan_of)",
      "plan_of" not in _v1_me_code and "economy.subscription" not in _v1_me)
check("AP-G9 v1_me 无 plus policy / 无自拼 entitlement 值",
      '"plus"' not in _v1_me and "expiry_days" not in _v1_me.split("access = project")[0]
      and "download_limits" not in _v1_me)
check("AP-G10 plan 是 access.membership payload alias",
      '"plan": access["membership"]' in _v1_me)
# AP-G11 (AST 稳定版): v1_me 的返回 dict 里 "credits" 键的值表达式
# 必须是对 spendable_balance(user["id"]) 的调用; 且该键不得取自
# project()/access 结果 (credits 永不进 projection)。
def _credits_guard():
    import ast as _a
    _body = "async def v1_me" + _v1_me.lstrip("async def v1_me").rstrip()
    _body = "\n".join(l for l in _body.split("\n") if not l.startswith("@router"))
    tree = _a.parse(_body)
    ret = None
    for node in _a.walk(tree):
        if isinstance(node, _a.Return) and isinstance(node.value, _a.Dict):
            ret = node.value
            break
    if ret is None:
        return False, "no return dict"
    kv = {}
    for k, v in zip(ret.keys, ret.values):
        if isinstance(k, _a.Constant):
            kv[k.value] = v
    cred = kv.get("credits")
    if not isinstance(cred, _a.Call):
        return False, "credits is not a call"
    fn = cred.func
    if not (isinstance(fn, _a.Name) and fn.id == "spendable_balance"):
        return False, f"credits func = {getattr(fn, 'id', '?')}"
    arg_ok = (len(cred.args) == 1 and isinstance(cred.args[0], _a.Subscript)
              and isinstance(cred.args[0].value, _a.Name)
              and cred.args[0].value.id == "user")
    if not arg_ok:
        return False, "credits arg is not user[...]"
    # access 值是 project() 调用; credits 键独立于它
    acc = kv.get("access")
    acc_is_project = isinstance(acc, _a.Name) and acc.id == "access"
    proj_call = any(isinstance(n, _a.Call) and isinstance(n.func, _a.Name)
                    and n.func.id == "project" for n in _a.walk(tree))
    return (arg_ok and acc_is_project and proj_call,
            f"credits=spendable_balance(user[..]) ok={arg_ok}, access-from-project={proj_call}")
_g11_ok, _g11_why = _credits_guard()
check('AP-G11 credits 独立来自 spendable_balance(user["id"]) 且不由 project()/access 提供 (AST)',
      _g11_ok, _g11_why)
_v1_all = _v1
check("AP-G11b v1_me 无 projection exception → Free/null fallback",
      "except" not in _v1_me)
check("AP-G12 其它 v1 endpoint 未加 membership/credits 参数",
      _v1_all.count("project(") == 1
      and "membership" not in _v1_all.split("async def v1_tools")[1])
_wctx_src = open(_os.path.join(_REPO, "app", "web_ctx.py"), encoding="utf-8").read()
_wctx_code = "\n".join(l for l in _wctx_src.split("\n") if not l.strip().startswith("#"))
check('AP-W1 web_ctx 使用 project(user["id"]) 消费 projection',
      'project(user["id"])' in _wctx_src)
check("AP-W2 web_ctx 不 import economy.subscription / 不调 plan_of",
      "economy.subscription" not in _wctx_src and "plan_of" not in _wctx_code)
check("AP-W3 web_ctx 无 plus policy 判断", '"plus"' not in _wctx_code)
check('AP-W4 my_plan 明确来自 access["membership"]',
      'access["membership"] if access else None' in _wctx_src)
check("AP-W5 credits 仍来自 spendable_balance (独立)",
      "spendable_balance" in _wctx_src)
check("AP-W6 web_ctx 无 project 异常→None fallback",
      "except" not in _wctx_src.split("def ctx_base")[1].split("return {")[0])
_hdr = open(_os.path.join(_REPO, "app", "templates", "partials", "header.html"), encoding="utf-8").read()
check("AP-W7 header.html 不自行推导 membership (只读 my_plan)",
      "plan_of" not in _hdr and "project" not in _hdr and "membership" not in _hdr
      and "my_plan" in _hdr)

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
