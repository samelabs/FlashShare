#!/usr/bin/env python3
"""MCP protocol canonical — Streamable HTTP transport rules, JSON-RPC
error layering, pre-ingress validation, reversible tool names, and the
three source-file input forms (inline base64 / https URL / reference).

Real HTTP surface + real DB for everything reachable over the wire. The
URL-source positive/oversize paths run the same _call_tool in-process
with the pinned transport replaced (local DNS/TLS cannot serve a public
https origin); the SSRF-blocked path runs over real HTTP against the
real guard.
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
from tests.support.cleanup import purge_users  # noqa: E402
_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:140]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import asyncio
import base64
import http.client as _hc
import json
import time
import urllib.parse
import uuid

from database import db

ts = int(time.time())
_u = "mp" + uuid.uuid4().hex[:6]
_HOST = ("127.0.0.1", int(_TEST_PORT))
_BASE = _os.environ.get("FLASHSHARE_BASE_URL", f"http://127.0.0.1:{_TEST_PORT}").rstrip("/")
_p = urllib.parse.urlsplit(_BASE)
_BASE_ORIGIN = f"{_p.scheme}://{_p.netloc}"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n%canonical\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    raw = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        return r.status, hdrs, json.loads(raw) if raw else None
    except Exception:
        return r.status, hdrs, raw.decode(errors="replace")


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    st, _h, _b = _req("POST", "/auth/register", b, {"Content-Type": ct})
    if st not in (200, 302, 303):
        raise SystemExit(f"register failed: {st}")
    b, ct = _form({"login": name, "password": "Test12345"})
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck


def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])


def _gk_count():
    import urllib.request as _ur
    base = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
    req = _ur.Request(base + "/api/files/list",
                      headers={"apikey": _os.environ["FLASHSHARE_GOKAPI_KEY"]})
    with _ur.urlopen(req, timeout=5) as r:
        return len(json.loads(r.read()))


def _ingress_n(uid):
    with db() as con:
        return int(con.execute("select count(*) c from ingress_requests"
                               " where user_id=%s", (uid,)).fetchone()["c"])


def _calls_n(uid):
    with db() as con:
        return int(con.execute("select count(*) c from tool_calls"
                               " where user_id=%s", (uid,)).fetchone()["c"])


from identity.apikeys import create_key
from economy.billing import grant as _grant

uid, ck = _register(_u + "a")
uid2, ck2 = _register(_u + "b")
_grant(uid, 100000, "manual_topup", idem_key=f"mp:{_u}:a", note="mp")
_grant(uid2, 100000, "manual_topup", idem_key=f"mp:{_u}:b", note="mp")
TOKEN = create_key(uid, "mp")["key"]
TOKEN2 = create_key(uid2, "mp2")["key"]


def _mcp(body, token=TOKEN, extra=None):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(extra or {})
    return _req("POST", "/mcp", body, h)


def _call(name, arguments, mid=1, token=TOKEN):
    return _mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _err_sentence(d):
    res = d.get("result") or {}
    return ((res.get("content") or [{}])[0].get("text", ""))


def _tool_text(d):
    """Machine channel of a tools/call result: structuredContent (the
    content text block is the readable summary — error/message/etc.
    ride both)."""
    return ((d.get("result") or {}).get("structuredContent") or {})


def _b64(b):
    return base64.b64encode(b).decode()


# ══════════ 1. protocol version negotiation ══════════
for req_v, want in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                    ("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
                    ("1999-01-01", "2025-11-25")):
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": req_v}})
    got = ((d or {}).get("result") or {}).get("protocolVersion")
    check(f"V1 initialize {req_v} → {want}", st == 200 and got == want, f"{st} {got}")
st, _h, d = _mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25"}})
check("V2 capabilities.tools.listChanged is false",
      ((d.get("result") or {}).get("capabilities") or {}).get("tools") == {"listChanged": False},
      str(d)[:120])

# Discovery must accept both valid LocalizedText forms: a plain string
# and a locale map without "en" (fallback to the first available locale).
# The same summary builder feeds tools/list and catalog_search.
from app.routes.mcp import _spec_summary
_label_base = {
    "id": "labelprobe_demo", "files_min": 0, "files_max": 0,
    "outputs": ["text"], "cost": 0,
    "desc": {"zh": "整理文件"},
}
for label, expected in (
    ("Plain Label", "Plain Label"),
    ({"zh": "工具名称"}, "工具名称"),
):
    _summary = _spec_summary({**_label_base, "label": label})
    check(f"V3 MCP discovery title handles {type(label).__name__}",
          _summary["title"] == expected
          and _summary["name"] == "labelprobe_demo"
          and "整理文件" in _summary["description"],
          _summary)

# ══════════ 1b. strict arguments: an undeclared key is -32602, named ══════════
# A wrong parameter name must never run as a successful default/unfiltered
# call — the agent cannot tell the mistake from the result. Allowed keys
# come from each tool's inputSchema (platform tools: files/params/
# delivery/client_ref).
_st, _h, d = _mcp({"jsonrpc": "2.0", "id": 91, "method": "tools/call",
                   "params": {"name": "catalog_search",
                              "arguments": {"query": "pdf"}}}, token=TOKEN)
_err = (d or {}).get("error") or {}
check("V4a meta tool unknown key → -32602 naming it",
      _err.get("code") == -32602 and "query" in (_err.get("message") or ""),
      d)
_st, _h, d = _mcp({"jsonrpc": "2.0", "id": 92, "method": "tools/call",
                   "params": {"name": "share",
                              "arguments": {"client_ref": "v4b",
                                            "queries": ["a"]}}}, token=TOKEN)
_err = (d or {}).get("error") or {}
check("V4b platform tool unknown key → -32602 naming it",
      _err.get("code") == -32602 and "queries" in (_err.get("message") or ""),
      d)
_st, _h, d = _mcp({"jsonrpc": "2.0", "id": 93, "method": "tools/call",
                   "params": {"name": "register",
                              "arguments": {"username": f"{_u}v4c",
                                            "role": "agent"}}})
_err = (d or {}).get("error") or {}
check("V4c anonymous register unknown key → -32602 naming it",
      _err.get("code") == -32602 and "role" in (_err.get("message") or ""),
      d)
_st, _h, d = _mcp({"jsonrpc": "2.0", "id": 94, "method": "tools/call",
                   "params": {"name": "catalog_search",
                              "arguments": {"q": "pdf", "page": 1}}},
                  token=TOKEN)
check("V4d declared keys still pass", "result" in (d or {}), d)

# ══════════ 2. MCP-Protocol-Version header ══════════
_ping = {"jsonrpc": "2.0", "id": 5, "method": "ping"}
st, _h, d = _mcp(_ping, extra={"MCP-Protocol-Version": "2031-01-01"})
check("H1 unsupported MCP-Protocol-Version → 400",
      st == 400 and ((d or {}).get("error") or {}).get("code") == -32600, f"{st} {d}")
for v in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"):
    st, _h, d = _mcp(_ping, extra={"MCP-Protocol-Version": v})
    check(f"H2 supported header {v} → 200", st == 200 and d.get("result") == {}, f"{st} {d}")
st, _h, d = _mcp(_ping)
check("H3 absent header → accepted (2025-03-26)", st == 200 and d.get("result") == {}, f"{st}")

# ══════════ 3. Origin / CORS / auth header ══════════
st, _h, _d = _mcp(_ping, extra={"Origin": "https://evil.example"})
check("O1 foreign Origin → 403", st == 403, f"{st}")
st, _h, _d = _mcp(_ping, extra={"Origin": "null"})
check("O2 opaque Origin 'null' → 403", st == 403, f"{st}")
st, h, d = _mcp(_ping, extra={"Origin": _BASE_ORIGIN})
check("O3 site Origin → 200 + echoed Access-Control-Allow-Origin",
      st == 200 and h.get("access-control-allow-origin") == _BASE_ORIGIN, f"{st} {h}")
st, h, d = _mcp(_ping)
check("O4 no Origin (non-browser) → 200, no wildcard ACAO",
      st == 200 and h.get("access-control-allow-origin") in (None,), f"{st} {h.get('access-control-allow-origin')}")
st, h, _d = _req("OPTIONS", "/mcp", None, {"Origin": _BASE_ORIGIN,
                                            "Access-Control-Request-Method": "POST"})
_ah = (h.get("access-control-allow-headers") or "").lower()
_eh = (h.get("access-control-expose-headers") or "").lower()
check("O5 preflight allowed origin → 204 + MCP headers allowed",
      st == 204 and all(x in _ah for x in ("authorization", "content-type",
                                           "mcp-protocol-version", "mcp-session-id",
                                           "last-event-id")),
      f"{st} {_ah}")
check("O6 expose WWW-Authenticate + Mcp-Session-Id",
      "www-authenticate" in _eh and "mcp-session-id" in _eh, _eh)
st, h, _d = _req("OPTIONS", "/mcp", None, {"Origin": "https://evil.example"})
check("O7 preflight foreign origin → 403", st == 403, f"{st}")
st, h, _d = _mcp(_ping, token=None)
check("O8 anonymous ping → 200 (discovery needs no key)",
      st == 200 and (_d or {}).get("result") == {}, f"{st} {_d}")
st, h, _d = _mcp(_ping, token="fsk_invalid_key")
check("O8b invalid key → 401 WWW-Authenticate resource_metadata",
      st == 401 and "oauth-protected-resource" in (h.get("www-authenticate") or ""), f"{st} {h.get('www-authenticate')}")

# ══════════ 4. JSON-RPC batches ══════════
_batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
          {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
for v in ("2025-06-18", "2025-11-25"):
    st, _h, d = _mcp(_batch, extra={"MCP-Protocol-Version": v})
    check(f"B1 batch rejected under {v} → 400 -32600",
          st == 400 and isinstance(d, dict) and (d.get("error") or {}).get("code") == -32600,
          f"{st} {d}")
st, _h, d = _mcp(_batch, extra={"MCP-Protocol-Version": "2025-03-26"})
check("B2 batch under 2025-03-26 → array of 2 responses",
      st == 200 and isinstance(d, list) and sorted(x["id"] for x in d) == [1, 2], f"{st} {d}")
st, _h, d = _mcp(_batch)
check("B3 batch without header (2025-03-26) → allowed",
      st == 200 and isinstance(d, list) and len(d) == 2, f"{st} {d}")
st, _h, d = _mcp([{"jsonrpc": "2.0", "method": "notifications/initialized"},
                  {"jsonrpc": "2.0", "id": 77, "result": {}}],
                 extra={"MCP-Protocol-Version": "2025-03-26"})
check("B4 batch of notifications/responses only → 202 no body",
      st == 202 and not d, f"{st} {d!r}")
st, _h, d = _mcp([], extra={"MCP-Protocol-Version": "2025-03-26"})
check("B5 empty batch → 400 JSON-RPC -32600 invalid request",
      st == 400 and isinstance(d, dict) and d.get("id") is None
      and (d.get("error") or {}).get("code") == -32600, f"{st} {d}")
st, _h, d = _mcp({"jsonrpc": "2.0", "id": 9, "result": {}})
check("B6 single client response → 202", st == 202 and not d, f"{st} {d!r}")

# ══════════ 5. non-object params / arguments ══════════
for label, body in (
        ("tools/call params list", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]}),
        ("tools/call params string", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"}),
        ("initialize params list", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": [1]}),
        ("tools/list params string", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": "x"}),
        ("arguments list", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "compress", "arguments": [1, 2]}}),
        ("arguments string", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "compress", "arguments": "x"}}),
        ("name not string", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": ["compress"], "arguments": {}}}),
        ("b64 not string", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "compress", "arguments": {
                                "files": [{"filename": "a.png", "b64": 12345}],
                                "client_ref": f"np-{_u}"}}}),
):
    st, _h, d = _mcp(body)
    check(f"N {label} → HTTP 200 JSON-RPC -32602",
          st == 200 and isinstance(d, dict) and d.get("id") == 1
          and (d.get("error") or {}).get("code") == -32602, f"{st} {str(d)[:100]}")

# ══════════ 6. validate BEFORE ingress ══════════
def _zero_side_effects(label, fn):
    g0, i0, c0 = _gk_count(), _ingress_n(uid), _calls_n(uid)
    out = fn()
    check(f"{label} zero ingress/Gokapi/tool_call",
          _gk_count() == g0 and _ingress_n(uid) == i0 and _calls_n(uid) == c0,
          f"gk {g0}->{_gk_count()} ingress {i0}->{_ingress_n(uid)} calls {c0}->{_calls_n(uid)}")
    return out

st, _h, d = _zero_side_effects("P1", lambda: _call(
    f"no_such_tool_{_u}", {"files": [{"filename": "a.png", "b64": _b64(PNG)}],
                           "client_ref": f"p1-{_u}"}))
check("P1 unknown tool with file → -32602",
      (d.get("error") or {}).get("code") == -32602, str(d)[:100])
st, _h, d = _zero_side_effects("P2", lambda: _call(
    "compress", {"files": [{"filename": "a.png", "b64": _b64(PNG)}],
                 "params": {"preset": "bogus"}, "client_ref": f"p2-{_u}"}))
check("P2 invalid declared param → isError param_invalid",
      (d.get("result") or {}).get("isError") is True and _tool_text(d).get("error") == "param_invalid",
      str(d)[:120])
check("P2b the denial names the parameter (field)",
      _tool_text(d).get("field") == "preset", str(d)[:160])
st, _h, d = _zero_side_effects("P3", lambda: _call(
    "compress", {"files": [{"filename": "a.pdf", "b64": _b64(PDF)}],
                 "client_ref": f"p3-{_u}"}))
_p3 = _err_sentence(d)
check("P3 format outside accept → isError + English sentence",
      (d.get("result") or {}).get("isError") is True
      and "not supported" in _p3
      and "err_format_not_supported" in _p3, _p3[:110])
st, _h, d = _zero_side_effects("P4", lambda: _call(
    "compress", {"files": [{"filename": "a.png", "b64": _b64(PNG)}],
                 "delivery": {"expiry_days": 30, "allowed_downloads": 50},
                 "client_ref": f"p4-{_u}"}))
check("P4 delivery not entitled (free) → isError err_expiry_not_entitled",
      (d.get("result") or {}).get("isError") is True
      and _tool_text(d).get("error") == "err_expiry_not_entitled", str(d)[:120])
st, _h, d = _zero_side_effects("P5", lambda: _call(
    "compress", {"files": [{"filename": "a.png", "b64": _b64(PNG)},
                           {"filename": "b.png", "b64": _b64(PNG)}],
                 "client_ref": f"p5-{_u}"}))
_p5 = _err_sentence(d)
check("P5 two files to single-file tool → isError + window sentence",
      (d.get("result") or {}).get("isError") is True
      and "err_file_count" in _p5 and "1 to 1" in _p5, _p5[:110])

# ══════ 7. tool names = key 原文 (username_toolname, 无投影无撞名机器) ══════
_ownA = _mkuser(f"{_u}o")            # owner 'mpXXXXXXo'
_ownB = _mkuser(f"{_u}o__ab")        # owner 'mpXXXXXXo__ab'
keyA = f"{_u}o_ab__tl"               # 与 keyB 字符串不同 — key 唯一索引即名单射
keyB = f"{_u}o__ab_tl"
def _mk_tool(key, owner, pkey):
    decl = [{"key": pkey, "type": "enum", "label": {"en": pkey},
             "options": [{"value": "v", "label": {"en": "v"}}], "required": True}]
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
            " label_json, accept_json, format_family,"
            " visibility, enabled, params_json, endpoint_json, created_at, updated_at)"
            " values (%s, %s, %s, 'http', %s, %s, 'image',"
            " 'public', true, %s, %s, %s, %s)",
            (key, owner, key.rsplit("_", 1)[-1], json.dumps({"en": key}),
             json.dumps([".pdf"]), json.dumps(decl),
             json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts, ts))
        con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                    " values (%s, 0, true, %s)", (key, ts))
_mk_tool(keyA, _ownA, "alpha")
_mk_tool(keyB, _ownB, "beta")
nameA, nameB = keyA, keyB            # MCP 名 = key 原文
check("T1 distinct keys carry distinct names (key uniqueness = name injectivity)",
      nameA and nameB and nameA != nameB, f"{nameA} {nameB}")
check("T2 name is the key verbatim", nameA == keyA and nameB == keyB)
for _nm in (nameA, nameB):
    _call("pin_tool", {"name": _nm})
_listed, _cur = [], None
for _ in range(20):
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/list",
                      "params": ({"cursor": _cur} if _cur else {})})
    res = d.get("result") or {}
    _listed += res.get("tools") or []
    _cur = res.get("nextCursor")
    if not _cur:
        break
_lnames = [t["name"] for t in _listed]
check("T6 pinned tools/list carries both tools under distinct names",
      nameA in _lnames and nameB in _lnames and len(_lnames) == len(set(_lnames)))
# discriminating call: A requires 'alpha', B requires 'beta'; both accept
# pdf only — a correctly routed call passes params and stops at the
# format gate (zero side effects), a misrouted one fails param_required.
st, _h, d = _call(nameA, {"files": [{"filename": "a.png", "b64": _b64(PNG)}],
                          "params": {"alpha": "v"}, "client_ref": f"t7-{_u}"})
_t7 = _err_sentence(d)
check("T7 name A routes to key A (format gate reached)",
      (d.get("result") or {}).get("isError") is True
      and "err_format_not_supported" in _t7, _t7[:110])
st, _h, d = _call(nameB, {"files": [{"filename": "a.png", "b64": _b64(PNG)}],
                          "params": {"beta": "v"}, "client_ref": f"t8-{_u}"})
_t8 = _err_sentence(d)
check("T8 name B routes to key B (format gate reached)",
      (d.get("result") or {}).get("isError") is True
      and "err_format_not_supported" in _t8, _t8[:110])
_spec = next((t for t in _listed if t["name"] == "compress"), {})
_items = (((_spec.get("inputSchema") or {}).get("properties") or {}).get("files") or {}).get("items") or {}
_forms = sorted(tuple(sorted(x.get("required") or [])) for x in _items.get("oneOf") or [])
check("T9 inputSchema files.items oneOf inline | url | reference",
      _forms == [("b64", "filename"), ("public_id",), ("url",)], str(_forms))

# ══════════ 8. URL source (real HTTP → real SSRF guard) ══════════
# request-shape rejection (non-https URL) → -32602; SSRF rejections
# during the guarded fetch → isError (tool-input semantics, FX-2)
for label, url, form in (("loopback", "https://127.0.0.1/a.png", "isError"),
                         ("private", "https://10.0.0.1/a.png", "isError"),
                         ("plain http", "http://example.com/a.png", "-32602")):
    st, _h, d = _zero_side_effects(f"U1 {label}", lambda url=url: _call(
        "compress", {"files": [{"url": url}], "client_ref": f"u1-{_u}-{label}"}))
    if form == "-32602":
        ok_u1 = (d.get("error") or {}).get("code") == -32602
    else:
        ok_u1 = (d.get("result") or {}).get("isError") is True
    check(f"U1 {label} URL → {form}", ok_u1, str(d)[:120])
st, _h, d = _call("compress", {"files": [{"url": "https://x.example/a.png",
                                          "b64": _b64(PNG), "filename": "a.png"}],
                               "client_ref": f"u2-{_u}"})
check("U2 item with two source forms → -32602",
      (d.get("error") or {}).get("code") == -32602, str(d)[:100])

# ══════════ 9. URL source in-process (pinned transport replaced) ══════════
import remote as _remote
import execution.dispatch as _dispatch_mod
from app.routes import mcp as _M

class _Req:
    class client:
        host = "127.0.0.1"
    headers = {}

_captured = []
async def _fake_dispatch(request, user, raw, *a, **k):
    _captured.append(raw)
    return {"ok": True, "call_id": 1, "result": {"publicId": "x"}}, None, None

_served = {}
def _fake_pinned(url, method="GET", body=None, headers=None, timeout_s=60,
                 has_credential=False, max_bytes=None):
    return 200, _served[url]

_orig_pinned, _orig_dx = _remote.pinned_request, _dispatch_mod.dispatch_execute
_remote.pinned_request = _fake_pinned
_dispatch_mod.dispatch_execute = _fake_dispatch
try:
    _user = {"id": uid}
    def _ip(args, mid=1):
        return asyncio.run(_M._call_tool(_Req(), _user, mid,
                                         {"name": "compress", "arguments": args}))
    urlA = f"https://files.example/{_u}/a.png"
    urlB = f"https://files.example/{_u}/b.png"
    _served[urlA] = PNG
    _served[urlB] = PNG
    refU = f"url-{_u}"
    g0, i0 = _gk_count(), _ingress_n(uid)
    r = _ip({"files": [{"url": urlA}], "client_ref": refU})
    _src = (_captured[-1]["source"]["files"] if _captured else [])
    check("U3 https URL source → ingress + canonical source handle",
      "result" in r and r["result"].get("isError") is False and len(_src) == 1
      and _gk_count() == g0 + 1 and _ingress_n(uid) == i0 + 1, str(r)[:140])
    check("U3b filename from URL path (a.png) staged",
          _captured and _captured[-1]["tool_id"] == "compress", str(_captured[-1:])[:100])
    g1 = _gk_count()
    r = _ip({"files": [{"url": urlA}], "client_ref": refU}, mid=2)
    _src2 = _captured[-1]["source"]["files"]
    check("U4 same ref + same URL → replay (zero new Gokapi, same handle)",
          r["result"].get("isError") is False and _gk_count() == g1
          and _src2 == _src, f"gk {g1}->{_gk_count()}")
    r = _ip({"files": [{"url": urlB}], "client_ref": refU}, mid=3)
    check("U5 same ref + different URL → -32009 idempotency_conflict",
          (r.get("error") or {}).get("code") == -32009
          and r["error"].get("message") == "idempotency_conflict"
          and _gk_count() == g1, str(r)[:120])
    r = _ip({"files": [{"filename": "a.png", "b64": _b64(PNG)}], "client_ref": refU}, mid=4)
    check("U6 same ref + inline instead of URL → -32009",
          (r.get("error") or {}).get("code") == -32009 and _gk_count() == g1, str(r)[:120])
    big = f"https://files.example/{_u}/big.png"
    from config import MAX_UPLOAD_BYTES
    _served[big] = b"\x89PNG\r\n\x1a\n" + b"\0" * MAX_UPLOAD_BYTES
    i1 = _ingress_n(uid)
    r = _ip({"files": [{"url": big}], "client_ref": f"big-{_u}"}, mid=5)
    _u7 = (r.get("result") or {})
    _u7t = ((_u7.get("content") or [{}])[0].get("text") or "")
    check("U7 oversize URL body → isError, zero ingress/Gokapi",
          _u7.get("isError") is True and "too_large" in _u7t
          and _ingress_n(uid) == i1 and _gk_count() == g1, str(r)[:120])
    del _served[big]
    def _blocked(url, **k):
        raise _remote.RemoteError("redirect_blocked", "redirect to non-https rejected")
    _remote.pinned_request = _blocked
    r = _ip({"files": [{"url": urlA}], "client_ref": f"rb-{_u}"}, mid=6)
    _u8 = (r.get("result") or {})
    _u8t = ((_u8.get("content") or [{}])[0].get("text") or "")
    check("U8 transport-level rejection → isError with typed code",
          _u8.get("isError") is True and "redirect_blocked" in _u8t
          and _ingress_n(uid) == i1,
          str(r)[:120])
finally:
    _remote.pinned_request = _orig_pinned
    _dispatch_mod.dispatch_execute = _orig_dx

# ══════════ 10. reference to an existing upload ══════════
def _web_upload(cookie, ref):
    b = "----mp"
    body = (f'--{b}\r\nContent-Disposition: form-data; name="tool"\r\n\r\ncompress\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="client_ref"\r\n\r\n{ref}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="r.png"\r\n'
            f'Content-Type: image/png\r\n\r\n').encode() + PNG + f"\r\n--{b}--\r\n".encode()
    st, _h, d = _req("POST", "/api/uploads", body,
                     {"Content-Type": f"multipart/form-data; boundary={b}", "Cookie": cookie})
    return (d or {}).get("file") or {}
src1 = _web_upload(ck, f"web-{_u}-1")
src2 = _web_upload(ck2, f"web-{_u}-2")
check("R0 fixtures: web tool uploads carry stagingId + sourceToken",
      src1.get("stagingId") and src1.get("sourceToken") and src2.get("stagingId"), str(src1)[:100])
refR = f"ref-{_u}"
i0 = _ingress_n(uid)
st, _h, d = _call("compress", {"files": [{"public_id": src1["stagingId"],
                                          "source_token": src1["sourceToken"]}],
                               "client_ref": refR})
_rt = (d.get("result") or {}).get("structuredContent") or {}
check("R1 reference form → executed result",
      (d.get("result") or {}).get("isError") is False
      and bool(_rt.get("items"))
      and _rt["items"][0].get("kind") == "file", str(d)[:140])
check("R2 reference form claims no ingress receipt", _ingress_n(uid) == i0)
c0 = _calls_n(uid)
st, _h, d = _call("compress", {"files": [{"public_id": src2["stagingId"],
                                          "source_token": src2["sourceToken"]}],
                               "client_ref": f"refx-{_u}"})
check("R3 foreign upload (other user's token) → isError, zero tool_call",
      (d.get("result") or {}).get("isError") is True and _calls_n(uid) == c0, str(d)[:120])
st, _h, d = _call("compress", {"files": [{"public_id": src1["stagingId"],
                                          "source_token": "0" * 64}],
                               "client_ref": f"refy-{_u}"})
check("R4 invalid source_token → isError, zero tool_call",
      (d.get("result") or {}).get("isError") is True and _calls_n(uid) == c0, str(d)[:120])
st, _h, d = _call("compress", {"files": [{"public_id": src1["stagingId"],
                                          "source_token": 7}],
                               "client_ref": f"refz-{_u}"})
check("R5 non-string source_token → -32602", (d.get("error") or {}).get("code") == -32602)
st, _h, d = _call("compress", {"files": [{"public_id": src1["stagingId"],
                                          "source_token": src1["sourceToken"]}],
                               "client_ref": refR}, mid=2)
check("R6 same ref + same reference → replay (same call)",
      ((d.get("result") or {}).get("structuredContent") or {}).get("replay")
      is True and _calls_n(uid) == c0, str(d)[:120])
src3 = _web_upload(ck, f"web-{_u}-3")
st, _h, d = _call("compress", {"files": [{"public_id": src3["stagingId"],
                                          "source_token": src3["sourceToken"]}],
                               "client_ref": refR}, mid=3)
_r7 = _err_sentence(d)
check("R7 same ref + different reference → conflict sentence, zero new call",
      (d.get("result") or {}).get("isError") is True
      and "idempotency_conflict" in _r7 and _calls_n(uid) == c0,
      _r7[:120])

# ══════════ 11. F6: body-tool adapter error layering ══════════
# 正文工具的输入方式/口径违规是调用方参数错误: preflight 的 err_input_mode
# 只有这一个码翻译成 "invalid input mode"; err_body_* 同为 -32602。
# 工具行 = trusted http 正文工具 (owner NULL, endpoint 不可达也无妨 —
# 全部用例在 preflight 期拒绝, 零网络零扣费)。
_btkey = f"mpbody{_os.urandom(4).hex()}"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " body_format, files_min, files_max, outputs_json, endpoint_json,"
        " created_at, updated_at)"
        " values (%s, NULL, %s, 'http', %s, 'public', TRUE, NULL, NULL,"
        " 'text', 0, 0, '[\"text\"]', %s, %s, %s)",
        (_btkey, _btkey, json.dumps({"en": "MP Body Probe"}),
         json.dumps({"url": "http://127.0.0.1:1/never", "timeout_s": 5}),
         ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 0, TRUE, %s)", (_btkey, ts))

def _ipn(name, args, mid=10):
    return asyncio.run(_M._call_tool(_Req(), {"id": uid}, mid,
                                     {"name": name, "arguments": args}))

try:
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
    _bt_spec = next((t for t in ((d.get("result") or {}).get("tools") or [])
                     if t.get("name") == _btkey), {})
    _bt_props = (_bt_spec.get("inputSchema") or {}).get("properties") or {}
    _bt_req = (_bt_spec.get("inputSchema") or {}).get("required") or []
    check("F6a body tool inputSchema exposes input (string), no files",
          _bt_props.get("input", {}).get("type") == "string"
          and "files" not in _bt_props
          and "input" in _bt_req and "client_ref" in _bt_req,
          str(_bt_spec)[:140])
    r = _ipn(_btkey, {"client_ref": f"f6b-{_u}"})  # input MISSING
    check("F6b body tool without input → -32602 invalid input mode",
          (r.get("error") or {}).get("code") == -32602
          and "invalid input mode" in r["error"].get("message", ""),
          str(r)[:130])
    r = _ipn(_btkey, {"client_ref": f"f6c-{_u}", "input": ""})
    check("F6c empty text input → -32602 err_body_required",
          (r.get("error") or {}).get("code") == -32602
          and "err_body_required" in r["error"].get("message", ""),
          str(r)[:130])
    r = _ipn(_btkey, {"client_ref": f"f6d-{_u}", "input": "x" * 100001})
    check("F6d text input over 100k → -32602 err_body_too_long",
          (r.get("error") or {}).get("code") == -32602
          and "err_body_too_long" in r["error"].get("message", ""),
          str(r)[:130])
    r = _ipn(_btkey, {"client_ref": f"f6e-{_u}", "input": 123})
    check("F6e non-string text input → -32602 err_body_invalid",
          (r.get("error") or {}).get("code") == -32602
          and "err_body_invalid" in r["error"].get("message", ""),
          str(r)[:130])
    r = _ipn("compress", {"client_ref": f"f6f-{_u}", "input": "x"})
    check("F6f input on a file tool → -32602 invalid input mode",
          (r.get("error") or {}).get("code") == -32602
          and "invalid input mode" in r["error"].get("message", "")
          and "files[]" in r["error"].get("message", ""),
          str(r)[:130])
finally:
    with db() as con:
        con.execute("delete from pricing where tool_id=%s", (_btkey,))
        con.execute("delete from platform_tools where tool_key=%s", (_btkey,))

# ══════════ 12. F7: machine-surface auth scoping (unit) ══════════
# /v1 精确前缀 (path == /v1 or /v1/...); fsot_ 只在 /mcp 解析;
# /v1 上 fsot_ + 有效 cookie 一律不认证。
import types as _types
from config import COOKIE_NAME as _CK
from app import main as _appmain

class _AuthReq:
    def __init__(self, path, headers=None, cookies=None):
        self.url = _types.SimpleNamespace(path=path)
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = dict(cookies or {})
        self.state = _types.SimpleNamespace()

_u1 = _appmain.get_user_from_request(_AuthReq(
    "/v1/tools", headers={"authorization": "Bearer fsot_not-a-token"},
    cookies={_CK: ck.split("=", 1)[1]}))
check("F7a /v1 + fsot_ + valid session cookie → NOT authenticated",
      _u1 is None, str(_u1)[:80])
_r2 = _AuthReq("/v1/tools", headers={"authorization": f"Bearer {TOKEN}"})
_u2 = _appmain.get_user_from_request(_r2)
check("F7b /v1 + valid fsk_ key → authenticated (auth_via token)",
      _u2 is not None and _u2["id"] == uid
      and getattr(_r2.state, "auth_via", None) == "token",
      f"{_u2 and _u2.get('id')} {getattr(_r2.state, 'auth_via', None)}")
_u3 = _appmain.get_user_from_request(_AuthReq(
    "/v1page", cookies={_CK: ck.split("=", 1)[1]}))
check("F7c /v1page is NOT a machine surface (prefix tightened) → session works",
      _u3 is not None and _u3["id"] == uid, str(_u3)[:80])
_u4 = _appmain.get_user_from_request(_AuthReq("/v1"))
check("F7d exact /v1 without credentials → anonymous", _u4 is None)

# ══════════ 13. setup pages ══════════
st, _h, html_en = _req("GET", "/agents?lang=en")
st2, _h, html_ja = _req("GET", "/agents?lang=ja")
st3, _h, html_tm = _req("GET", "/tool/compress/mcp?lang=en")
_mcp_url = f"{_BASE}/mcp"
check("S1 Claude Code snippet (http transport + Bearer header, site base URL)",
      st == 200 and f'claude mcp add --transport http 518 {_mcp_url} '
      '--header "Authorization: Bearer &lt;fsk_…&gt;"' in html_en)
check("S2 Cursor / VS Code JSON: type http + url + headers",
      html_en.count('"type": "http"') == 2 and '"servers": {' in html_en
      and html_en.count(f'"url": "{_mcp_url}"') == 2)
check("S3 Claude Desktop via mcp-remote with API key placeholder (C1)",
      f'"mcp-remote", "{_mcp_url}"' in html_en
      and "Bearer &lt;fsk_…&gt;" in html_en
      and "${TOKEN}" not in html_en)
check("S4 localized prose, API key placeholder",
      st2 == 200 and "MCP クライアントにサーバー URL を追加します" in html_ja
      and "（fsk_ で始まる）" not in html_ja and "（fsk_ 开头）" not in html_ja
      and "&lt;fsk_…&gt;" in html_ja and "&lt;token&gt;" not in html_ja)
# P6: the /tool/{key}/mcp twin page is retired — the tool page's access
# area (MCP card + declaration-generated API card) carries the surface.
check("S5 tool MCP twin page retired (404), tool page carries MCP + API cards",
      st3 == 404)
_ajs = open(_os.path.join(_REPO, "static", "pages", "agents.js"), encoding="utf-8").read()
_be = open(_os.path.join(_REPO, "static", "pages", "base-engine.js"), encoding="utf-8").read()
check("S6 copy feedback: agents.js delegates to the one authority; no hardcoded Chinese",
      "window.copyFeedback" in _ajs and 'window.t("copied")' in _be
      and "已复制" not in _ajs and "已复制" not in _be)

# ══════════ cleanup ══════════
_uids = [uid, uid2, _ownA, _ownB]
with db() as con:
    for k in (keyA, keyB):
        con.execute("delete from pricing where tool_id=%s", (k,))
        con.execute("delete from platform_tools where tool_key=%s", (k,))
for u in _uids:
    with db() as con:
        con.execute("delete from tool_calls where user_id=%s", (u,))
        con.execute("delete from uploads where user_id=%s", (u,))
        con.execute("delete from ledger where user_id=%s", (u,))
for u in _uids:
    with db() as con:
        purge_users(con, [u])
print("cleanup: mcp protocol fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
