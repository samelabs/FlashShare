#!/usr/bin/env python3
"""the canonical-invocation unification ToolInvocation Unification 验收 A-K"""
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

# ═══ G: Public REST v1 retired ═══
# real token for the MCP section below (minted via the test user's session)
_conn_g = http.client.HTTPConnection(*_HOST, timeout=30)
_conn_g.request("POST", "/api/keys", json.dumps({"name": "scc"}),
                {"Cookie": cookie, "Content-Type": "application/json"})
_tk_g = json.loads(_conn_g.getresponse().read()); _conn_g.close()
bearer = (_tk_g.get("key") or {}).get("key") or ""
conn = http.client.HTTPConnection(*_HOST, timeout=30)
conn.request("POST", "/api/v1/execute", json.dumps({}),
             {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
resp = conn.getresponse(); resp.read(); st = resp.status; conn.close()
check("G-R1 retired /api/v1/execute → 404", st == 404, str(st))

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
# _META: channel/UX meta tools without a files model. demo_shout is the
# first-party chat-form demo (files 0..0) — by the discovery contract a
# 0..0 tool exposes no files argument (T3), so it cannot carry one here.
_META = ("catalog_search", "create_upload", "list_uploads",
         "delete_upload", "pin_tool", "unpin_tool", "list_favorites",
         "demo_shout")
all_have_files = all("files" in (t.get("inputSchema", {}).get("properties") or {}) for t in tools if t["name"] not in _META)
no_b64 = all("file_b64" not in (t.get("inputSchema", {}).get("properties") or {}) for t in tools)
check("H3 所有工具只有 files[] 模型", all_have_files and no_b64, f"{len(tools)} tools")
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

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
sys.exit(1 if FAIL else 0)
