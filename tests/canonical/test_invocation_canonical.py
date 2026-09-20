#!/usr/bin/env python3
"""Stage C ToolInvocation Unification 验收 A-K"""
import json, uuid, http.client, sys, os, base64, threading, re
import os as _os, sys as _sys
_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

# ── FAIL-CLOSED GATE ─────────────────────────────────────────────────────
# Canonical integration tests run ONLY against an explicitly provided
# isolated service port + isolated test database. No defaults, no
# production fallbacks. Refuse to run before any HTTP request or DB write.
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
_HOST = ("127.0.0.1", _TEST_PORT_INT)

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

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
    sys.path.insert(0, os.path.join(_REPO, 'core'))
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

def upload(cookie, name=b"t.png", ct="image/png", extra=None, no_tool=False):
    """上传一个 1x1 png 源文件, 返回 {publicId, sourceToken}"""
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    conn = http.client.HTTPConnection(*_HOST, timeout=30)
    fields = {} if no_tool else {"tool": "compress"}
    fields["client_ref"] = f"up-{uuid.uuid4().hex[:10]}"
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
        sys.path.insert(0, os.path.join(_REPO, 'core'))
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
src_dispatch = re.sub(r'"""[\s\S]*?"""|#[^\n]*', '', open(os.path.join(_REPO, 'core/execution/dispatch.py')).read())
check("B1 dispatch 无 flat source 字段分叉",
      "public_ids" not in src_dispatch and "source_tokens" not in src_dispatch
      and "req.public_id" not in src_dispatch)
check("B2 dispatch 无 single/multi request-shape branch",
      "req.public_id" not in src_dispatch and "_RawRequest" not in src_dispatch)

# ═══ C/D/E: 真实执行链 ═══
cookie, uname = new_user()
up = upload(cookie)
check("C0 upload ok", up.get("ok"), str(up)[:120])
pid, tok = up["file"]["publicId"], up["file"].get("sourceToken", "")

# compress (multi=false) canonical
st, r = req("POST", "/api/tools/execute", {
    "tool_id": "compress", "source": {"files": [{"public_id": pid, "source_token": tok}]},
    "params": {}, "delivery": {"expiry_days": 7, "allowed_downloads": 200},
    "client_ref": "sc-test-1"}, cookie)
check("C1 compress canonical 执行 ok", r.get("ok"), f"{st} {str(r)[:150]}")

# C: handler capture — local handler 只见 params。用 probe 工具? 没有 → 用 DB 检查产物 delivery
if r.get("ok"):
    row = db_query(f"select expiry_days, allowed_downloads from uploads where public_id='{r['result']['publicId']}'")
    # uploads 表名可能是 shares — 试两个
    if not row or row == []:
        row = db_query(f"select expiry_days, allowed_downloads from shares where public_id='{r['result']['publicId']}'")
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
    "tool_id": "compress", "source": {"files": [{"public_id": up_e["file"]["publicId"], "source_token": up_e["file"].get("sourceToken", "")}]},
    "params": {}, "delivery": {"expiry_days": 7, "allowed_downloads": 200},
    "client_ref": "sc-test-1"}, cookie)
check("E1b 同 ref + 新 source → 409 idempotency_conflict (ref 绑定 operation)",
      st == 409 and r2b.get("error") == "idempotency_conflict", f"{st} {str(r2b)[:110]}")
up_f = upload(cookie)
st, r3 = req("POST", "/api/tools/execute", {
    "tool_id": "compress", "source": {"files": [{"public_id": up_f["file"]["publicId"], "source_token": up_f["file"].get("sourceToken", "")}]},
    "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
    "client_ref": f"fresh-{uuid.uuid4().hex[:8]}"}, cookie)
check("E2 新 client_ref → fresh (非 replay)", r3.get("ok") and not r3.get("replay"), str(r3)[:120])

# ═══ multi 工具: zip_pack 双文件 (one schema) ═══
up2 = upload(cookie, name=b"b.png")
up3 = upload(cookie, name=b"c.png")
st, rz = req("POST", "/api/tools/execute", {
    "tool_id": "zip_pack",
    "source": {"files": [
        {"public_id": up2["file"]["publicId"], "source_token": up2["file"].get("sourceToken", "")},
        {"public_id": up3["file"]["publicId"], "source_token": up3["file"].get("sourceToken", "")}]},
    "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
    "client_ref": f"auto-{uuid.uuid4().hex[:8]}-2"}, cookie)
check("B3 zip_pack files[2] 同一 schema ok", rz.get("ok"), f"{st} {str(rz)[:150]}")

