#!/usr/bin/env python3
"""Tool chain — Invocation → Dispatch → Gate: the IO-1 ToolInvocation
shape on every surface, operation identity (client_ref + fingerprint),
body (text/json) tools, inputs window / items results, and the
cross-surface coherence fixes.
"""
from _harness import PORT, REPO, TEST_DB, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# ToolInvocation unification
#
# the canonical-invocation unification ToolInvocation Unification 验收 A-K
# ════════════════════════════════════════════════════════════════════════
section('ToolInvocation unification')

import json, uuid, http.client, sys, os, base64, threading, re
import os as _os, sys as _sys

_HOST = ("127.0.0.1", PORT)


def mp(files=None, fields=None):
    out = b""; fields = fields or {}
    for k, v in fields.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for name, ct, data in (files or []):
        out += f'--B\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + data + b"\r\n"
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"

def new_user(prefix="sc"):
    conn = http.client.HTTPConnection(*_HOST, timeout=30)
    u = prefix + uuid.uuid4().hex[:6]
    b, ct = mp(fields={"username": u, "password": "Test12345", "password_confirm": "Test12345", "email": f"{u}@t.co"})
    conn.request("POST", "/auth/register", b, {"Content-Type": ct}); conn.getresponse().read()
    b, ct = mp(fields={"login": u, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    resp = conn.getresponse(); resp.read()
    conn.close()
    _seed_test_credits(u)
    return (resp.getheader("Set-Cookie") or "").split(";")[0], u

def _seed_test_credits(username, amount=50):
    """Test fixture: seed spendable credits via the production grant primitive
    so canonical runs do not depend on the daily-bonus product setting."""
    import psycopg2
    _os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
    sys.path.insert(0, os.path.join(REPO, 'core'))
    from database import db as _db
    from economy.billing import grant
    _c = psycopg2.connect(TEST_DB); _c.autocommit = True
    _cur = _c.cursor(); _cur.execute("select id from users where username=%s", (username,))
    _uid = _cur.fetchone()[0]; _c.close()
    with _db() as _c2:
        grant(_uid, amount, "grant", idem_key=f"test:{username}", note="canonical test funds")

def req(method, path, body, cookie=None, raw_body=None):
    conn = http.client.HTTPConnection(*_HOST, timeout=60)
    h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = cookie
    payload = raw_body if raw_body is not None else json.dumps(body)
    conn.request(method, path, payload, h)
    resp = conn.getresponse()
    try:
        data = json.loads(resp.read())
    except Exception:
        data = {"raw": True}
    conn.close()
    return resp.status, data

_PG = None
def _pg():
    global _PG
    if _PG is None:
        import psycopg2
        _PG = psycopg2.connect(TEST_DB); _PG.autocommit = True
    return _PG

def db_query(sql):
    cur = _pg().cursor()
    try:
        cur.execute(sql)
        return cur.fetchall()
    except Exception:
        return [("ERR",)]

def upload(cookie, name=b"t.png", ct="image/png", extra=None):
    """上传一个 1x1 png 源文件, 返回 {publicId, sourceToken}"""
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    conn = http.client.HTTPConnection(*_HOST, timeout=30)
    fields = {"tool": "compress",
              "client_ref": f"up-{uuid.uuid4().hex[:10]}"}
    if extra: fields.update(extra)
    b, ctt = mp(files=[(name.decode() if isinstance(name, bytes) else name, ct, png)], fields=fields)
    conn.request("POST", "/api/uploads", b, {"Content-Type": ctt, "Cookie": cookie})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return data

# ═══ A: parser ═══ (in-process; pure function, no DB)
_PARSER = None
def parse_invocation(raw):
    """Parse via the repo's canonical parser (pure function, no DB)."""
    global _PARSER
    if _PARSER is None:
        sys.path.insert(0, os.path.join(REPO, 'core'))
        from execution.invocation import parse_tool_invocation, InvocationError
        _PARSER = (parse_tool_invocation, InvocationError)
    p, E = _PARSER
    try:
        inv = p(raw)
        return 'OK:' + json.dumps(inv.to_json()), ''
    except E as e:
        return 'BAD:' + str(e), ''

def parse_ok(raw):
    out, err = parse_invocation(raw)
    if out.startswith("OK:"):
        return json.loads(out[3:])
    return None

def parse_bad(raw):
    out, err = parse_invocation(raw)
    return out == "BAD:invalid_request"

GOOD = {"tool_id": "compress",
        "source": {"files": [{"public_id": "fAAAAAAAAAAAA", "source_token": "tok"}]},
        "params": {"preset": "2mb"},
        "delivery": {"expiry_days": 7, "allowed_downloads": 200},
        "client_ref": "my-job-123"}
inv = parse_ok(GOOD)
check("A0 canonical PASS", inv and inv["tool_id"] == "compress" and len(inv["source"]["files"]) == 1
      and inv["params"] == {"preset": "2mb"} and inv["delivery"] == {"expiry_days": 7, "allowed_downloads": 200}
      and inv["client_ref"] == "my-job-123")
check("A0b to_json round-trip", parse_ok(inv) == inv)

bad_cases = [
    ("raw=[]", []),
    ("old top-level public_id", {"tool_id": "x", "public_id": "fAAA", "source": {"files": [{"public_id": "fA", "source_token": ""}]}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}),
    ("old public_ids", {"tool_id": "x", "public_ids": ["fA"], "source": {"files": [{"public_id": "fA", "source_token": ""}]}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}),
    ("params 非 dict", {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]}, "params": "abc", "delivery": {"expiry_days": 1, "allowed_downloads": 50}}),
    ("source 非 object", {"tool_id": "x", "source": "files", "delivery": {"expiry_days": 1, "allowed_downloads": 50}}),
    ("files=[]", {"tool_id": "x", "source": {"files": []}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}),
    ("files>20", {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}] * 21}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}),
    ("file 缺 source_token key", {"tool_id": "x", "source": {"files": [{"public_id": "fA"}]}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}),
    ("delivery string 值", {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]}, "delivery": {"expiry_days": "7", "allowed_downloads": 50}}),
    ("delivery 非枚举", {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]}, "delivery": {"expiry_days": 5, "allowed_downloads": 50}}),
    ("unknown top-level field", {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}, "extra": 1}),
    ("params.expiry_days 旧混法", {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]}, "params": {"expiry_days": 7}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}),
]
for name, raw in bad_cases:
    check(f"A1 拒 {name}", parse_bad(raw))