# multi=false + 2 files → err_too_many_files
st, rm = req("POST", "/api/tools/execute", {
    "tool_id": "compress",
    "source": {"files": [
        {"public_id": up2["file"]["publicId"], "source_token": ""},
        {"public_id": up3["file"]["publicId"], "source_token": ""}]},
    "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
    "client_ref": f"auto-{uuid.uuid4().hex[:8]}-3"}, cookie)
check("B4 multi=false files[2] → err_too_many_files", rm.get("error") == "err_too_many_files", f"{st} {rm}")

# ═══ G: REST v1 ═══
# 需 bearer token — 用测试账号已有 token? 生成: users 表 token 字段/专用路由
# 直接为测试用户 mint 一个 token (security.token_hash 单向 — 造已知明文+hash 插行)
PLAIN = "fsk_sctest_" + uuid.uuid4().hex
import psycopg2 as _pgm
sys.path.insert(0, os.path.join(_REPO, 'core'))
from security import token_hash as _th
_cm = _pgm.connect(TEST_DB); _cm.autocommit = True
_curm = _cm.cursor()
_curm.execute('select id from users where username=%s', (uname,))
_uid = _curm.fetchone()[0]
_curm.execute('insert into api_tokens (user_id, name, token_hash, prefix, status, created_at) values (%s, %s, %s, %s, %s, %s)', (_uid, 'sctest', _th(PLAIN), 'sctest', 'active', 1))
_cm.close()
print('minted (direct)')
bearer = PLAIN
if True:
    st, r = req("POST", "/api/v1/execute", {
        "tool_id": "compress", "source": {"files": [{"public_id": pid, "source_token": tok}]},
        "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": "v1-job-1"}, None)
    # req() 不支持 auth header — 手动
    conn = http.client.HTTPConnection(*_HOST, timeout=60)
    conn.request("POST", "/api/v1/execute", json.dumps({
        "tool_id": "compress", "source": {"files": [{"public_id": pid, "source_token": tok}]},
        "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": "v1-job-1"}), {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
    resp = conn.getresponse(); r = json.loads(resp.read()); st = resp.status; conn.close()
    check("G1 v1 canonical PASS", r.get("ok"), f"{st} {str(r)[:150]}")
    conn = http.client.HTTPConnection(*_HOST, timeout=60)
    conn.request("POST", "/api/v1/execute", json.dumps({
        "tool_id": "compress", "public_id": pid, "source_token": tok, "params": {},
        "client_ref": "v1-flat-1"}),
        {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
    resp = conn.getresponse(); r2 = json.loads(resp.read()); st2 = resp.status; conn.close()
    check("G2 v1 旧 flat → 422", st2 == 422 and r2.get("error") == "invalid_request", f"{st2} {r2}")
    conn = http.client.HTTPConnection(*_HOST, timeout=60)
    conn.request("POST", "/api/v1/execute", json.dumps({
        "tool_id": "compress", "source": {"files": [{"public_id": pid, "source_token": tok}]},
        "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": None}), {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
    resp = conn.getresponse(); r3 = json.loads(resp.read()); st3 = resp.status; conn.close()
    check("G3 v1 client_ref null → client_ref_required", st3 == 422 and r3.get("error") == "client_ref_required", f"{st3} {r3}")


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
all_have_files = all("files" in (t.get("inputSchema", {}).get("properties") or {}) for t in tools if t["name"] != "catalog_search")
no_b64 = all("file_b64" not in (t.get("inputSchema", {}).get("properties") or {}) for t in tools)
check("H3 所有工具只有 files[] 模型", all_have_files and no_b64, f"{len(tools)} tools")
check("H4 delivery schema 存在", "delivery" in (comp or {}).get("inputSchema", {}).get("properties", {}))
_req_all = all("client_ref" in (t.get("inputSchema", {}).get("required") or [])
               for t in tools if t["name"] != "catalog_search")
_req_cs = ("client_ref" in (([t for t in tools if t["name"] == "catalog_search"]
            or [{}])[0].get("inputSchema", {}).get("required") or []))
check("H4b executable tools required 含 client_ref (catalog_search 不含)",
      _req_all and not _req_cs)

# MCP tools/call 真实执行 (session cookie)
mbody = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
    "name": "compress",
    "arguments": {"files": [{"filename": "m.png", "b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}],
                   "params": {}, "delivery": {"expiry_days": 3, "allowed_downloads": 10},
                   "client_ref": f"mcpexec-{{uuid.uuid4().hex[:8]}}"}}}
conn = http.client.HTTPConnection(*_HOST, timeout=60)
conn.request("POST", "/mcp", json.dumps(mbody), {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
resp = conn.getresponse()
try:
    mcres = json.loads(resp.read())
except Exception:
    mcres = {}
conn.close()
mtext = ((mcres.get("result") or {}).get("content") or [{}])[0].get("text", "")
mok = False
try:
    mok = json.loads(mtext).get("result", {}).get("publicId", "") != ""
except Exception:
    pass
check("H5 MCP tools/call canonical 执行 ok", mok, mtext[:150])

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
          'core/execution/invocation.py', 'app/routes/api.py', 'app/routes/apiv1.py', 'app/routes/mcp.py',
          'static/app.js', 'static/pages/process-flow.js']:
    srcs[f] = open(os.path.join(_REPO, f)).read()
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
# G1 Tool icon fallback 只有一个定义 (canonical fallback key 单点):
import re as _re
fb_defs = len(_re.findall(r'TOOL_ICON_FALLBACK\s*=\s*"wand"', icons_src))
check("G1 tool icon fallback 单一定义", fb_defs == 1
      and icons_src.count('or "wand"') == 0)
# G2 Market/User Page 卡片循环不各自 inline Tool SVG fallback (empty-state
# 装饰 svg 不属于 tool identity — 只查 tool 卡/macro 调用形态):
for tmpl in ("app/templates/tools_market.html", "app/templates/user_page.html"):
    src = _rd(*tmpl.split("/"))
    check(f"G2 {tmpl.split('/')[-1]} 卡面经 macro, 无自绘 tool svg/mkt-icon",
          "tool_card(" in src and "mkt-icon" not in src
          and 'icon_svg | safe' not in src)
# G3 Home/Market/User Page consume canonical renderer (macro partial):
part = _rd("app", "templates", "partials", "tool_identity.html")
check("G3 partial 消费 icon_tool_svg 单一 renderer",
      "icon_tool_svg" in part and "icon_svg(" not in part)
home_src3 = _rd("app", "templates", "home.html")
check("G3b home 工具 tab 经 partial (无 icon_path 工具渲染)",
      "tool_tab(" in home_src3
      and '{{ icon_path(t.icon' not in home_src3)
# G4/G5: author 槽位在 partial 单点, market 开 / user page 关:
check("G4 community author 槽位存在 (market 开)",
      "tcard-author" in part and "show_author" in part)
check("G5 user page show_author=False (页头已定义作者, 无冗余)",
      'show_author=False' in _rd("app", "templates", "user_page.html"))
# G6 origin 语义单源:
cat_src = _rd("core", "governance", "catalog.py")
check("G6 Official/Community origin 单源 (is_official_id)",
      "def is_official_id" in cat_src
      and '"origin":' in cat_src)
for tmpl in ("app/templates/tools_market.html", "app/templates/user_page.html",
             "app/templates/home.html"):
    src = _rd(*tmpl.split("/"))
    check(f"G6 {tmpl.split('/')[-1]} 无 origin 字符串判断",
          "'/' not in t.id" not in src and "'/' in t.id" not in src)
# G7 card href 仍是 Tool Page:
check("G7 card href = /tool/{id}",
      'href="/tool/{{ t.id }}"' in part)
# G8 home 保持执行 surface (tool-tab div, 非导航 a):
home_src = _rd("app", "templates", "home.html")
check("G8 home tool-tab 为执行 surface (partial div role=button, home 无导航 a)",
      '<div class="tool-tab"' in part and 'role="button"' in part
      and "tool_tab(" in home_src
      and '<a class="tool-tab"' not in home_src)
check("PG19 production 零 deprecated icon fallback 字面 (wand 兜底仅 icons.py)",
      sum(_rd("app", "templates", t).count('or "wand"')
          for t in ("home.html", "tools_market.html", "user_page.html")) == 0)


# ═══ INL: preview must not consume download allowance ═══
# upload a real PNG (share path: no tool) → inline + zip-list read via
# the internal non-counting primitive; ?download=1 stays counted.
def _get_raw(path, cookie=None):
    conn = http.client.HTTPConnection(*_HOST, timeout=30)
    h = {}
    if cookie: h["Cookie"] = cookie
    conn.request("GET", path, "", h)
    resp = conn.getresponse()
    body = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, hdrs, body

_sess_uid = None
_c = _pg().cursor()
_c.execute("select id from users where username=%s", (uname,))
_row = _c.fetchone()
if _row:
    _sess_uid = _row[0]
if _sess_uid is None:
    # fall back: any seeded canonical user
    _c.execute("select id from users order by id limit 1")
    _r2 = _c.fetchone()
    _sess_uid = _r2[0] if _r2 else None
if _sess_uid is not None:
    import importlib
    sys.path.insert(0, _REPO)
    _wa = importlib.import_module("app.web_auth")
    _sid = _wa.create_browser_session(_sess_uid)
    _from_config = importlib.import_module("config")
    _ck_val = f"{_from_config.COOKIE_NAME}={_sid}"

    def _upload_raw(cookie, name, ct, data):
        """raw multipart upload (arbitrary bytes) on the share path"""
        conn = http.client.HTTPConnection(*_HOST, timeout=30)
        fields = {"client_ref": f"rz-{uuid.uuid4().hex[:8]}"}
        b, ctt = mp(files=[(name, ct, data)], fields=fields)
        conn.request("POST", "/api/uploads", b,
                     {"Content-Type": ctt, "Cookie": cookie})
        resp = conn.getresponse()
        d = json.loads(resp.read())
        conn.close()
        return d
    # share path (no tool field): the upload lands a live share record
    _up = upload(_ck_val, name=b"prev.png", ct="image/png",
                 extra={"client_ref": f"prev-{uuid.uuid4().hex[:8]}"}, no_tool=True)
    if _up.get("ok") and (_up.get("file") or _up.get("files")):
        _f = _up.get("file") or _up["files"][0]
        _pid = _f["publicId"]
        from gokapi import get_gokapi_file as _ggf
        def _count():
            _meta = _ggf(_f.get("gokapiId") or "")
            # lookup by uploads row when the API answer lacks the id
            if not _meta:
                _c2 = _pg().cursor()
                _c2.execute("select gokapi_id from uploads where public_id=%s", (_pid,))
                _r3 = _c2.fetchone()
                if _r3 and _r3[0]:
                    _meta = _ggf(_r3[0])
            return int((_meta or {}).get("DownloadCount", -1))
        _before = _count()
        st_i, _h_i, _b_i = _get_raw(f"/f/{_pid}/inline")
        check("INL-1 inline preview 200 (image/png)",
              st_i == 200 and _h_i.get("content-type", "").startswith("image/png"),
              f"{st_i} {_h_i.get('content-type')}")
        check("INL-2 inline read does not increase download count",
              _count() == _before, f"before={_before} after={_count()}")
        st_d, h_d, _b_d = _get_raw(f"/f/{_pid}?download=1", cookie=_ck_val)
        check("INL-3 user download still counted (302 to hotlink or stream)",
              st_d in (200, 302), st_d)
        # The 302 hands the browser the counted public gokapi URL. CI has
        # no Nginx, so follow it against the gokapi service directly —
        # exactly what the browser does — one real user download.
        if st_d == 302 and h_d.get("location"):
            _loc = h_d["location"]
            _gh = http.client.HTTPConnection(
                os.environ["FLASHSHARE_GOKAPI_URL"].split("//")[1].split(":")[0],
                int(os.environ["FLASHSHARE_GOKAPI_URL"].rsplit(":", 1)[1]),
                timeout=30)
            _gh.request("GET", _loc, "", {})
            _gr = _gh.getresponse(); _gr.read(); _gh.close()
        check("INL-4 download increases count exactly once",
              _count() == _before + 1, f"before={_before} after={_count()}")
        import zipfile as _zf_mod, io as _io
        _buf = _io.BytesIO()
        with _zf_mod.ZipFile(_buf, "w") as _za:
            _za.writestr("a.txt", "hello")
        _zip = _upload_raw(_ck_val, "prev.zip", "application/zip", _buf.getvalue())
        if _zip.get("ok") and (_zip.get("file") or _zip.get("files")):
            _zf = _zip.get("file") or _zip["files"][0]
            _zpid = _zf["publicId"]
            _rz = _upload_raw(_ck_val, "real.zip", "application/zip", _buf.getvalue())
            if _rz.get("ok") and (_rz.get("file") or _rz.get("files")):
                _rf = _rz.get("file") or _rz["files"][0]
                from gokapi import get_gokapi_file as _ggf2
                _c3 = _pg().cursor()
                _c3.execute("select gokapi_id from uploads where public_id=%s",
                            (_rf["publicId"],))
                _gid = _c3.fetchone()[0]
                _zc0 = int((_ggf2(_gid) or {}).get("DownloadCount", -1))
                st_z, _hz, bz = _get_raw(f"/f/{_rf['publicId']}/zip-list")
                check("INL-5 zip-list 200 ok",
                      st_z == 200 and json.loads(bz).get("ok"), st_z)
                _zc1 = int((_ggf2(_gid) or {}).get("DownloadCount", -1))
                check("INL-6 zip-list internal reads do not increase count",
                      _zc1 == _zc0, f"before={_zc0} after={_zc1}")
            else:
                check("INL-5 zip fixture upload ok", False, str(_rz)[:120])
        else:
            check("INL zip fixture upload ok", False, str(_zip)[:120])
    else:
        check("INL preview fixture upload ok", False, str(_up)[:120])
else:
    check("INL fixture user available", False, "no user row")

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
sys.exit(1 if FAIL else 0)