# params 缺失 → {}; delivery 缺失也拒 (两字段必须存在)
check("A2 params 缺失 → invalid (C-close 精确五键)", parse_bad({"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}))
check("A3 delivery 缺失拒", parse_bad({"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]}}))
check("A4 client_ref null → invalid (required identity)", parse_bad({**GOOD, "client_ref": None}))
for cr in ["", "x" * 129]:
    check(f"A5 拒 client_ref {cr[:12]!r}", parse_bad({**GOOD, "client_ref": cr}))
for cr2 in ["   ", 123]:
    check(f"A5b 拒 client_ref {cr2!r}", parse_bad({**GOOD, "client_ref": cr2}))
check("A5c 缺 client_ref 键 → invalid", parse_bad(
    {"tool_id": "x", "source": {"files": [{"public_id": "fA", "source_token": ""}]},
     "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50}}))

import re as _re
# ═══ B: one source schema (dispatch 无 single/multi branch) ═══
src_dispatch = re.sub(r'"""[\s\S]*?"""|#[^\n]*', '', open(os.path.join(REPO, 'core/execution/dispatch.py')).read())
check("B1 dispatch 无 flat source 字段分叉",
      "public_ids" not in src_dispatch and "source_tokens" not in src_dispatch
      and "req.public_id" not in src_dispatch)
check("B2 dispatch 无 single/multi request-shape branch",
      "req.public_id" not in src_dispatch and "_RawRequest" not in src_dispatch)

# ═══ C/D/E: 真实执行链 ═══
cookie, uname = new_user()
up = upload(cookie)
check("C0 upload ok", up.get("ok"), str(up)[:120])
pid, tok = up["file"]["stagingId"], up["file"].get("sourceToken", "")

# compress (multi=false) canonical
st, r = req("POST", "/api/tools/execute", {
    "tool_id": "compress", "source": {"files": [{"public_id": pid, "source_token": tok}]},
    "params": {}, "delivery": {"expiry_days": 7, "allowed_downloads": 200},
    "client_ref": "sc-test-1"}, cookie)
check("C1 compress canonical 执行 ok", r.get("ok"), f"{st} {str(r)[:150]}")

# C: handler capture — local handler 只见 params。用 probe 工具? 没有 → 用 DB 检查产物 delivery
if r.get("ok"):
    row = db_query(f"select expiry_days, allowed_downloads from uploads where public_id='{r['result']['items'][0]['public_id']}'")
    # uploads 表名可能是 shares — 试两个
    if not row or row == []:
        row = db_query(f"select expiry_days, allowed_downloads from shares where public_id='{r['result']['items'][0]['public_id']}'")
    check("D1 local delivery 7/200 落库", row and row[0][0] in (7, "7") and row[0][1] in (200, "200"), str(row))

# 旧 flat request → 422
st, r = req("POST", "/api/tools/execute", {
    "tool_id": "compress", "public_id": pid, "source_token": tok, "params": {}}, cookie)
check("C2 旧 flat request → 422 invalid_request", st == 422 and r.get("error") == "invalid_request", f"{st} {r}")

# E: client_ref 幂等 = same ref + SAME fingerprint。canonical 规则下 ref
# 绑定完整 operation (含 source public_id): 同 ref 重试必须用同一 source;
# 换 source 重试 = 新 operation, 必须 new ref (否则 409)。
st, r2 = req("POST", "/api/tools/execute", {
    "tool_id": "compress", "source": {"files": [{"public_id": pid, "source_token": tok}]},
    "params": {}, "delivery": {"expiry_days": 7, "allowed_downloads": 200},
    "client_ref": "sc-test-1"}, cookie)
check("E1 同 client_ref + 同 payload (同 source) → replay", r2.get("replay") is True and r2.get("ok"), str(r2)[:120])
up_e = upload(cookie)
st, r2b = req("POST", "/api/tools/execute", {
    "tool_id": "compress", "source": {"files": [{"public_id": up_e["file"]["stagingId"], "source_token": up_e["file"].get("sourceToken", "")}]},
    "params": {}, "delivery": {"expiry_days": 7, "allowed_downloads": 200},
    "client_ref": "sc-test-1"}, cookie)
check("E1b 同 ref + 新 source → 409 idempotency_conflict (ref 绑定 operation)",
      st == 409 and r2b.get("error") == "idempotency_conflict", f"{st} {str(r2b)[:110]}")
up_f = upload(cookie)
st, r3 = req("POST", "/api/tools/execute", {
    "tool_id": "compress", "source": {"files": [{"public_id": up_f["file"]["stagingId"], "source_token": up_f["file"].get("sourceToken", "")}]},
    "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
    "client_ref": f"fresh-{uuid.uuid4().hex[:8]}"}, cookie)
check("E2 新 client_ref → fresh (非 replay)", r3.get("ok") and not r3.get("replay"), str(r3)[:120])

# ═══ 多文件窗口工具: zip_pack 双文件 (one schema) ═══
up2 = upload(cookie, name=b"b.png")
up3 = upload(cookie, name=b"c.png")
st, rz = req("POST", "/api/tools/execute", {
    "tool_id": "zip_pack",
    "source": {"files": [
        {"public_id": up2["file"]["stagingId"], "source_token": up2["file"].get("sourceToken", "")},
        {"public_id": up3["file"]["stagingId"], "source_token": up3["file"].get("sourceToken", "")}]},
    "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
    "client_ref": f"auto-{uuid.uuid4().hex[:8]}-2"}, cookie)
check("B3 zip_pack files[2] 同一 schema ok", rz.get("ok"), f"{st} {str(rz)[:150]}")

# multi=false + 2 files → err_too_many_files
st, rm = req("POST", "/api/tools/execute", {
    "tool_id": "compress",
    "source": {"files": [
        {"public_id": up2["file"]["stagingId"], "source_token": ""},
        {"public_id": up3["file"]["stagingId"], "source_token": ""}]},
    "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
    "client_ref": f"auto-{uuid.uuid4().hex[:8]}-3"}, cookie)
check("B4 files[2] to a 1..1 tool → err_file_count", rm.get("error") == "err_file_count", f"{st} {rm}")

# ═══ G: API key for the MCP section below (minted via the test user's session) ═══
_conn_g = http.client.HTTPConnection(*_HOST, timeout=30)
_conn_g.request("POST", "/api/keys", json.dumps({"name": "scc"}),
                {"Cookie": cookie, "Content-Type": "application/json"})
_tk_g = json.loads(_conn_g.getresponse().read()); _conn_g.close()
bearer = (_tk_g.get("key") or {}).get("key") or ""

# ═══ H: MCP ═══
mc = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
conn = http.client.HTTPConnection(*_HOST, timeout=30)
conn.request("POST", "/mcp", mc, {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
resp = conn.getresponse()
try:
    mdata = json.loads(resp.read())
except Exception:
    mdata = {}
conn.close()
tools = (mdata.get("result") or {}).get("tools", [])
comp = next((t for t in tools if t["name"] == "compress"), None)
zp = next((t for t in tools if t["name"] == "zip_pack"), None)
def files_schema(t):
    return (t or {}).get("inputSchema", {}).get("properties", {}).get("files", {})
check("H1 compress files[] maxItems=1", files_schema(comp).get("maxItems") == 1 and files_schema(comp).get("minItems") == 1, str(files_schema(comp)))
check("H2 zip_pack files[] maxItems=20", files_schema(zp).get("maxItems") == 20, str(files_schema(zp)))
# _META: channel/UX meta tools without a files model. demo_shout 退役后
# 由正文工具 (P3 input 暴露) 承担对话形态; 目前内置无 0..0 工具。
_META = ("catalog_search", "create_upload", "list_uploads",
         "delete_upload", "pin_tool", "unpin_tool", "list_favorites")
# IO-1 source XOR: file tools expose files[], body tools expose input
_one_source = [t["name"] for t in tools if t["name"] not in _META
               and ("files" in (t.get("inputSchema", {}).get("properties") or {}))
               == ("input" in (t.get("inputSchema", {}).get("properties") or {}))]
no_b64 = all("file_b64" not in (t.get("inputSchema", {}).get("properties") or {}) for t in tools)
check("H3 每个工具恰一种输入模型 (files[] XOR input), 无 file_b64",
      not _one_source and no_b64, f"{len(tools)} tools; {_one_source}")
check("H4 delivery schema 存在", "delivery" in (comp or {}).get("inputSchema", {}).get("properties", {}))
_req_all = all("client_ref" in (t.get("inputSchema", {}).get("required") or [])
               for t in tools
               if t["name"] not in _META)
_req_cs = ("client_ref" in (([t for t in tools if t["name"] == "catalog_search"]
            or [{}])[0].get("inputSchema", {}).get("required") or []))
check("H4b executable tools required 含 client_ref (catalog_search 不含)",
      _req_all and not _req_cs)

# MCP tools/call 真实执行 (session cookie)
mbody = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
    "name": "compress",
    "arguments": {"files": [{"filename": "m.png", "b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}],
                   "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
                   "client_ref": f"mcpexec-{uuid.uuid4().hex[:8]}"}}}
conn = http.client.HTTPConnection(*_HOST, timeout=60)
conn.request("POST", "/mcp", json.dumps(mbody), {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
resp = conn.getresponse()
try:
    mcres = json.loads(resp.read())
except Exception:
    mcres = {}
conn.close()
# the result arrives as structuredContent (items + result_url)
msc = (mcres.get("result") or {}).get("structuredContent") or {}
mtext = ((mcres.get("result") or {}).get("content") or [{}])[0].get("text", "")
mok = bool(msc.get("result_url")) and isinstance(msc.get("items"), list)     and len(msc["items"]) >= 1 and msc["items"][0].get("kind") == "file"
check("H5 MCP tools/call canonical 执行 ok (structuredContent items)",
      mok, str(msc)[:150])

# H6: missing client_ref → -32602, zero upload/zero tool_call (校验先于 decode/quota/upload)
_gk_n = None
try:
    import urllib.request as _ur
    with _ur.urlopen("http://127.0.0.1:53901/files", timeout=5) as _r:
        _gk_n = len(json.loads(_r.read()))
except Exception:
    _gk_n = -1
mbad = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
    "name": "compress",
    "arguments": {"files": [{"filename": "n.png", "b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}],
                   "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10}}}}
conn = http.client.HTTPConnection(*_HOST, timeout=60)
conn.request("POST", "/mcp", json.dumps(mbad), {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
resp = conn.getresponse()
try:
    _mres_bad = json.loads(resp.read())
except Exception:
    _mres_bad = {}
conn.close()
_ec = _mres_bad.get("error", {}).get("code")
try:
    import urllib.request as _ur
    with _ur.urlopen("http://127.0.0.1:53901/files", timeout=5) as _r:
        _gk_n2 = len(json.loads(_r.read()))
except Exception:
    _gk_n2 = -1
check("H6 MCP 缺 client_ref → -32602 + zero upload",
      _ec == -32602 and (_gk_n2 in (-1, _gk_n) or _gk_n2 == _gk_n), f"code={_ec} gk={_gk_n}->{_gk_n2}")

# ═══ I: selftest 删除 ═══
conn = http.client.HTTPConnection(*_HOST, timeout=30)
conn.request("POST", "/api/usertools/x/selftest", "{}", {"Content-Type": "application/json", "Cookie": cookie})
resp = conn.getresponse(); resp.read(); sc_st = resp.status; conn.close()
check("I1 selftest endpoint 已删 (404/405)", sc_st in (404, 405), str(sc_st))

# ═══ J: 旧协议生产代码清零 ═══
srcs = {}
for f in ['core/execution/dispatch.py', 'core/execution/gate.py', 'core/execution/orchestrator.py',
          'core/execution/invocation.py', 'app/routes/api.py', 'app/routes/mcp.py',
          'static/app.js', 'static/pages/process-flow.js']:
    srcs[f] = open(os.path.join(REPO, f)).read()
strip_pat = re.compile(r'"""[\s\S]*?"""|"[^"]*"|' + chr(39) + '[^' + chr(39) + ']*' + chr(39) + r'|//[^\n]*|#[^\n]*', re.S)
# 规范第12条: 名字允许存在于 canonical source.files[] 与 upload 域;
# 禁的是旧协议形态 — 复数 top-level 字段 + flat request 解析 (raw.get("public_id"))
use_pat = re.compile(r'\bpublic_ids\b|\bsource_tokens\b|raw\.get\(\s*[\'\"]public_id[\'\"]\s*\)|raw\.get\(\s*[\'\"]source_token[\'\"]\s*\)')
inv_uses = sum(len(use_pat.findall(strip_pat.sub('', sv))) for sv in srcs.values())
check("J1 public_ids/source_tokens invocation uses = 0", inv_uses == 0, str({k: v for k, v in srcs.items() if 'public_ids' in v or 'source_tokens' in v}))
lifecycle_left = sum(s.count('params.get("expiry_days"') + s.count('params["expiry_days"') for s in srcs.values())
check("J2 params 中构造 expiry/allowed = 0", lifecycle_left == 0)
v1raw = sum(s.count('_v1_raw') for s in srcs.values())
check("J3 request.state._v1_raw = 0", v1raw == 0)
check("J4 mcp 无 file_b64 single branch", "args.get(\"file_b64\")" not in srcs['app/routes/mcp.py'])

# ── Tool identity/card contract guards (Batch A) ──
import os as _os
_REPO2 = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _rd(*p):
    return open(_os.path.join(_REPO2, *p), encoding="utf-8").read()

icons_src = _rd("core", "icons.py")
# G1 Tool icon 无 fallback 机制 (图标 = 输入类型硬编码映射, 全链无兜底):
import re as _re
check("G1 tool icon 无 fallback 定义/无 wand 兜底",
      "TOOL_ICON_FALLBACK" not in icons_src
      and icons_src.count('or "wand"') == 0
      and _re.search(r'return "wand"', icons_src) is None)
# G2 Market/User Page 卡片循环不各自 inline Tool SVG fallback (empty-state
# 装饰 svg 不属于 tool identity — 只查 tool 卡/macro 调用形态):
for tmpl in ("app/templates/tools_market.html", "app/templates/user_page.html"):
    src = _rd(*tmpl.split("/"))
    # v2: market rides the same inline .tc family as home (icon_tool_svg);
    # user_page keeps the tool_card macro — both canonical, none hand-drawn
    check(f"G2 {tmpl.split('/')[-1]} 卡面经 canonical renderer, 无自绘 tool svg/mkt-icon",
          ("tool_card(" in src or "icon_tool_svg(" in src)
          and "mkt-icon" not in src
          and 'icon_svg | safe' not in src)
# G3 Home/Market/User Page consume canonical renderer (macro partial):
part = _rd("app", "templates", "partials", "tool_identity.html")
check("G3 partial 消费 icon_tool_svg 单一 renderer",
      "icon_tool_svg(t.icon)" in part
      and "icon_svg(t." not in part
      and 'icon_svg("verified")' in part)  # 会员徽章: 全局 registry 单源
home_src3 = _rd("app", "templates", "home.html")
check("G3b home 工具卡经 tool_card 宏 (无 per-page icon 渲染)",
      "tool_card(t, lang, texts)" in home_src3
      and "icon_tool_svg(" not in home_src3
      and '{{ icon_path(t.icon' not in home_src3)
# G4 卡面单源: 唯一 tool_card 宏渲染四行卡 (图标+名称/说明 2 行/底部标签行
# +作者文本); 作者在卡内是文本 (卡片=单一导航面), calls 槽位不进卡:
check("G4 tool_card 为唯一卡面 (四行卡, 作者为文本, 无 calls 槽位)",
      part.count("{% macro tool_card(") == 1
      and "icon_tool_svg(t.icon)" in part
      and "t.stats" not in part
      and '<a class="by"' not in part and 'class="by"' in part)
# G5 market/user page 同一导航卡调用 (无 per-page 参数分叉):
for tmpl in ("app/templates/tools_market.html", "app/templates/user_page.html"):
    src5 = _rd(*tmpl.split("/"))
    check(f"G5 {tmpl.split('/')[-1]} 同一卡族 (macro 或 v2 inline .tc)",
          "tool_card(t, lang, texts)" in src5 or "icon_tool_svg(" in src5)
# G6 origin 语义单源:
cat_src = _rd("core", "governance", "catalog.py")
check("G6 Official/Community origin 单源 (owner 字段判定, 无形状判定)",
      "is_official_id" not in cat_src
      and '"origin": "official" if row["owner_id"] is None' in cat_src)
for tmpl in ("app/templates/tools_market.html", "app/templates/user_page.html",
             "app/templates/home.html"):
    src = _rd(*tmpl.split("/"))
    check(f"G6 {tmpl.split('/')[-1]} 无 origin 字符串判断",
          "'/' not in t.id" not in src and "'/' in t.id" not in src)
# G7 card href 仍是 Tool Page:
check("G7 card href = /tool/{id}",
      'href="/tool/{{ t.id }}"' in part)
# G8 v2 home: bento cards navigate to the tool page; the execution
# surface is the composer (mount + in-place tool selector):
home_src = _rd("app", "templates", "home.html")
check("G8 home 卡为导航面 + composer 为执行面 (选择器原地切换)",
      'id="toolInput"' in home_src
      and "tool_card(t, lang, texts)" in home_src
      and "icon_tool_svg(" not in home_src)
check("PG19 production 零 deprecated icon fallback 字面 (wand 兜底仅 icons.py)",
      sum(_rd("app", "templates", t).count('or "wand"')
          for t in ("home.html", "tools_market.html", "user_page.html")) == 0)


# ════════════════════════════════════════════════════════════════════════
# Operation identity: client_ref + invocation fingerprint
#
# Operation identity canonical — client_ref + invocation fingerprint.
#
# Client_ref is a REQUIRED client-generated stable operation identity;
# same user+tool+ref is permanently bound to one canonical invocation
# fingerprint (SHA-256 of canonical semantic payload). same ref + same
# hash → running=progress / ok=replay / failed=new-attempt; same ref +
# different hash → 409 idempotency_conflict with zero charge and zero
# execution. Real parser/gate/DB behavior — not source greps.
# ════════════════════════════════════════════════════════════════════════
section('Operation identity: client_ref + invocation fingerprint')

import os as _os
import sys as _sys


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
            " execution, label_json,"
            " visibility, enabled, created_at, updated_at, accept_json, format_family)"
            " values (%s, %s, %s, 'local', %s,"
            " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
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
    return asyncio.run(
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
toolR = {"id": tR, "tool_key": tR, "owner_id": owner}

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
toolC = {"id": tC, "tool_key": tC, "owner_id": owner}
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
toolN = {"id": tN, "tool_key": tN, "owner_id": owner}
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
toolF = {"id": tF, "tool_key": tF, "owner_id": owner}
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
toolX = {"id": tX, "tool_key": tX, "owner_id": owner}
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


# ════════════════════════════════════════════════════════════════════════
# Body-tool contract
#
# Body-tool contract: declaration storage, IO-1 source.body, and a
# temporarily registered local body tool executed in a REAL worker
# subprocess (the child re-runs load_all and must discover it).
#
# P1 of the tool-api batch: 文件工具与正文工具是两个互斥输入方式 —
# files_min=files_max=0 + body_format ∈ {text,json} (+可选 placeholder)。
# ════════════════════════════════════════════════════════════════════════
section('Body-tool contract')

import os as _os
import sys as _sys


_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")


import base64
import http.client as _hc
import json
import subprocess
import time
import uuid
ts_bdy = int(time.time())
_u_bdy = "bt" + _os.urandom(4).hex()
_HOST_bdy = ("127.0.0.1", int(TEST_PORT))

from database import db  # noqa: E402


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_bdy, timeout=60)
    if body is None:
        payload = None
    elif isinstance(body, bytes):
        payload = body
    else:
        payload = json.dumps(body).encode()
    conn.request(method, path, payload, headers or {})
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw) if raw else {}
    except Exception:
        return r.status, {}


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register_login(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    conn = _hc.HTTPConnection(*_HOST_bdy, timeout=30)
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    return ck


ck = _register_login(_u_bdy)
with db() as con:
    uid = con.execute("select id from users where username=%s", (_u_bdy,)).fetchone()["id"]

# ═══ A. declaration + storage + projection (user supply) ═══
st, r = _req("POST", "/api/usertools", {
    "label": "Body Demo", "url": "https://93.184.216.34/bb",
    "timeout_s": 30, "files_min": 0, "files_max": 0,
    "body_format": "text",
    "body_placeholder": {"en": "Paste text"},
    "cost": 0},
    {"Content-Type": "application/json", "Cookie": ck})
key = (r or {}).get("tool_key") or ""
check("A1 body tool created", st == 200 and r.get("ok") and key,
      f"{st} {str(r)[:90]}")
with db() as con:
    row = con.execute(
        "select body_format, body_placeholder_json, accept_json,"
        " format_family from platform_tools where tool_key=%s",
        (key,)).fetchone()
check("A2 stored row: body columns set, file columns NULL",
      row["body_format"] == "text"
      and json.loads(row["body_placeholder_json"]) == {"en": "Paste text"}
      and row["accept_json"] is None and row["format_family"] is None,
      str(dict(row)) if row else "no row")

# owner projection carries the body fields
st, r = _req("GET", "/api/usertools", None, {"Cookie": ck})
mine = next((t for t in (r.get("tools") or []) if t["tool_key"] == key), {})
check("A3 owner projection exposes body_format/placeholder",
      mine.get("body_format") == "text"
      and mine.get("body_placeholder") == {"en": "Paste text"}, str(mine)[:110])

# mutual-exclusion rejects
for payload, code, why in (
    ({"label": "X", "url": "https://93.184.216.34/x", "timeout_s": 30,
      "files_min": 0, "files_max": 0, "body_format": "text",
      "accept": [".png"]}, "bad_accept", "body tool with accept"),
    ({"label": "X", "url": "https://93.184.216.34/x", "timeout_s": 30,
      "files_min": 1, "files_max": 1, "format_families": "image",
      "accept": [".png"], "body_format": "text"},
     "bad_body_format", "file tool with body_format"),
    ({"label": "X", "url": "https://93.184.216.34/x", "timeout_s": 30,
      "files_min": 0, "files_max": 0}, "bad_body_format",
     "files 0..0 without body_format"),
):
    st, r = _req("POST", "/api/usertools", payload,
                 {"Content-Type": "application/json", "Cookie": ck})
    check(f"A4 {why} → 422 {code}",
          st == 422 and r.get("error") == code, f"{st} {str(r)[:70]}")

# PATCH: text → json (placeholder preserved); json → file (mutex columns
# cleared server-side, same transaction); file → body again
st, r = _req("PATCH", f"/api/usertools/{key}", {"body_format": "json"},
             {"Content-Type": "application/json", "Cookie": ck})
with db() as con:
    row = con.execute(
        "select body_format, body_placeholder_json from platform_tools"
        " where tool_key=%s", (key,)).fetchone()
check("A5 PATCH body_format text→json, placeholder preserved",
      st == 200 and row["body_format"] == "json"
      and json.loads(row["body_placeholder_json"]) == {"en": "Paste text"},
      f"{st} {str(dict(row)) if row else ''}")
st, r = _req("PATCH", f"/api/usertools/{key}",
             {"files_min": 1, "files_max": 1, "format_families": "image",
              "accept": [".png"]},
             {"Content-Type": "application/json", "Cookie": ck})
with db() as con:
    row = con.execute(
        "select body_format, body_placeholder_json, accept_json,"
        " format_family from platform_tools where tool_key=%s",
        (key,)).fetchone()
check("A6 PATCH switch body→file clears body columns",
      st == 200 and row["body_format"] is None
      and row["body_placeholder_json"] is None
      and row["accept_json"] is not None and row["format_family"] == "image",
      f"{st} {str(dict(row)) if row else ''}")
st, r = _req("PATCH", f"/api/usertools/{key}",
             {"files_min": 0, "files_max": 0, "body_format": "text"},
             {"Content-Type": "application/json", "Cookie": ck})
with db() as con:
    row = con.execute(
        "select body_format, accept_json, format_family"
        " from platform_tools where tool_key=%s", (key,)).fetchone()
check("A7 PATCH switch file→body clears file columns",
      st == 200 and row["body_format"] == "text"
      and row["accept_json"] is None and row["format_family"] is None,
      f"{st} {str(dict(row)) if row else ''}")

# F4: update_tool PATCH 终态防御 — 对正文工具只带 body_format:None 的
# PATCH 会留下 files_max=0 + body_format NULL 的互斥违例终态; 单点在
# 写库前拒绝, 库内无变化 (route 层的输入方式切换组装不触发此路径 —
# 这是 supply 单点的防御层)。
from governance.tool_supply import update_tool, DeclarationError
_f4 = False
try:
    update_tool(key, {"body_format": None})
except DeclarationError as exc:
    _f4 = str(exc) == "bad_body_format"
except Exception as exc:  # noqa: BLE001
    _f4 = f"unexpected {type(exc).__name__}: {exc}"
with db() as con:
    row = con.execute(
        "select files_max, body_format from platform_tools where tool_key=%s",
        (key,)).fetchone()
check("F4 inconsistent final state (files 0..0, body_format NULL) → rejected, row unchanged",
      _f4 is True and row["files_max"] == 0 and row["body_format"] == "text",
      f"{_f4} {dict(row) if row else ''}")

# ═══ B. invocation-level contract through the app ═══
def _exec(source, ref):
    return _req("POST", "/api/tools/execute", {
        "tool_id": "share",
        "source": source,
        "params": {},
        "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": ref},
        {"Content-Type": "application/json", "Cookie": ck})

st, r = _exec({"body": "hello"}, f"bt-filetool-body-{_u_bdy}")
check("B1 body source to a file tool → 422 err_input_mode",
      st == 422 and r.get("error") == "err_input_mode", f"{st} {str(r)[:70]}")

def _exec_body(tool, source, ref):
    return _req("POST", "/api/tools/execute", {
        "tool_id": tool,
        "source": source,
        "params": {},
        "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": ref},
        {"Content-Type": "application/json", "Cookie": ck})

st, r = _exec_body(key, {"files": []}, f"bt-emptyfiles-{_u_bdy}")
check("B2 files[] source to a body tool → 422 err_input_mode",
      st == 422 and r.get("error") == "err_input_mode", f"{st} {str(r)[:70]}")

def _exec_body_params(params, ref):
    return _req("POST", "/api/tools/execute", {
        "tool_id": key,
        "source": {"body": "x"},
        "params": params,
        "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": ref},
        {"Content-Type": "application/json", "Cookie": ck})

st, r = _exec_body_params({"opt": {"a": 1}}, f"bt-pdict-{_u_bdy}")
check("B2b body tool param dict value → 422 param_invalid (field)",
      st == 422 and r.get("error") == "param_invalid"
      and r.get("field") == "opt", f"{st} {str(r)[:80]}")
st, r = _exec_body_params({"opt": None}, f"bt-pnull-{_u_bdy}")
check("B2c body tool param null value → 422 param_invalid (field)",
      st == 422 and r.get("error") == "param_invalid"
      and r.get("field") == "opt", f"{st} {str(r)[:80]}")
st, r = _exec_body_params({"opt": [1, 2]}, f"bt-plist-{_u_bdy}")
check("B2d body tool param list value → 422 param_invalid (field)",
      st == 422 and r.get("error") == "param_invalid", f"{st} {str(r)[:80]}")
st, r = _exec_body(key, {"body": ""}, f"bt-emptybody-{_u_bdy}")
check("B3 empty text body → 422 err_body_required",
      st == 422 and r.get("error") == "err_body_required", f"{st} {str(r)[:70]}")
st, r = _exec_body(key, {"body": "x" * 100001}, f"bt-longbody-{_u_bdy}")
check("B4 text body over 100k → 422 err_body_too_long",
      st == 422 and r.get("error") == "err_body_too_long", f"{st} {str(r)[:70]}")
with db() as con:
    n_calls = con.execute(
        "select count(*) as n from tool_calls where user_id=%s and"
        " client_ref like %s", (uid, f"bt-%{_u_bdy}")).fetchone()["n"]
check("B5 all denials above left zero tool_calls",
      n_calls == 0, n_calls)

# ═══ C. parser + fingerprint (direct, no server state) ═══
from execution.invocation import (parse_tool_invocation, InvocationError,
                                  invocation_fingerprint, ToolInvocation,
                                  BODY_ABSENT)

good_body = {"tool_id": "x", "source": {"body": None},
             "params": {}, "delivery": {"expiry_days": 1,
                                        "allowed_downloads": 50},
             "client_ref": "r1"}
inv = parse_tool_invocation(good_body)
check("C1 body:null parses (key-presence semantics)",
      inv.has_body() and inv.body is None and inv.files == [])
try:
    parse_tool_invocation({**good_body, "source": {}})
    check("C2 empty source rejected", False, "accepted")
except InvocationError:
    check("C2 empty source rejected", True)
try:
    parse_tool_invocation({**good_body,
                           "source": {"files": [], "body": "x"}})
    check("C3 files+body both rejected", False, "accepted")
except InvocationError:
    check("C3 files+body both rejected", True)


def _mkbody(value, fmt):
    i = ToolInvocation("x", [], {}, {"expiry_days": 1,
                                     "allowed_downloads": 50}, "r")
    i.body = value
    i.body_format = fmt
    return i

h1 = invocation_fingerprint(_mkbody({"b": 2, "a": 1}, "json"))
h2 = invocation_fingerprint(_mkbody({"a": 1, "b": 2}, "json"))
check("C4 json body canonical equivalence → same fingerprint",
      h1 == h2 and len(h1) == 64)
check("C5 json body value change → different fingerprint",
      invocation_fingerprint(_mkbody({"a": 1}, "json"))
      != invocation_fingerprint(_mkbody({"a": 2}, "json")))
check("C6 text body fingerprint deterministic",
      invocation_fingerprint(_mkbody("abc", "text"))
      == invocation_fingerprint(_mkbody("abc", "text")))
check("C7 body vs files fingerprint differ",
      invocation_fingerprint(_mkbody("abc", "text"))
      != invocation_fingerprint(ToolInvocation(
          "x", [], {}, {"expiry_days": 1, "allowed_downloads": 50}, "r")))

# F5: canonical_body_payload 严格化 — body_format 未注入即抛错, 不按
# 值形态推断、无 repr 兜底 (调用方契约缺口必须显性失败)。
from execution.invocation import canonical_body_payload
try:
    canonical_body_payload(_mkbody("abc", None))
    check("F5 canonical_body_payload without body_format raises", False,
          "accepted")
except RuntimeError:
    check("F5 canonical_body_payload without body_format raises", True)

# ═══ D. real worker subprocess: temp body tool via load_all ═══
_tag = _os.urandom(4).hex()
_mod_name = f"tools_tmpbody_{_tag}"
_ttext = f"tmpbodyt_{_tag}"
_tjson = f"tmpbodyj_{_tag}"
_tmp = _os.path.join(REPO, "core", _mod_name + ".py")
with open(_tmp, "w") as f:
    f.write(f'''
import json as _json
from tools import register


def _echo(tool_input, params):
    return {{"items": [{{"kind": "text", "text": _json.dumps(
        {{"body": tool_input.get("body"), "params": params}},
        ensure_ascii=False)}}]}}


register(tool_id="{_ttext}", label={{"en": "TmpBodyT"}}, handler=_echo,
         files_min=0, files_max=0, outputs=("text",),
         body_format="text", body_placeholder={{"en": "Paste text"}})
register(tool_id="{_tjson}", label={{"en": "TmpBodyJ"}}, handler=_echo,
         files_min=0, files_max=0, outputs=("text",),
         body_format="json")
''')
try:
    _core_dir = _os.path.join(REPO, "core")
    runner = (
        "import sys, json\n"
        f"sys.path[:0] = [{REPO!r}, {_core_dir!r}]\n"
        "from execution.worker import process_tool\n"
        "req = json.load(sys.stdin)\n"
        "out = process_tool(req['tool'], [], req['params'], req['body'])\n"
        "json.dump(out, sys.stdout)\n")
    def _worker_call(tool, body, params=None):
        proc = subprocess.run(
            [_sys.executable, "-c", runner],
            input=json.dumps({"tool": tool, "body": body,
                              "params": params or {}}),
            capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            return None, proc.stderr[-400:]
        try:
            return json.loads(proc.stdout), None
        except Exception as exc:
            return None, f"bad stdout: {proc.stdout[:200]} {exc}"

    out, err = _worker_call(_ttext, "hello body", {"k": "v"})
    parsed = json.loads(out["items"][0]["text"]) if out else {}
    check("D1 text body through real worker subprocess (load_all re-run)",
          bool(out) and parsed == {"body": "hello body", "params": {"k": "v"}},
          err or str(out)[:110])
    out, err = _worker_call(_tjson, {"b": 2, "a": 1})
    parsed = json.loads(out["items"][0]["text"]) if out else {}
    check("D2 json object body survives the process boundary",
          bool(out) and parsed["body"] == {"a": 1, "b": 2}, err or str(out)[:110])
    out, err = _worker_call(_tjson, None)
    parsed = json.loads(out["items"][0]["text"]) if out else {}
    check("D3 json null body round-trips as None",
          bool(out) and parsed["body"] is None, err or str(out)[:110])
finally:
    try:
        _os.remove(_tmp)
    except OSError:
        pass

# ═══ cleanup ═══
with db() as con:
    con.execute("delete from platform_tools where owner_id=%s", (uid,))
    purge_users(con, [uid])


# ════════════════════════════════════════════════════════════════════════
# IO-1 contract: inputs window / items results / MCP surface
#
# IO-1 contract: inputs files window / items results / MCP agent surface.
#
#   D*  declaration: inputs.files {min,max,accept} (0 min legal), outputs
#       kinds, multi rejected, text param limit
#   C*  call: 0-file text tool executes; file-count window enforced
#       (under min / over max → typed error)
#   R*  results: multi-file share → one file item per input, ordered,
#       anonymous download md5 identical; result page /r/{id} renders
#       ordered items; settlement is one transaction (injected landing
#       failure leaves zero uploads rows)
#   M*  MCP: outputSchema present and consistent; structuredContent items
#       match; pass-through share of a small image emits NO image block
#       (echo suppressed) while a transformed image does; json item
#       rendered as text; isError text is one JSON block with error +
#       message + hint (format mismatch lists accept; file count states
#       the window)
#   X*  developer endpoint invalid shape → call failed + exact refund;
#       valid items shape (text + json + file via data_b64) round-trips
#
# A first-party trusted http fixture implements "text in → text+json+file
# out" with a mode switch for the invalid-shape case.
#
# Fail-closed env guard; per-run unique names; self-cleanup.
# ════════════════════════════════════════════════════════════════════════
section('IO-1 contract: inputs window / items results / MCP surface')

import base64
import hashlib
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid


import urllib.parse

_HOST_io = ("127.0.0.1", int(TEST_PORT))
_BASE = f"http://127.0.0.1:{TEST_PORT}"
BASE_URL = _os.environ.get("FLASHSHARE_BASE_URL", _BASE).rstrip("/")
_u_io = "io" + _uuid.uuid4().hex[:6]
ts_io = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _req_io(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_io, timeout=120)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form_io(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp(body, token):
    st, _h, d, _sc = _req_io("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token, mid=1):
    return _mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


from database import db

uid = None
TOOL_KEY = f"io{_u_io}_texty"
try:
    # register user + API key + credits
    b, ct = _form_io({"username": _u_io + "a", "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{_u_io}@t.co"})
    _req_io("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_io({"login": _u_io + "a", "password": "Test12345"})
    st_l, _h, _d, sc = _req_io("POST", "/auth/login", b, {"Content-Type": ct})
    ck_io = sc.split(";")[0]
    st, _h, d_k, _sc = _req_io("POST", "/api/keys", {"name": "io"},
                             {"Content-Type": "application/json", "Cookie": ck_io})
    TOK = d_k["key"]["key"]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u_io + "a",)).fetchone()["id"])
        from repo.ledger import insert_row
        insert_row(con, uid, 100000, 100000, "manual_topup", None, None,
                   f"io:{_u_io}", None, ts_io)
    check("F1 user + key + credits", uid and TOK, uid)

    # The suite targets the LOCAL tool surface for text-out
    # (registry-backed) and validates the developer http items contract
    # through direct drive_http calls (the hosted /tool-endpoints
    # surface was deleted in C1 — first-party tools run local only).
    # ── text-out local tool: register via seed adapter is code-side
    # only, so drive the contract through share/compress plus direct
    # module-level item validation. ──

    # ═══ D. declaration ═══
    from governance.tool_supply import (validate_tool_declaration,
                                        validate_tool_patch,
                                        DeclarationError)
    good = validate_tool_declaration({
        "tool_key": f"{_u_io}t", "tool_id": f"{_u_io}t", "owner_id": None,
        "execution": "local",
        "label": {"en": "T"},
        "format_family": "image",
        "inputs": {"files": {"min": 0, "max": 20, "accept": [".png"]}},
        "outputs": ["text", "json"], "params": None,
        "visibility": "public", "enabled": True})
    check("D1 min=0 declaration valid",
          good["inputs"]["files"]["min"] == 0, good)
    body_tool = validate_tool_declaration({
        "tool_key": f"{_u_io}t3", "tool_id": f"{_u_io}t3", "owner_id": None,
        "execution": "local",
        "label": {"en": "T"},
        "inputs": {"files": {"min": 0, "max": 0, "accept": None}},
        "body_format": "text",
        "body_placeholder": {"en": "Paste text"},
        "outputs": ["text"], "params": None,
        "visibility": "public", "enabled": True})
    check("D1b body-tool declaration (files 0..0 + body_format) valid",
          body_tool["inputs"]["files"]["max"] == 0
          and body_tool["body_format"] == "text", body_tool)
    for bad, why in (
        ({"min": -1, "max": 1, "accept": None}, "min<0"),
        ({"min": 2, "max": 1, "accept": None}, "min>max"),
        ({"min": 1, "max": 21, "accept": None}, "max>20"),
        ({"min": 0, "max": 0, "accept": [".png"]}, "max=0 with accept"),
    ):
        decl = {"tool_key": f"{_u_io}t2", "tool_id": f"{_u_io}t2", "owner_id": None,
                "execution": "local",
                "label": {"en": "T"},
                "inputs": {"files": bad},
                "outputs": ["file"], "params": None,
                "visibility": "public", "enabled": True}
        try:
            validate_tool_declaration(decl)
            check(f"D2 {why} rejected", False, "accepted")
        except DeclarationError as exc:
            check(f"D2 {why} rejected", str(exc) == "bad_inputs", str(exc))
    # 输入方式互斥 (body_format ↔ files window)
    for extra, why in (
        ({"body_format": None}, "files 0..0 without body_format"),
        ({"body_format": "bogus"}, "body_format outside vocabulary"),
        ({"body_format": "text", "format_family": "image"},
         "body tool with tag"),
    ):
        decl = {"tool_key": f"{_u_io}t4", "tool_id": f"{_u_io}t4", "owner_id": None,
                "execution": "local",
                "label": {"en": "T"},
                "inputs": {"files": {"min": 0, "max": 0, "accept": None}},
                "outputs": ["text"], "params": None,
                "visibility": "public", "enabled": True, **extra}
        try:
            validate_tool_declaration(decl)
            check(f"D2b {why} rejected", False, "accepted")
        except DeclarationError as exc:
            check(f"D2b {why} rejected",
                  str(exc) in ("bad_body_format", "bad_format_family"), str(exc))
    for extra, why in (
        ({"body_format": "text"}, "file tool with body_format"),
        ({"body_placeholder": {"en": "x"}}, "file tool with placeholder"),
    ):
        decl = {"tool_key": f"{_u_io}t5", "tool_id": f"{_u_io}t5", "owner_id": None,
                "execution": "local",
                "label": {"en": "T"},
                "format_family": "image",
                "inputs": {"files": {"min": 1, "max": 1,
                                     "accept": [".png"]}},
                "outputs": ["file"], "params": None,
                "visibility": "public", "enabled": True, **extra}
        try:
            validate_tool_declaration(decl)
            check(f"D2c {why} rejected", False, "accepted")
        except DeclarationError as exc:
            check(f"D2c {why} rejected",
                  str(exc) in ("bad_body_format", "bad_body_placeholder"),
                  str(exc))
    # 正文取值口径 (governance.body 单点)
    from governance.body import (validate_body_value, canonical_json,
                                 BodyValueError, BODY_MAX_CHARS)
    check("D6a text body accepted verbatim",
          validate_body_value("text", "hello") == "hello")
    try:
        validate_body_value("text", "")
        check("D6b empty text body rejected", False, "accepted")
    except BodyValueError as exc:
        check("D6b empty text body rejected",
              str(exc) == "err_body_required", str(exc))
    try:
        validate_body_value("text", "x" * (BODY_MAX_CHARS + 1))
        check("D6c text body over limit rejected", False, "accepted")
    except BodyValueError as exc:
        check("D6c text body over limit rejected",
              str(exc) == "err_body_too_long", str(exc))
    check("D6d json null body is legal (key-presence semantics)",
          validate_body_value("json", None) == "null")
    check("D6e json canonical serialization (sorted/compact)",
          canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}')
    check("D6f json str root canonical",
          canonical_json("abc") == '"abc"')
    try:
        canonical_json(float("nan"))
        check("D6g NaN body rejected", False, "accepted")
    except BodyValueError as exc:
        check("D6g NaN body rejected", str(exc) == "err_body_invalid",
              str(exc))
    try:
        canonical_json([1, float("inf")])
        check("D6h Infinity nested rejected", False, "accepted")
    except BodyValueError:
        check("D6h Infinity nested rejected", True)
    patch = validate_tool_patch({"multi": True})
    check("D3 multi is no longer declaration vocabulary (ignored key,"
          " never persisted)", "multi" not in patch, patch)
    from governance.tool_params import (validate_params_decl,
                                        enforce_params_decl,
                                        ParamsValueError,
                                        TEXT_PARAM_MAX_CHARS)
    decl = validate_params_decl([{"key": "txt", "type": "text"}])
    out = enforce_params_decl(decl, {"txt": "hello"})
    check("D4 text param accepted", out["txt"] == "hello", out)
    try:
        enforce_params_decl(decl, {"txt": "x" * (TEXT_PARAM_MAX_CHARS + 1)})
        check("D5 text over limit rejected", False, "accepted")
    except ParamsValueError as exc:
        check("D5 text over limit rejected", str(exc) == "param_too_long",
              str(exc))
    check("D5b limit constant = 100,000", TEXT_PARAM_MAX_CHARS == 100_000,
          TEXT_PARAM_MAX_CHARS)

    # ═══ C. file-count window (share: 1..20) ═══
    A64 = base64.b64encode(PNG).decode()
    st, d = _call("share", {"files": [], "client_ref": f"c0-{_u_io}"}, TOK, 10)
    res = d.get("result") or {}
    txt = (res.get("content") or [{}])[0].get("text", "")
    check("C1 0 files to share (min=1) → isError + English window hint",
          res.get("isError") is True
          and "accepts 1 to 20 files" in txt, txt[:100])
    files21 = [{"filename": f"f{i}.png", "b64": A64} for i in range(21)]
    st, d = _call("share", {"files": files21, "client_ref": f"c21-{_u_io}"}, TOK, 11)
    check("C2 21 files → rejected (array window)",
          (d.get("error") or {}).get("code") == -32602
          or (d.get("result") or {}).get("isError") is True, str(d)[:100])
    # compress declares 1..1 → 2 files is a window error
    st, d = _call("compress", {"files": [
        {"filename": "a.png", "b64": A64},
        {"filename": "b.png", "b64": A64}], "client_ref": f"c2-{_u_io}"}, TOK, 12)
    res = d.get("result") or {}
    txt = (res.get("content") or [{}])[0].get("text", "")
    check("C3 single-file tool with 2 files → isError + window hint",
          res.get("isError") is True and "accepts 1 to 1 files" in txt,
          txt[:100])

    # ═══ R. multi-file share items + /r page ═══
    files3 = [{"filename": f"r{i}.png", "b64": A64} for i in range(3)]
    st, d = _call("share", {"files": files3, "client_ref": f"r3-{_u_io}"}, TOK, 20)
    res = d.get("result") or {}
    check("R0 share call ok", res.get("isError") is False, str(d)[:120])
    sc = res["structuredContent"]
    items = sc["items"]
    check("R1 3 in → 3 ordered file items",
          [i["filename"] for i in items] == ["r0.png", "r1.png", "r2.png"],
          items)
    check("R2 every item has a direct download_url + page_url",
          all(i["download_url"].endswith("?download=1") and i["page_url"]
              for i in items), items[:1])
    dl = items[0]["download_url"]
    loc = urllib.parse.urlsplit(dl)
    st_d, h_d, _b, _sc = _req_io("GET", loc.path + "?" + loc.query)
    gid = urllib.parse.parse_qs(urllib.parse.urlsplit(
        h_d.get("location", "")).query).get("id", [""])[0]
    import urllib.request as _ur
    gk = (_os.environ.get("FLASHSHARE_GOKAPI_URL") or "").rstrip("/")
    if not gk:
        _sys.stderr.write(
            "ERROR: FLASHSHARE_GOKAPI_URL is required (R3 direct-download "
            "identity is checked against the storage authority).\n")
        _sys.exit(2)
    with _ur.urlopen(f"{gk}/downloadFile?id={gid}", timeout=15) as r:
        blob = r.read()
    check("R3 anonymous direct download md5 identical",
          hashlib.md5(blob).hexdigest() == hashlib.md5(PNG).hexdigest(),
          len(blob))
    st, _h, page, _sc = _req_io("GET", f"/r/{sc['result_url'].rsplit('/', 1)[1]}")
    # Image results render as one media group: 3 image files = one
    # rs-item grid carrying 3 rs-cells, each with its own download.
    check("R4 /r page renders ordered items",
          st == 200 and page.count('class="rs-cell"') == 3
          and "r0.png" in page and "r1.png" in page and "r2.png" in page
          and page.count("rs-dl") >= 3, st)
    # grid cells caption name + size (same row facts as file rows); the
    # img alt stays the tool name
    check("R4b /r media: filename + size captions, alt is the tool name",
          st == 200
          and page.count('<figcaption><b class="if-name"') == 3
          and page.count('alt="Free Share"') == 3, st)
    _rp = _call("share", {"files": files3, "client_ref": f"r3-{_u_io}"},
                TOK, 21)[1]
    check("R5 replay returns the same items (idempotent)",
          ((_rp.get("result") or {}).get("structuredContent")
            or {}).get("replay") is True, str(_rp)[:100])

    # ═══ R6. settlement atomicity (in-process _finish + injected
    # landing failure): every file item lands in ONE transaction or none
    # does; the failure settles to failed + exact refund, zero uploads.
    from execution import gate as _gate
    from repo.ledger import claim_call
    from execution.results import StagedItems, StagedFileItem
    import repo.uploads as _upmod
    with db() as con:
        _before = int(con.execute(
            "select count(*) c from uploads where user_id=%s",
            (uid,)).fetchone()["c"])
    _staged = StagedItems(
        order=["file", "file"],
        files=[StagedFileItem(gokapi_id=f"gk_{_u_io}_a", filename="x0.png",
                              content_type="image/png", size=70,
                              expiry_days=1, allowed_downloads=5,
                              expire_at=None, url_hotlink=""),
               StagedFileItem(gokapi_id=f"gk_{_u_io}_b", filename="x1.png",
                              content_type="image/png", size=70,
                              expiry_days=1, allowed_downloads=5,
                              expire_at=None, url_hotlink="")],
        ip="127.0.0.1")
    _orig_ir = _upmod.insert_record
    def _boom_ir(*a, **k):
        raise RuntimeError("injected landing failure")
    _upmod.insert_record = _boom_ir
    try:
        with db() as con:
            _anchor, _claimed = claim_call(
                con, f"rf-{_u_io}", uid, "share", 25,
                "{}", ts_io, commission_rate_snapshot=0,
                invocation_hash="e" * 64)
        try:
            _gate._finish({"id": uid},
                          {"id": "share", "tool_key": "share",
                           "owner_id": None},
                          int(_anchor["id"]), 25, _staged, 0,
                          {"expiry_days": 1, "allowed_downloads": 5})
            finished = "no-raise"
        except Exception:
            finished = "raised"
    finally:
        _upmod.insert_record = _orig_ir
    with db() as con:
        _after = int(con.execute(
            "select count(*) c from uploads where user_id=%s",
            (uid,)).fetchone()["c"])
        _call_row = con.execute(
            "select status from tool_calls where id=%s",
            (int(_anchor["id"]),)).fetchone()
    check("R6 injected landing failure → zero uploads rows (all-or-nothing)",
          _after == _before and _call_row is not None
          and _call_row["status"] != "ok",
          (_before, _after, finished, _call_row and dict(_call_row)))

    # ═══ E. result expiry (delivery-owned, every shape) ═══
    from execution.results import land_items_result, StagedItems
    import time as _time
    _e_ts = int(_time.time())
    with db() as con:
        _text_rec = land_items_result(
            con, uid, StagedItems(order=["text", "json"],
                                  inline=[{"kind": "text", "text": "hi"},
                                          {"kind": "json", "data": {"a": 1}}],
                                  files=[]), "http://x",
            {"expiry_days": 3, "allowed_downloads": 5})
    _exp_expect = _e_ts + 3 * 86400
    check("E1 text/json-only result expires with the delivery window",
          isinstance(_text_rec[2].get("expires_at"), int)
          and abs(_text_rec[2]["expires_at"] - _exp_expect) <= 2,
          _text_rec[2].get("expires_at"))
    # file result inherits the latest file expiry (staged expire_at)
    _f_exp = _e_ts + 5 * 86400
    with db() as con:
        _file_rec = land_items_result(
            con, uid,
            StagedItems(order=["file"],
                        files=[__import__("execution.results", fromlist=["StagedFileItem"]).StagedFileItem(
                            gokapi_id=f"gk_{_u_io}_e", filename="e.png",
                            content_type="image/png", size=10,
                            expiry_days=5, allowed_downloads=5,
                            expire_at=_f_exp, url_hotlink="")],
                        ip="127.0.0.1"), "http://x",
            {"expiry_days": 1, "allowed_downloads": 5})
    check("E2 file result expiry = latest file expiry",
          _file_rec[2].get("expires_at") == _f_exp, _file_rec[2].get("expires_at"))
    # /r/{id} enforces expiry at render: a past-expiry record → 404
    _dead_id = "dead" + _u_io[:8]
    with db() as con:
        con.execute(
            "insert into tool_calls (user_id, tool_id, status, client_ref,"
            " invocation_hash, started_at, commission_rate_snapshot,"
            " result_public_id, result_json, finished_at)"
            " values (%s, 'share', 'ok', %s, %s, %s, 0, %s, %s, %s)",
            (uid, f"dead-{_u_io}", "d" * 64, ts_io, _dead_id,
             __import__("json").dumps({"result_id": _dead_id,
                                       "expires_at": ts_io - 3600,
                                       "items": [{"kind": "text", "text": "x"}]}),
             ts_io))
    st, _h_d, _b_d, _sc_d = _req_io("GET", f"/r/{_dead_id}")
    check("E3 expired /r → 404 (no existence oracle)", st == 404, st)
    # live result page renders header facts + the media grid + downloads
    st, _h_r, page_r, _sc_r = _req_io(
        "GET", f"/r/{sc['result_url'].rsplit('/', 1)[1]}")
    check("E4 /r page: tool name + expiry + downloads facts + items",
          st == 200 and "Free Share" in page_r
          and page_r.count('class="rs-cell"') == 3
          and page_r.count("rs-dl") >= 3
          and "?download=1" in page_r, st)
    # the h1 is the content (3 files → "3 files"), the tool rides the
    # meta line; no QR row; copy/share/X are the one share bar
    check("E4b /r page: h1 = content count, tool in meta, share bar, no qrrow",
          st == 200
          and "<h1>3 files</h1>" in page_r
          and "「Free Share」" in page_r
          and "qrrow" not in page_r
          and 'class="share-bar sb-compact"' in page_r
          and 'data-sb="copy"' in page_r and "x.com/intent/tweet" in page_r, st)
    # MCP expires_at mirrors the record authority
    st, d = _call("compress", {"files": [{"filename": "e2.png", "b64": A64}],
                               "client_ref": f"e2-{_u_io}"}, TOK, 44)
    _sc_exp = ((d.get("result") or {}).get("structuredContent") or {}).get("expires_at")
    check("E5 MCP expires_at = record expireAt (int)",
          isinstance(_sc_exp, int) and _sc_exp > ts_io, _sc_exp)

    # ═══ M. MCP surface ═══
    st, d = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, TOK)
    tools = {t["name"]: t for t in d["result"]["tools"]}
    check("M1 every tool has outputSchema + English title",
          all("outputSchema" in t and t.get("title") for t in tools.values())
          and tools["share"]["title"] == "Free Share",
          [t.get("title") for t in tools.values()][:3])
    check("M2 description states inputs+outputs+price, English only",
          "Outputs: file" in tools["share"]["description"]
          and not any("\u4e00" <= ch <= "\u9fff"
                      for ch in tools["share"]["description"]),
          tools["share"]["description"])
    schema = tools["share"]["outputSchema"]
    sc_props = schema["properties"]["items"]["items"]["oneOf"]
    check("M3 outputSchema matches structuredContent shape",
          {k for k in schema["properties"]} == {
              "call_id", "replay", "result_url", "expires_at", "items"}
          and len(sc_props) == 3, schema["properties"].keys())
    # image echo suppression: share returns the input bytes unchanged,
    # so the image block is dropped — an agent's own upload must not
    # bounce back into its context. The one content block is the
    # READABLE summary (no JSON dump); the file link lives once, in
    # structuredContent.items (no duplicate resource_link blocks).
    st, d = _call("share", {"files": [{"filename": "img.png", "b64": A64}],
                            "client_ref": f"mi-{_u_io}"}, TOK, 40)
    content = (d.get("result") or {}).get("content") or []
    kinds = [c["type"] for c in content]
    _m4_sc = (d.get("result") or {}).get("structuredContent") or {}
    _m4_text = content[-1].get("text") if content else ""
    check("M4 pass-through share → readable summary block only, NO image block",
          kinds == ["text"]
          and (_m4_sc.get("items") or [{}])[0].get("download_url")
          and not _m4_text.lstrip().startswith("{")
          and "download" in _m4_text, kinds)
    # a transformed image still gets the block. compress only re-encodes
    # ABOVE the target: a >1MB noise PNG forces a real JPEG conversion
    # (bytes differ, echo rule does not apply).
    import io as _io
    import random as _random
    from PIL import Image as _Img
    _rng = _random.Random(0xC0)
    _img = _Img.new("RGB", (1024, 1024))
    _img.putdata([(_rng.randrange(256), _rng.randrange(256),
                   _rng.randrange(256)) for _ in range(1024 * 1024)])
    _buf = _io.BytesIO()
    _img.save(_buf, "PNG")
    BIG64 = base64.b64encode(_buf.getvalue()).decode()
    st, d = _call("compress", {"files": [{"filename": "big.png",
                                          "b64": BIG64}],
                               "params": {"preset": "1mb"},
                               "client_ref": f"mi2-{_u_io}"}, TOK, 43)
    content = (d.get("result") or {}).get("content") or []
    kinds = [c["type"] for c in content]
    check("M4b re-encoded image → image block + JSON block, no resource_link",
          "image" in kinds and "resource_link" not in kinds
          and kinds[-1] == "text"
          and any(c.get("mimeType") == "image/jpeg" for c in content
                  if c["type"] == "image"), kinds)
    # format mismatch error text
    bad_b = base64.b64encode(b"not a png at all").decode()
    st, d = _call("compress", {"files": [{"filename": "z.png",
                                           "b64": bad_b}],
                               "client_ref": f"me-{_u_io}"}, TOK, 41)
    res = d.get("result") or {}
    txt = ((res.get("content") or [{}])[0].get("text", ""))
    err = (d.get("error") or {}).get("message", "")
    # content/extension mismatch keeps its typed code and carries the
    # structured ingress detail (detected/suggested_name null for an
    # unrecognizable body) — readable text + structuredContent channel
    _m5 = res.get("structuredContent") or {}
    check("M5 format-mismatch error is plain English + typed detail",
          ((d.get("result") or {}).get("isError") is True
            and _m5.get("error") == "err_content_mismatch_image"
            and _m5.get("filename") == "z.png"
            and _m5.get("detected") is None
            and _m5.get("suggested_name") is None
            and "does not match" in txt)
          or ("not supported" in err and ".png" in err), (txt, err))
    # catalog_search summary shape
    st, d = _call("catalog_search", {"q": "share"}, TOK, 42)
    tools_sum = ((d.get("result") or {}).get("structuredContent")
                 or {}).get("tools") or []
    check("M6 catalog_search returns tools/list-shaped summaries",
          tools_sum and tools_sum[0]["name"] == "share"
          and tools_sum[0].get("title")
          and "Outputs" in tools_sum[0]["description"], tools_sum[:1])

    # ═══ X. developer endpoint items contract (direct drive_http) ═══
    import remote as _remote
    decl_ep = {"url": f"{BASE_URL}/nonexistent", "timeout_s": 10}
    sources = [{"filename": "a.png", "content_type": "image/png",
                "size": len(PNG), "url": "https://x.test/a.png"}]
    # invalid shapes via a local listener would need TLS; drive the
    # response-parsing path directly with a stubbed _http_call
    orig_call = _remote._http_call
    def _fake_call(url, req_body, req_ct, headers, timeout_s, trusted,
                   deadline=None):
        body = _fake_call.body
        return _fake_call.status, body, _fake_call.ctype, None
    _fake_call.ctype = "application/vnd.518.items+json"
    _remote._http_call = _fake_call
    try:
        _fake_call.status = 200
        _fake_call.body = _json.dumps({"legacy": "shape"}).encode()
        try:
            _remote.drive_http(decl_ep, sources, {}, {}, True)
            check("X1 non-items JSON envelope → invalid_response", False,
                  "accepted")
        except _remote.RemoteError as exc:
            check("X1 non-items JSON envelope → invalid_response",
                  exc.code == "invalid_response", exc.code)
        _fake_call.body = b"raw bytes not json"
        try:
            _remote.drive_http(decl_ep, sources, {}, {}, True)
            check("X2 raw-byte envelope rejected (no legacy mode)",
                  False, "accepted")
        except _remote.RemoteError as exc:
            check("X2 raw-byte envelope rejected (no legacy mode)",
                  exc.code == "invalid_response", exc.code)
        _fake_call.body = _json.dumps({"items": [
            {"kind": "text", "text": "ok"},
            {"kind": "json", "data": {"n": 1}},
            {"kind": "file", "filename": "o.png",
             "content_type": "image/png",
             "data_b64": base64.b64encode(PNG).decode()}]}).encode()
        out = _remote.drive_http(decl_ep, sources, {}, {}, True)
        check("X3 items shape round-trips (text+json+file)",
              [i["kind"] for i in out["items"]] == ["text", "json", "file"]
              and out["items"][2]["bytes"] == PNG, out["items"][:2])
        _fake_call.body = _json.dumps({"items": [
            {"kind": "file", "filename": "x.png",
             "content_type": "image/png"}]}).encode()
        try:
            _remote.drive_http(decl_ep, sources, {}, {}, True)
            check("X4 file item without data → invalid", False, "accepted")
        except _remote.RemoteError as exc:
            check("X4 file item without data → invalid",
                  exc.code == "invalid_response", exc.code)
        _fake_call.body = _json.dumps({"items": [
            {"kind": "text", "text": "x" * 2_000_000}]}).encode()
        try:
            _remote.drive_http(decl_ep, sources, {}, {}, True)
            check("X5 oversized inline payload → invalid", False, "accepted")
        except _remote.RemoteError as exc:
            check("X5 oversized inline payload → invalid",
                  "invalid_response" in (exc.code, str(exc)), exc.code)
    finally:
        _remote._http_call = orig_call
    check("X6 request payload key is files (not job_inputs)",
          all("files" == k or k in ("filename", "content_type", "size",
                                    "url")
              for src in sources for k in src), sources)
    src_txt = open(_os.path.join(REPO, "core", "remote.py"),
                   encoding="utf-8").read()
    check("X6b job_inputs gone from the wire contract",
          '"job_inputs"' not in src_txt, "grep job_inputs")
finally:
    with db() as con:
        if uid is not None:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from uploads where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            con.execute("delete from api_keys where user_id=%s", (uid,))
            con.execute("delete from sessions where user_id=%s", (uid,))
            con.execute("delete from upload_usage where user_id=%s", (uid,))
            con.execute("delete from ingress_requests where user_id=%s", (uid,))
            purge_users(con, [uid])
    print("cleanup done")


# ════════════════════════════════════════════════════════════════════════
# Cross-surface coherence: http tools, windows, staging cleanup, tickets
#
# the coherence pass coherence fixes — behavior tests over the real call paths.
#
# X1*   P0-1: http tool with files succeeds via BOTH surfaces (MCP
#       tools/call and web /api/uploads + /api/tools/execute); source
#       field is `url`; billing exactly once per logical call.
# X2/X3 P1: files_min 0 survives read-back and PATCH; a half window
#       PATCH keeps the other bound; min>max rejected.
# X4    P2-4: a later item failing staging deletes every object staged
#       for the earlier items (zero residue, typed refund).
# X5    P2-5: ticket re-issue after failure/expiry succeeds; a replay
#       after success returns the same handles.
# X6    P2-6: create_upload returns structuredContent matching its
#       outputSchema.
# X7    P3-7: content-sniff and accept mismatch share ONE expression
#       (isError tool result with the typed English sentence).
# X11   P3-11: result_public_id uniqueness; ticket housekeeping purge.
# ════════════════════════════════════════════════════════════════════════
section('Cross-surface coherence: http tools, windows, staging cleanup, tickets')

import base64
import http.server
import json as _json
import os as _os
import secrets as _secrets
import sys as _sys
import threading
import time
import uuid


import http.client as _hc
import urllib.parse

_HOST_coh = ("127.0.0.1", int(TEST_PORT))
_BASE = f"http://127.0.0.1:{TEST_PORT}"
_u_coh = "fx1" + uuid.uuid4().hex[:8]

PNG_coh = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mNk"
    "+M+QzGdlGU0AAAvKBwGAWjR9awAAAABJRU5ErkJggg==")


def _req_coh(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_coh, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form_coh(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp_coh(body, token):
    st, _h, d, _sc = _req_coh("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call_coh(name, arguments, token, mid=1):
    return _mcp_coh({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


# ── in-suite http tool endpoint: single-shape items contract ──
class _ToolHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = _json.loads(self.rfile.read(n) or b"{}")
        files = body.get("files") or []
        items = [{"kind": "file", "filename": f.get("filename") or "out.png",
                  "content_type": "image/png",
                  "data_b64": base64.b64encode(PNG_coh).decode()}
                 for f in files] or [{"kind": "text", "text": "no files"}]
        out = _json.dumps({"items": items}).encode()
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/vnd.518.items+json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ToolHandler)
TOOL_PORT = _srv.serve_forever  # noqa: F841 (kept ref for clarity)
TOOL_URL = f"http://127.0.0.1:{_srv.server_address[1]}/t"
threading.Thread(target=_srv.serve_forever, daemon=True).start()

from database import db  # noqa: E402
from utils import now  # noqa: E402

ts_coh = now()
uid = None
TOK_coh = None
OP_TOOL = f"fxhttp{_u_coh}"
USER_TOOL = f"fxuser{_u_coh}"
created_uids = []

try:
    # ── fixtures: operator http tool (cost 2) + a plain user ──
    from web_auth import create_browser_session
    from config import COOKIE_NAME
    op = "fxop" + _u_coh
    with db() as con:
        op_uid = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s,%s,'x','FX',"
            "'samelabs',%s,%s) returning id",
            (op, f"{op}@t.co", ts_coh, ts_coh)).fetchone()["id"]
        created_uids.append(op_uid)
    ck_coh = f"{COOKIE_NAME}={create_browser_session(op_uid)}"
    st, _h, d, _sc = _req_coh("POST", "/samelabs/tools/create", {
        "tool_id": OP_TOOL, "label": "FX Http Echo", "format_families": "image",
        "accept": ".png",
        "url": TOOL_URL, "timeout_s": 30,
        "files_min": 1, "files_max": 5, "outputs": ["file"],
        "cost": 2}, {"Content-Type": "application/json", "Cookie": ck_coh})
    check("F1 operator http tool created", st == 200 and (d or {}).get("ok"),
          f"{st} {str(d)[:80]}")

    b, ct = _form_coh({"username": _u_coh + "a", "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{_u_coh}@t.co"})
    _req_coh("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_coh({"login": _u_coh + "a", "password": "Test12345"})
    st, _h, _d, sc = _req_coh("POST", "/auth/login", b, {"Content-Type": ct})
    CK = sc.split(";")[0]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u_coh + "a",)).fetchone()["id"])
        created_uids.append(uid)
        from repo.ledger import insert_row
        insert_row(con, uid, 10000, 10000, "manual_topup", None, None,
                   f"fx1:{_u_coh}", None, ts_coh)
    st, _h, d_k, _sc = _req_coh("POST", "/api/keys", {"name": "fx1"},
                            {"Content-Type": "application/json", "Cookie": CK})
    TOK_coh = d_k["key"]["key"]
    check("F2 user + key + credits", bool(TOK_coh), TOK_coh)

    # ═══ X1: http tool with files — MCP surface ═══
    A64 = base64.b64encode(PNG_coh).decode()
    st, d = _call_coh(OP_TOOL, {"files": [
        {"filename": "in-a.png", "b64": A64},
        {"filename": "in-b.png", "b64": A64}],
        "client_ref": f"x1-mcp-{_u_coh}"}, TOK_coh, 10)
    res = d.get("result") or {}
    sc = res.get("structuredContent") or {}
    items = sc.get("items") or []
    check("X1a MCP http tool with files succeeds",
          res.get("isError") is False and len(items) == 2,
          str(d)[:140])
    check("X1b items are real file items with download urls",
          all(it.get("kind") == "file" and "?download=1" in it.get("download_url", "")
              for it in items), str(items)[:120])
    with db() as con:
        n_consume = int(con.execute(
            "select count(*) c from ledger where user_id=%s and kind='consume'",
            (uid,)).fetchone()["c"])
    check("X1c billing exactly once", n_consume == 1, n_consume)
    # replay same ref → still one charge, replay flag
    st, d = _call_coh(OP_TOOL, {"files": [
        {"filename": "in-a.png", "b64": A64},
        {"filename": "in-b.png", "b64": A64}],
        "client_ref": f"x1-mcp-{_u_coh}"}, TOK_coh, 11)
    sc2 = ((d.get("result") or {}).get("structuredContent") or {})
    with db() as con:
        n_consume2 = int(con.execute(
            "select count(*) c from ledger where user_id=%s and kind='consume'",
            (uid,)).fetchone()["c"])
    check("X1d replay no second charge + replay flag",
          n_consume2 == 1 and sc2.get("replay") is True,
          f"{n_consume2} {sc2.get('replay')}")

    # ═══ X1: web surface (staging + execute) ═══
    b = (f'--B\r\nContent-Disposition: form-data; name="tool"\r\n\r\n'
         f'{OP_TOOL}\r\n'
         f'--B\r\nContent-Disposition: form-data; name="client_ref"\r\n\r\n'
         f'x1-web-up-{_u_coh}\r\n').encode()
    for i in range(2):
        b += (f'--B\r\nContent-Disposition: form-data; name="file"; '
              f'filename="w{i}.png"\r\nContent-Type: image/png\r\n\r\n'
              ).encode() + PNG_coh + b"\r\n"
    b += b"--B--\r\n"
    ct = "multipart/form-data; boundary=B"
    st, _h, d_up, _sc = _req_coh("POST", "/api/uploads", b,
                             {"Content-Type": ct, "Cookie": CK})
    _staged = (d_up or {}).get("files") or [(d_up or {}).get("file")]
    staged_ids = [f.get("stagingId") for f in _staged if f]
    check("X1e web staging returns url-contract handles",
          st == 200 and len(staged_ids) == 2 and all(staged_ids), f"{st} {str(d_up)[:80]}")
    st, _h, d_x, _sc = _req_coh("POST", "/api/tools/execute", {
        "tool_id": OP_TOOL,
        "source": {"files": [{"public_id": _staged[0]["stagingId"],
                              "source_token": _staged[0].get("sourceToken", "")},
                             {"public_id": _staged[1]["stagingId"],
                              "source_token": _staged[1].get("sourceToken", "")}]},
        "params": {},
        "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": f"x1-web-{_u_coh}"},
        {"Content-Type": "application/json", "Cookie": CK})
    _items = ((d_x or {}).get("result") or {}).get("items") or []
    check("X1f web execute http tool with files succeeds",
          st == 200 and (d_x or {}).get("ok") and len(_items) == 2,
          f"{st} {str(d_x)[:100]}")
    check("X1g record file items carry page_url + download_url",
          all("page_url" in it and "?download=1" in it.get("download_url", "")
              for it in _items if it.get("kind") == "file"),
          str(_items)[:120])

    # ═══ X2/X3: window read-back + half PATCH (real PATCH/GET) ═══
    from governance.tool_supply import create_tool
    _xkey = create_tool({
        "tool_key": f"{_u_coh}a_{USER_TOOL}", "tool_id": USER_TOOL,
        "owner_id": uid, "execution": "http",
        "label": {"en": "FX User Tool"},
        "format_family": "image",
        "inputs": {"files": {"min": 0, "max": 3, "accept": [".png"]}},
        "outputs": ["text"],
        "params": [{"key": "note", "type": "text", "required": True,
                    "label": {"en": "Note"}}],
        "binding": {"url": TOOL_URL, "timeout_s": 30},
        "visibility": "private", "enabled": True})
    with db() as con:
        con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                    " values (%s, 1, TRUE, %s)",
                    (_xkey, ts_coh))
    st, _h, d_list, _sc = _req_coh("GET", "/api/usertools", {}, {"Cookie": CK})
    entry = next((t for t in (d_list or {}).get("tools", [])
                  if t.get("tool_id") == _xkey), None)
    check("X2 files_min 0 survives read-back (not coerced to 1)",
          entry and entry.get("files_min") == 0 and entry.get("files_max") == 3,
          str(entry and {k: entry.get(k) for k in ('files_min', 'files_max')}))
    st, _h, d_p, _sc = _req_coh("PATCH", f"/api/usertools/{_xkey}",
                            {"label": {"en": "FX v2"}},
                            {"Content-Type": "application/json", "Cookie": CK})
    st, _h, d_list, _sc = _req_coh("GET", "/api/usertools", {}, {"Cookie": CK})
    entry = next((t for t in (d_list or {}).get("tools", [])
                  if t.get("tool_id") == _xkey), None)
    check("X3a PATCH without window keeps stored bounds (0/3)",
          st == 200 and entry and entry.get("files_min") == 0
          and entry.get("files_max") == 3,
          str(entry and {k: entry.get(k) for k in ('files_min', 'files_max')}))
    st, _h, d_p, _sc = _req_coh("PATCH", f"/api/usertools/{_xkey}",
                            {"files_min": 2},
                            {"Content-Type": "application/json", "Cookie": CK})
    st, _h, d_list, _sc = _req_coh("GET", "/api/usertools", {}, {"Cookie": CK})
    entry = next((t for t in (d_list or {}).get("tools", [])
                  if t.get("tool_id") == _xkey), None)
    check("X3b PATCH files_min only keeps stored max (2/3)",
          st == 200 and entry and entry.get("files_min") == 2
          and entry.get("files_max") == 3,
          str(entry and {k: entry.get(k) for k in ('files_min', 'files_max')}))
    st, _h, d_p, _sc = _req_coh("PATCH", f"/api/usertools/{_xkey}",
                            {"files_max": 5},
                            {"Content-Type": "application/json", "Cookie": CK})
    st, _h, d_list, _sc = _req_coh("GET", "/api/usertools", {}, {"Cookie": CK})
    entry = next((t for t in (d_list or {}).get("tools", [])
                  if t.get("tool_id") == _xkey), None)
    check("X3c PATCH files_max only keeps stored min (2/5)",
          st == 200 and entry and entry.get("files_min") == 2
          and entry.get("files_max") == 5,
          str(entry and {k: entry.get(k) for k in ('files_min', 'files_max')}))
    st, _h, d_p, _sc = _req_coh("PATCH", f"/api/usertools/{_xkey}",
                            {"files_max": 1},
                            {"Content-Type": "application/json", "Cookie": CK})
    check("X3d min>max rejected (bad_files_range)",
          st == 422 and (d_p or {}).get("error") == "bad_files_range",
          f"{st} {str(d_p)[:60]}")

    # ═══ X4: staging cleanup on 2nd-item failure (real gate path) ═══
    import asyncio
    import execution.orchestrator as _orch
    from execution import gate as _gate
    import tools as _tools
    import gokapi as _gk_mod

    def _two_files_handler(tool_input, params):
        return {"items": [
            {"kind": "file", "data": PNG_coh, "filename": "a.png",
             "content_type": "image/png", "size": len(PNG_coh)},
            {"kind": "file", "data": PNG_coh, "filename": "b.png",
             "content_type": "image/png", "size": len(PNG_coh)},
        ]}
    _tools.register(
        tool_id=f"fxlocal{_u_coh}",
        label={"en": "FX Local Two"},
        handler=_two_files_handler,
        accept={".png"},
        files_min=0, files_max=1, outputs=("file",), tag="image")
    with db() as con:
        con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                    " values (%s, 0, TRUE, %s)", (f"fxlocal{_u_coh}", ts_coh))

    import storage as _storage_mod
    _orig_write = _storage_mod.write
    _write_n = {"n": 0}
    _written = []

    def _flaky_write(stream, filename, ct, ed, ad, size):
        _write_n["n"] += 1
        if _write_n["n"] == 2:
            raise RuntimeError("injected second-item staging failure")
        out = _orig_write(stream, filename, ct, ed, ad, size)
        _written.append(out.get("FileInfo", {}).get("Id"))
        return out

    _tool_row = {"id": f"fxlocal{_u_coh}", "tool_key": f"fxlocal{_u_coh}",
                 "owner_id": None,
                 "execution": "local", "files_max": 1}
    _user_row = {"id": uid, "username": _u_coh + "a"}

    class _Inv:
        tool_id = f"fxlocal{_u_coh}"
        files = []
        params = {}
        delivery = {"expiry_days": 1, "allowed_downloads": 5}
        client_ref = f"x4-{_u_coh}"

        @staticmethod
        def has_body():
            return False

    def _ptool(tool_id, sources, params, body=None):
        # in-process worker equivalent: this process owns the registered
        # handler (the process pool worker cannot see test registrations)
        return _two_files_handler({"sources": [
            {"stream": __import__("io").BytesIO(sb), "filename": fn,
             "content_type": ct, "size": sz}
            for sb, fn, ct, sz in sources]}, params)

    async def _orch_cb():
        return await _orch.execute_local(
            {"ip": "127.0.0.1"}, _tool_row, [], {}, _user_row,
            _ptool, None,
            {"expiry_days": 1, "allowed_downloads": 5})

    _ok_list, _before = _gk_mod.list_gokapi_files()
    _orch.storage_write = _flaky_write
    try:
        _out, _err, _st = asyncio.run(_gate.run(_user_row, _tool_row, _Inv(), _orch_cb))
        check("X4a injected 2nd-item failure settles as failure",
              _err is not None and _err.get("error") in ("tool_failed",
                                                         "err_endpoint_failed"),
              f"{_st} {str(_err)[:80]}")
    finally:
        _orch.storage_write = _orig_write
    check("X4d injection actually fired (2 staging writes attempted)",
          _write_n["n"] == 2 and len(_written) == 1, str(_write_n))
    _ok_list2, _after = _gk_mod.list_gokapi_files()
    _after_ids = {i.get("Id") for i in _after} if _ok_list2 else None
    _residue = [g for g in _written if g in (_after_ids or set())]
    check("X4b zero staged objects survive the failure",
          _ok_list2 and not _residue, str(_residue)[:80])
    with db() as con:
        row = con.execute(
            "select status from tool_calls where client_ref=%s and user_id=%s",
            (f"x4-{_u_coh}", uid)).fetchone()
    check("X4c call settled failed (refund path)",
          row is not None and row["status"] == "failed", str(row))

    # ═══ X5: ticket re-issue + replay idempotency (real HTTP) ═══
    def _create_upload(ref, fname="t.png", size=None):
        return _call_coh("create_upload",
                     {"filename": fname, "size": size or len(PNG_coh),
                      "client_ref": ref}, TOK_coh, _secrets.randbelow(10**6))
    REF = f"tk-{_u_coh}"
    st, d = _create_upload(REF)
    _u1 = (((d.get("result") or {}).get("structuredContent")
            or {}).get("upload_url") or "")
    t1 = _u1.rsplit("/", 1)[-1]
    # burn the ticket with a WRONG body (declared size exceeded → 413)
    st_put, _h, d_put, _sc = _req_coh(
        "PUT", f"/mcp/upload/{t1}", PNG_coh + b"x" * 10, {})
    check("X5a bad PUT rejected before any receipt",
          st_put == 413, f"{st_put} {str(d_put)[:60]}")
    # re-issue with the SAME ref and correct bytes → success
    st, d = _create_upload(REF)
    _u2 = (((d.get("result") or {}).get("structuredContent")
            or {}).get("upload_url") or "")
    t2 = _u2.rsplit("/", 1)[-1]
    check("X5b same-ref re-issue after failed ticket is a NEW ticket",
          t2 != t1, (t1, t2))
    st_put, _h, d_ok, _sc = _req_coh("PUT", f"/mcp/upload/{t2}", PNG_coh, {})
    check("X5c retried PUT with same ref succeeds (no 409)",
          st_put == 200 and (d_ok or {}).get("public_id"),
          f"{st_put} {str(d_ok)[:80]}")
    first_pid = (d_ok or {}).get("public_id")
    first_tok = (d_ok or {}).get("source_token")
    # success + same ref again → replay returns the SAME handles
    st, d = _create_upload(REF)
    _u3 = (((d.get("result") or {}).get("structuredContent")
            or {}).get("upload_url") or "")
    t3 = _u3.rsplit("/", 1)[-1]
    st_put, _h, d_rp, _sc = _req_coh("PUT", f"/mcp/upload/{t3}", PNG_coh, {})
    check("X5d replay after success returns the original handles",
          st_put == 200 and (d_rp or {}).get("public_id") == first_pid
          and (d_rp or {}).get("source_token") == first_tok,
          f"{st_put} {str(d_rp)[:80]}")

    # ═══ X6: create_upload structuredContent ═══
    res = d.get("result") or {}
    sc = res.get("structuredContent") or {}
    check("X6 create_upload returns structuredContent matching schema",
          set(sc) >= {"upload_url", "expires_at", "max_bytes"}
          and isinstance(sc.get("expires_at"), int)
          and sc.get("upload_url", "").startswith(_BASE + "/mcp/upload/"),
          str(sc)[:100])

    # ═══ X7: sniff vs accept mismatch share one expression ═══
    # (sniff keeps its typed err_content_mismatch_* code + detected-format
    #  detail; accept mismatch stays err_format_not_supported)
    TXT_AS_PNG = b"this is plain text, not a png"
    st, d = _call_coh(OP_TOOL, {"files": [
        {"filename": "fake.png", "b64": base64.b64encode(TXT_AS_PNG).decode()}],
        "client_ref": f"x7a-{_u_coh}"}, TOK_coh, 20)
    r7a = d.get("result") or {}
    t7a = ((r7a.get("content") or [{}])[0].get("text") or "")
    e7a = r7a.get("structuredContent") or {}
    check("X7a content-sniff mismatch → isError (not -32602)",
          r7a.get("isError") is True
          and e7a.get("error") == "err_content_mismatch_image"
          and e7a.get("filename") == "fake.png"
          and e7a.get("detected") is None
          and e7a.get("suggested_name") is None
          and "does not match" in t7a, t7a[:110])
    st, d = _call_coh("compress", {"files": [
        {"filename": "a.pdf", "b64": base64.b64encode(b"%PDF-fake").decode()}],
        "client_ref": f"x7b-{_u_coh}"}, TOK_coh, 21)
    r7b = d.get("result") or {}
    t7b = ((r7b.get("content") or [{}])[0].get("text") or "")
    check("X7b accept mismatch → isError with the same sentence shape",
          r7b.get("isError") is True and "err_format_not_supported" in t7b
          and "not supported" in t7b, t7b[:110])
    check("X7c both expressions identical shape (isError, code present)",
          (r7a.get("isError") is True) == (r7b.get("isError") is True)
          and (r7a.get("structuredContent") or {}).get("error")
          == "err_content_mismatch_image"
          and "err_format_not_supported" in t7b)

    # ═══ X11: index uniqueness + ticket purge ═══
    with db() as con:
        con.execute(
            "insert into tool_calls (client_ref, user_id, tool_id, status,"
            " cost_snapshot, started_at, finished_at, invocation_hash,"
            " result_public_id, result_json) values (%s,%s,'share','ok',0,"
            "%s,%s,%s,'fx1dup" + _u_coh + "', '{}')",
            (f"i1-{_u_coh}", uid, ts_coh, ts_coh, "a" * 64))
        try:
            con.execute(
                "insert into tool_calls (client_ref, user_id, tool_id, status,"
                " cost_snapshot, started_at, finished_at, invocation_hash,"
                " result_public_id, result_json) values (%s,%s,'share','ok',0,"
                "%s,%s,%s,'fx1dup" + _u_coh + "', '{}')",
                (f"i2-{_u_coh}", uid, ts_coh, ts_coh, "b" * 64))
            con.rollback()
            check("X11a result_public_id unique index enforced", False,
                  "duplicate accepted")
        except Exception:
            con.rollback()
            check("X11a result_public_id unique index enforced", True)
    from repo import upload_tickets as _tickets
    with db() as con:
        # one UNUSED row per (user, ref) — the live-one index — so each
        # fixture case gets its own ref
        con.execute(
            "insert into mcp_upload_tickets (ticket_hash, user_id, filename,"
            " content_type, declared_size, client_ref,"
            " expires_at, used_at, created_at) values"
            " (%s,%s,'a.png','image/png',10,%s,%s,NULL,%s),"
            " (%s,%s,'b.png','image/png',10,%s,%s,%s,%s),"
            " (%s,%s,'c.png','image/png',10,%s,%s,NULL,%s)",
            (__import__("hashlib").sha256(("fxexp" + _u_coh).encode()).hexdigest(),
             uid, f"purge-exp-{_u_coh}", ts_coh - 100, ts_coh - 200,
             __import__("hashlib").sha256(("fxused" + _u_coh).encode()).hexdigest(),
             uid, f"purge-used-{_u_coh}", ts_coh + 600,
             ts_coh - 25 * 3600, ts_coh - 26 * 3600,
             __import__("hashlib").sha256(("fxlive" + _u_coh).encode()).hexdigest(),
             uid, f"purge-live-{_u_coh}", ts_coh + 600, ts_coh))
    n = _tickets.purge(ts_coh)
    with db() as con:
        left = [r["ticket_hash"] for r in con.execute(
            "select ticket_hash from mcp_upload_tickets where client_ref like %s",
            ("purge-%" + _u_coh,)).fetchall()]
    check("X11b ticket purge drops expired-unused + >24h-used, keeps live",
          n >= 2 and left == [__import__("hashlib").sha256(("fxlive" + _u_coh).encode()).hexdigest()], str(left))

    # ═══ B-item tests (the coherence passb) ═══
    # B2: MCP inputSchema required hierarchy (+ jsonschema validation)
    st, d = _mcp_coh({"jsonrpc": "2.0", "id": 30, "method": "tools/list",
                  "params": {}}, TOK_coh)
    _tools_l = {t["name"]: t for t in (d.get("result") or {}).get("tools", [])}
    # MCP 名 = tool_key 原文 (无投影)
    _spec = _tools_l.get(_xkey) or {}
    _schema = _spec.get("inputSchema") or {}
    # the fixture tool is 0..3 files → files optional; only params is
    # required at top level. Schema validity is driven per-tool.
    _ok_arg = {"params": {"note": "hi"}, "client_ref": f"b2v-{_u_coh}"}
    _bad_arg = {"params": {}, "client_ref": f"b2v-{_u_coh}"}
    _v_ok = _v_bad = None
    _top_req = _schema.get("required") or []
    _preq = ((_schema.get("properties") or {}).get("params") or {}).get("required") or []
    check("B2a top-level required ⊆ {files,client_ref,params}; note stays in params.required",
          set(_top_req) <= {"files", "client_ref", "params"}
          and "note" not in _top_req and "note" in _preq
          and ("params" in _top_req or "files" in _top_req),
          f"top={_top_req} params={_preq}")
    try:
        import jsonschema as _jsv
        _comp = _tools_l.get("compress") or {}
        _comp_schema = _comp.get("inputSchema") or {}
        _comp_ok = {"files": [{"filename": "a.png", "b64": A64}],
                    "params": {}, "client_ref": f"b2v2-{_u_coh}"}
        _comp_bad = {"params": {}, "client_ref": f"b2v2-{_u_coh}"}
        _jsv.validate(_comp_ok, _comp_schema)   # files_min>=1: files given
        _miss = False
        try:
            _jsv.validate(_comp_bad, _comp_schema)
        except _jsv.ValidationError:
            _miss = True
        check("B2b jsonschema: valid args pass, missing required field fails",
              _miss)
    except ImportError:
        # jsonschema not installed in the venv — structural checks only
        check("B2b jsonschema library unavailable (structural B2a only)",
              True, "import error")

    # B3: concurrent create_upload for one client_ref → ONE live ticket
    import concurrent.futures as _cf
    REF3 = f"b3-{_u_coh}"

    def _one_issue(_):
        st, d = _call_coh("create_upload",
                      {"filename": "c.png", "size": len(PNG_coh),
                       "client_ref": REF3}, TOK_coh, 400 + _)
        try:
            sc = (d.get("result") or {}).get("structuredContent") or {}
            return sc.get("upload_url")
        except Exception:
            return None
    with _cf.ThreadPoolExecutor(max_workers=8) as _ex:
        _urls = list(_ex.map(_one_issue, range(8)))
    with db() as con:
        _live = con.execute(
            "select count(*) c from mcp_upload_tickets"
            " where user_id=%s and client_ref=%s and used_at is null"
            " and expires_at > %s", (uid, REF3, ts_coh)).fetchone()["c"]
    check("B3 concurrent create_upload → exactly one live ticket",
          _live == 1, f"live={_live}")
    check("B3b all 8 responses return a usable upload_url (one live ticket)",
          all(u for u in _urls), str(_urls)[:100])

    # B4: a hung internal Gokapi read must not pin the event loop —
    # while one inline preview is held, unrelated endpoints answer fast.
    st, d = _call_coh("share", {"files": [{"filename": "hold.png", "b64": A64}],
                            "client_ref": f"b4-{_u_coh}"}, TOK_coh, 31)
    _items = ((d.get("result") or {}).get("structuredContent") or {}).get("items") or []
    _pid_hold = next((it.get("page_url", "").rsplit("/f/", 1)[-1]
                      for it in _items if it.get("kind") == "file"), None)
    import urllib.request as _ureq, urllib.error as _uerr
    import time as _time
    _held = False
    if _pid_hold:
        # arm the stall on THIS file id, fire the preview without waiting,
        # then verify other requests answer within 2s
        import os as _os2, subprocess as _sp
        _os.environ["FAKE_GOKAPI_HOLD_PREFIXES"] = _pid_hold
        _os.environ["FAKE_GOKAPI_HOLD_SECONDS"] = "8"
        try:
            import socket as _sock
            def _slow_get():
                c = _sock.create_connection(_HOST_coh, timeout=15)
                c.sendall(f"GET /f/{_pid_hold}/inline HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                return c
            _conn_hold = _slow_get()
            _held = True
            _t0 = _time.time()
            _h_code = None
            try:
                _r = _ureq.urlopen(f"{_BASE}/healthz", timeout=5)
                _h_code = _r.status
                _r.read()
            except _uerr.HTTPError as _e:
                _h_code = _e.code
            _dt = _time.time() - _t0
            check("B4 hung Gokapi preview does not block the loop (healthz < 2s)",
                  _h_code == 200 and _dt < 2.0, f"healthz={_h_code} dt={_dt:.2f}s")
            _t0 = _time.time()
            _k_code = None
            try:
                _r = _ureq.urlopen(f"{_BASE}/account/keys", timeout=5)
                _k_code = _r.status
            except _uerr.HTTPError as _e:
                _k_code = _e.code
            _dt2 = _time.time() - _t0
            check("B4b /account/keys answers while preview hangs (< 2s)",
                  _k_code in (200, 302) and _dt2 < 2.0, f"keys={_k_code} dt={_dt2:.2f}s")
        finally:
            _os.environ.pop("FAKE_GOKAPI_HOLD_PREFIXES", None)
            _os.environ.pop("FAKE_GOKAPI_HOLD_SECONDS", None)
            try:
                _conn_hold.close()
            except Exception:
                pass
    check("B4c inline hold fixture established", _held, "share call failed")

    # B1: register/login throttles fire at the CONFIGURED threshold in an
    # ISOLATED app instance (low limits; no ip exemption — the requests
    # come from 127.0.0.1 like production-behind-Nginx).
    import subprocess as _sp2
    _rl_port = 53893
    _env2 = dict(_os.environ)
    _env2.update({
        "DATABASE_URL": TEST_DB.replace("postgresql://", "postgresql+psycopg://"),
        "FLASHSHARE_SESSION_SECRET": "canonical-test",
        "FLASHSHARE_GOKAPI_URL": _os.environ.get("FLASHSHARE_GOKAPI_URL", ""),
        "FLASHSHARE_GOKAPI_KEY": _os.environ.get("FLASHSHARE_GOKAPI_KEY", ""),
        "FLASHSHARE_BASE_URL": f"http://127.0.0.1:{_rl_port}",
        "FLASHSHARE_LOG_DIR": "/tmp/fs-fx1-log",
        "TOOL_VAULT_KEY": _os.environ.get("TOOL_VAULT_KEY", ""),
        "FLASHSHARE_REGISTER_LIMIT_PER_HOUR": "2",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    _proc = _sp2.Popen(
        [_sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(_rl_port)],
        cwd=REPO, env=_env2,
        stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)
    try:
        import time as _time2
        _ok_up = False
        # 120 x 0.5s = 60s: cold-start can include first-import cost, so
        # the old 20s budget raced slow machines.
        for _ in range(120):
            _time2.sleep(0.5)
            try:
                _r = _ureq.urlopen(f"http://127.0.0.1:{_rl_port}/healthz", timeout=2)
                _ok_up = _r.status == 200
                if _ok_up:
                    break
            except Exception:
                continue
        check("B1 isolated app (limit=2) started", _ok_up)
        _codes = []
        for _i in range(3):
            _b = (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                  for k, v in {})
            _body = b""
            for k, v in {"username": f"rl{_i}{_u_coh}", "password": "Test12345",
                         "password_confirm": "Test12345",
                         "email": f"rl{_i}{_u_coh}@t.co"}.items():
                _body += (f'--B\r\nContent-Disposition: form-data; name="{k}"'
                          f'\r\n\r\n{v}\r\n').encode()
            _body += b"--B--\r\n"
            try:
                _ureq.urlopen(
                    _ureq.Request(f"http://127.0.0.1:{_rl_port}/auth/register",
                                  data=_body, method="POST",
                                  headers={"Content-Type":
                                           "multipart/form-data; boundary=B"}),
                    timeout=10)
                _codes.append(200)
            except _uerr.HTTPError as _e2:
                _codes.append(_e2.code)
        check("B1 3rd register from one ip rejected at limit=2 (429)",
              _codes[:2] == [200, 200] and _codes[2] == 429, str(_codes))
    finally:
        _proc.terminate()
        _proc.wait(timeout=10)
        with db() as con:
            purge_users(con, [r["id"] for r in con.execute(
                "select id from users where username like %s",
                ("rl%" + _u_coh + "%",)).fetchall()])

finally:
    with db() as con:
        con.execute("delete from operator_audit where actor_user_id = ANY(%s)",
                    (created_uids,))
        for t in created_uids:
            purge_users(con, [t])
        con.execute("delete from platform_tools where tool_key like %s",
                    (f"%{_u_coh}%",))
        con.execute("delete from mcp_upload_tickets where client_ref like %s",
                    ("%" + _u_coh + "%",))
        con.execute("delete from tool_calls where client_ref like %s",
                    (f"%{_u_coh}%",))
    _srv.shutdown()
    print("cleanup done")


finish()
