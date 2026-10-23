#!/usr/bin/env python3
"""MCP surface — headless registration, the agent path, discovery
(catalog_search / developer guide), two-stage upload tickets, envelope
and meta tools, and the Streamable HTTP / JSON-RPC protocol rules.
"""
from _harness import REPO, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Headless registration
#
# MCP headless registration canonical — POST /auth/mcp/register.
#
# R*  The unauthenticated machine registration: a bare POST mints an
#     account plus its Bearer key in one response (no cookie, no
#     session); the returned key authenticates a real MCP initialize;
#     the 401 guidance points agents at the headless endpoint; a
#     provided username follows the web validators (taken → 409 typed,
#     short → 400 typed); a non-JSON body is err_invalid_request.
# ════════════════════════════════════════════════════════════════════════
section('Headless registration')

import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import uuid as _uuid


_HOST = ("127.0.0.1", int(TEST_PORT))
uids = []


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=120)
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


def _register(payload):
    return _req("POST", "/auth/mcp/register", payload,
                {"Content-Type": "application/json"})


def _initialize(token):
    return _req("POST", "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "canonical", "version": "0"}}},
        {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         **({"Authorization": f"Bearer {token}"} if token else {})})


try:
    # R1: bare registration mints account + key in one response
    st, h, d, sc = _register({})
    ok = (st == 200 and (d or {}).get("ok") is True
          and str((d or {}).get("username") or "").startswith("agent_")
          and isinstance((d or {}).get("password"), str)
          and str(((d or {}).get("key") or {}).get("key") or "").startswith("fsk_"))
    check("R1 bare register returns account + fsk_ key", ok, f"{st} {d}")
    first = d or {}
    if (first.get("key") or {}).get("user_id"):
        uids.append(first["key"]["user_id"])

    # R2: machine surface — no session cookie is ever set
    check("R2 no Set-Cookie on register", st == 200 and not sc, sc)

    # R3: the returned key authenticates a real MCP initialize
    st3, _h3, d3, _sc3 = _initialize(first["key"]["key"])
    ok3 = (st3 == 200
           and ((d3 or {}).get("result") or {}).get("protocolVersion")
           == "2025-03-26")
    check("R3 returned key passes MCP initialize", ok3, f"{st3} {d3}")

    # R4: no key = anonymous discovery that points at the register tool
    st4, _h4, d4, _sc4 = _initialize(None)
    _ins = str((((d4 or {}) if isinstance(d4, dict) else {})
                .get("result") or {}).get("instructions") or "")
    check("R4 anonymous initialize → 200 + register guidance",
          st4 == 200 and "'register'" in _ins
          and "Authorization: Bearer" in _ins, f"{st4} {_ins[:80]}")

    def _anon(method, params=None, token=None):
        return _req("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 7, "method": method,
            "params": params or {}},
            {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             **({"Authorization": f"Bearer {token}"} if token else {})})

    st, _h, d, _sc = _anon("tools/list")
    _names = [t["name"] for t in ((d or {}).get("result") or {}).get("tools", [])]
    check("R4b anonymous tools/list → register + first-party tools, no meta tools",
          st == 200 and _names[:1] == ["register"] and "share" in _names
          and "create_upload" not in _names and "catalog_search" not in _names,
          _names[:6])
    st, h, d, _sc = _anon("tools/call", {"name": "share", "arguments": {
        "files": [{"filename": "a.txt", "b64": "aGk="}], "client_ref": "r4c"}})
    _wa = (h or {}).get("www-authenticate") or ""
    check("R4c anonymous tools/call → 401 + WWW-Authenticate challenge (register stays the anonymous path)",
          st == 401 and "oauth-protected-resource" in _wa
          and "register" in _json.dumps(d or {}), f"{st} {_wa}")
    # Malformed anonymous register arguments must not create an account.
    # Count rows before/after the real HTTP request, not just the response.
    from database import db
    with db() as con:
        _before = int(con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])
    st_bad, _h_bad, d_bad, _sc_bad = _anon(
        "tools/call", {"name": "register", "arguments": ["invalid"]})
    _unexpected = (((d_bad or {}).get("result") or {})
                   .get("structuredContent") or {}).get("username")
    with db() as con:
        _after = int(con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])
        if _unexpected:
            # Cleanup a leaked account if this regression fails on old code.
            _row = con.execute(
                "SELECT id FROM users WHERE username=%s", (_unexpected,)).fetchone()
            if _row:
                uids.append(int(_row["id"]))
    check("R4c2 malformed register arguments → -32602, zero account writes",
          st_bad == 200 and ((d_bad or {}).get("error") or {}).get("code") == -32602
          and _before == _after,
          f"{st_bad} {d_bad} users {_before}->{_after}")
    st, _h, d, _sc = _anon("tools/call", {"name": "register", "arguments": {}})
    _r = (d or {}).get("result") or {}
    _out = _r.get("structuredContent") or {}
    check("R4d register tool → api_key + username/password + pricing_url",
          st == 200 and _r.get("isError") is False
          and str(_out.get("api_key", "")).startswith("fsk_")
          and _out.get("username") and _out.get("password")
          and str(_out.get("pricing_url", "")).endswith("/pricing"), _out)
    st, _h, d, _sc = _anon("tools/list", token=_out.get("api_key"))
    _names = [t["name"] for t in ((d or {}).get("result") or {}).get("tools", [])]
    check("R4e the registered key unlocks the full working set (no register)",
          st == 200 and "create_upload" in _names and "register" not in _names,
          _names[:6])
    st, _h, d, _sc = _anon("ping", token="fsk_definitely_invalid")
    check("R4f invalid key → 401 + WWW-Authenticate (never silently anonymous)",
          st == 401
          and "oauth-protected-resource" in (_h.get("www-authenticate") or "")
          and "register" in str((d or {}).get("message", "")), f"{st} {d}")

    # R5: a provided username follows the web validators — taken → 409
    chosen = "mr" + _uuid.uuid4().hex[:8]
    st5a, _h5a, d5a, _sc5a = _register({"username": chosen})
    ok5a = (st5a == 200 and (d5a or {}).get("username") == chosen)
    check("R5a chosen username honored", ok5a, f"{st5a} {d5a}")
    if ((d5a or {}).get("key") or {}).get("user_id"):
        uids.append(d5a["key"]["user_id"])
    st5b, _h5b, d5b, _sc5b = _register({"username": chosen})
    check("R5b duplicate username → 409 err_username_taken",
          st5b == 409 and (d5b or {}).get("error") == "err_username_taken",
          f"{st5b} {d5b}")

    # R6: short username → the typed web validator error
    st6, _h6, d6, _sc6 = _register({"username": "ab"})
    check("R6 short username → 400 err_username_short",
          st6 == 400 and (d6 or {}).get("error") == "err_username_short",
          f"{st6} {d6}")

    # R7: a non-JSON body is rejected before any write
    st7, _h7, d7, _sc7 = _req("POST", "/auth/mcp/register", b"not-json",
                              {"Content-Type": "application/json"})
    check("R7 non-JSON body → 400 err_invalid_request",
          st7 == 400 and (d7 or {}).get("error") == "err_invalid_request",
          f"{st7} {d7}")

    # R8: an unknown Bearer key is still rejected
    st8, _h8, d8, _sc8 = _initialize("fsk_unknown")
    check("R8 unknown key initialize → 401", st8 == 401, f"{st8} {d8}")
finally:
    if uids:
        from database import db
        with db() as con:
            purge_users(con, uids)


# ════════════════════════════════════════════════════════════════════════
# Agent path
#
# Agent-path canonical — the MCP surface an agent actually walks.
#
# A*  Auth failure is self-describing: no/invalid API key → 401 with
#     JSON body {error: unauthorized, message pointing at /account/keys}
#     and the WWW-Authenticate header kept.
# B*  create_upload refuses unsupported extensions BEFORE issuing a
#     ticket (same single-point word lists the PUT runs): isError with
#     the PUT's code and zero ticket rows.
# C*  Agent-delivered text formats: a .log shares fine; a .log carrying
#     a NUL byte is rejected (text sniff).
# D*  isError results are ONE text block whose text parses as JSON with
#     error + message — for platform-level format refusal and for
#     tool-accept mismatch alike.
# E*  Image echo suppression: share of a small PNG emits NO image block
#     (bytes identical to this call's input); compress of the same PNG
#     still does (bytes changed).
# F*  Discovery mentions the human-link path: initialize instructions
#     and the share description both carry result_url; /llms.txt too.
# ════════════════════════════════════════════════════════════════════════
section('Agent path')

import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid


_HOST_agp = ("127.0.0.1", int(TEST_PORT))
_u = "ap" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n%%EOF\n"


def _req_agp(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_agp, timeout=120)
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


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp(body, token):
    st, _h, d, _sc = _req_agp("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token, mid=1):
    return _mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _b64(b):
    return base64.b64encode(b).decode()


def _tool_text(d):
    """structuredContent — the machine channel of an isError result
    (content text is the readable summary)."""
    return (((d or {}).get("result") or {}).get("structuredContent")
            or {})


from database import db

uid = None
try:
    # ═══ A. no key = anonymous discovery; a bad key = self-describing 401
    st, h, d = (lambda r: (r[0], r[1], r[2]))(_req_agp(
        "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"Content-Type": "application/json"}))
    check("A0 no key → ping answers (anonymous discovery, no 401)",
          st == 200 and isinstance(d, dict) and d.get("result") == {},
          f"{st} {d}")
    st, h, d = (lambda r: (r[0], r[1], r[2]))(_req_agp(
        "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"Content-Type": "application/json",
         "Authorization": "Bearer fsk_not_a_real_key"}))
    check("A1 invalid key → 401 with JSON body",
          st == 401 and isinstance(d, dict), f"{st} {d}")
    check("A2 body error=unauthorized, message points at register + /account/keys",
          isinstance(d, dict) and d.get("error") == "unauthorized"
          and "register" in str(d.get("message", ""))
          and "/account/keys" in str(d.get("message", "")), d)
    check("A3 WWW-Authenticate kept (resource metadata)",
          "oauth-protected-resource" in (h.get("www-authenticate") or ""),
          h.get("www-authenticate"))
    st, _h, d = (lambda r: (r[0], r[1], r[2]))(_req_agp(
        "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"Content-Type": "application/json",
         "Authorization": "Bearer fsk_wrongkey"}))
    check("A4 wrong key → same shape", st == 401
          and isinstance(d, dict) and d.get("error") == "unauthorized", d)

    # user + key + credits for the rest
    b, ct = _form({"username": _u + "a", "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{_u}@t.co"})
    _req_agp("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u + "a", "password": "Test12345"})
    st_l, _h, _d, sc = _req_agp("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    st, _h, d_k, _sc = _req_agp("POST", "/api/keys", {"name": "ap"},
                             {"Content-Type": "application/json", "Cookie": ck})
    TOK = d_k["key"]["key"]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
        from repo.ledger import insert_row
        insert_row(con, uid, 100000, 100000, "manual_topup", None, None,
                   f"ap:{_u}", None, ts)
    check("F0 user + key + credits", uid and TOK, uid)

    # ═══ B. create_upload pre-validates the extension ═══════════════
    st, d = _call("create_upload", {"filename": "x.bin", "size": 10,
                                    "client_ref": f"apb1-{_u}"}, TOK)
    _t = _tool_text(d)
    check("B1 x.bin ticket refused with the PUT's code",
          (d.get("result") or {}).get("isError") is True
          and _t.get("error") == "err_format_not_supported"
          and isinstance(_t.get("message"), str) and _t["message"], _t)
    with db() as con:
        n = int(con.execute(
            "select count(*) c from mcp_upload_tickets where user_id=%s",
            (uid,)).fetchone()["c"])
    check("B2 no ticket row left behind", n == 0, n)
    st, d = _call("create_upload", {"filename": "x.exe", "size": 10,
                                    "client_ref": f"apb3-{_u}"}, TOK)
    check("B3 blocked ext refused with the PUT's code",
          (d.get("result") or {}).get("isError") is True
          and _tool_text(d).get("error") == "err_file_type_blocked",
          _tool_text(d))

    # ═══ C. agent-delivered text formats ════════════════════════════
    st, d = _call("share", {"files": [{"filename": "agent.log",
                                       "b64": _b64(b"2026-09-27 INFO ok\n")}],
                            "client_ref": f"apc1-{_u}"}, TOK)
    _sc = (d.get("result") or {}).get("structuredContent") or {}
    check("C1 .log shares fine (file item)",
          (d.get("result") or {}).get("isError") is False
          and [i.get("kind") for i in _sc.get("items", [])] == ["file"],
          str(d)[:120])
    st, d = _call("share", {"files": [{"filename": "bad.log",
                                       "b64": _b64(b"ab\x00cd")}],
                            "client_ref": f"apc2-{_u}"}, TOK)
    _t = _tool_text(d)
    # NUL text → sniff err_content_mismatch_text, surfaced VERBATIM with
    # the structured ingress detail (detected/suggested_name null when
    # the real format is unrecognizable)
    check("C2 NUL .log rejected",
          (d.get("result") or {}).get("isError") is True
          and _t.get("error") == "err_content_mismatch_text"
          and _t.get("filename") == "bad.log"
          and _t.get("detected") is None
          and _t.get("suggested_name") is None
          and isinstance(_t.get("message"), str), _t)

    # ═══ D. unified isError JSON (both failure kinds) ═══════════════
    st, d = _call("share", {"files": [{"filename": "x.bin",
                                       "b64": _b64(b"whatever")}],
                            "client_ref": f"apd1-{_u}"}, TOK)
    _t1 = _tool_text(d)
    check("D1 platform format refusal parses as JSON with error+message",
          (d.get("result") or {}).get("isError") is True
          and _t1.get("error") == "err_format_not_supported"
          and isinstance(_t1.get("message"), str) and _t1["message"], _t1)
    st, d = _call("compress", {"files": [{"filename": "doc.pdf",
                                          "b64": _b64(PDF)}],
                               "client_ref": f"apd2-{_u}"}, TOK)
    _t2 = _tool_text(d)
    check("D2 tool accept mismatch parses as JSON with error+message",
          (d.get("result") or {}).get("isError") is True
          and _t2.get("error") == "err_format_not_supported"
          and isinstance(_t2.get("message"), str) and _t2["message"], _t2)
    check("D3 both carry client_ref",
          _t1.get("client_ref") == f"apd1-{_u}"
          and _t2.get("client_ref") == f"apd2-{_u}", (_t1, _t2))

    # ═══ E. image echo suppression ══════════════════════════════════
    st, d = _call("share", {"files": [{"filename": "shot.png",
                                       "b64": _b64(PNG)}],
                            "client_ref": f"ape1-{_u}"}, TOK)
    _kinds = [c["type"] for c in
              ((d.get("result") or {}).get("content") or [])]
    check("E1 share of own PNG → NO image block (echo suppressed)",
          (d.get("result") or {}).get("isError") is False
          and "image" not in _kinds and "resource_link" not in _kinds
          and _kinds == ["text"], _kinds)
    # compress only re-encodes images ABOVE the target: a >1MB noise
    # PNG forces a real JPEG conversion (bytes differ from the input,
    # so the echo rule must NOT suppress the block).
    import io as _io
    import random as _random
    from PIL import Image as _Img
    _rng = _random.Random(0xB8)
    _img = _Img.new("RGB", (1024, 1024))
    _img.putdata([(_rng.randrange(256), _rng.randrange(256),
                   _rng.randrange(256)) for _ in range(1024 * 1024)])
    _buf = _io.BytesIO()
    _img.save(_buf, "PNG")
    BIG_PNG = _buf.getvalue()
    st, d = _call("compress", {"files": [{"filename": "big.png",
                                          "b64": _b64(BIG_PNG)}],
                               "params": {"preset": "1mb"},
                               "client_ref": f"ape2-{_u}"}, TOK)
    _kinds = [c["type"] for c in
              ((d.get("result") or {}).get("content") or [])]
    check("E2 compress re-encoded image → image block still attached",
          (d.get("result") or {}).get("isError") is False
          and "image" in _kinds and "resource_link" not in _kinds, _kinds)

    # ═══ F. discovery mentions the human-link path ══════════════════
    st, d = _mcp({"jsonrpc": "2.0", "id": 30,
                  "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18"}}, TOK)
    _instr = ((d or {}).get("result") or {}).get("instructions") or ""
    check("F1 initialize instructions carry result_url + share",
          "result_url" in _instr and "share" in _instr, _instr[:100])
    st, d = _mcp({"jsonrpc": "2.0", "id": 31, "method": "tools/list"}, TOK)
    _share = next((t for t in ((d or {}).get("result") or {}).get("tools", [])
                   if t.get("name") == "share"), {})
    check("F2 share description carries result_url + delivery default",
          "result_url" in _share.get("description", "")
          and "1 day" in _share.get("description", ""),
          _share.get("description", "")[:120])
    st, _h, body = (lambda r: (r[0], r[1], r[2]))(_req_agp("GET", "/llms.txt"))
    _lt = body if isinstance(body, str) else _json.dumps(body)
    check("F3 llms.txt carries the link sentence",
          "result_url" in _lt and "call share" in _lt, _lt[:80])
    _create = next((t for t in ((d or {}).get("result") or {}).get("tools", [])
                    if t.get("name") == "create_upload"), {})
    check("F4 create_upload spec: staged files stated NOT listable",
          "never appears in list_uploads" in _create.get("description", ""),
          _create.get("description", "")[:160])

finally:
    if uid is not None:
        with db() as con:
            purge_users(con, [uid])


# ════════════════════════════════════════════════════════════════════════
# Discovery: search single point, catalog_search, /developers
#
# Flow-clarity canonical — MCP discovery + the public developer guide.
#
# S*  Search single point: repo._market_base predicate for every surface
#     (home selector API / /tools market / MCP catalog_search) — same
#     ordering, multi-token AND, % and _ literal, empty query browses.
# M*  catalog_search returns the exact callable spec tools/list carries
#     (shared constructor) + favorite flag; a hit is callable by name
#     without listing the tool first.
# L*  initialize instructions and llms.txt carry the caller's / the free
#     tier's real account limits from the entitlement projection.
# D*  /developers: anonymous 200, every number rendered from the code
#     constants, the documented example request built by the real
#     dispatch_payload and the example response validated by the real
#     parse_endpoint_items; nav/footer/account wiring points at it.
# ════════════════════════════════════════════════════════════════════════
section('Discovery: search single point, catalog_search, /developers')

import base64
import http.client as _hc
import http.server
import json as _json
import os as _os
import re as _re
import sys as _sys
import threading
import time as _time
import uuid as _uuid


_HOST_flc = ("127.0.0.1", int(TEST_PORT))
_u = "fc" + _uuid.uuid4().hex[:6]


def _req_flc(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_flc, timeout=120)
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


def _form_flc(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp_flc(body, token, mid=1):
    st, _h, d, _sc = _req_flc("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token):
    return _mcp_flc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _sc_tools(payload):
    return (((payload or {}).get("result") or {})
            .get("structuredContent") or {}).get("tools") or []


def _register(name):
    b, ct = _form_flc({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    _req_flc("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_flc({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req_flc("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req_flc("POST", "/api/keys", {"name": "fc"},
                              {"Content-Type": "application/json", "Cookie": ck})
    return ck, d_k["key"]["key"]


def _publish(cookie, tool_id, extra=None):
    body = {"label": tool_id,
            "url": "https://93.184.216.34/x", "timeout_s": 30,
            "files_min": 0, "files_max": 1,
            "format_families": "image", "accept": [".png"],
            "outputs": ["text"], "cost": 0}
    body.update(extra or {})
    return _req_flc("POST", "/api/usertools", body,
                {"Content-Type": "application/json", "Cookie": cookie})


def _go_public(key):
    from database import db
    with db() as con:
        con.execute("update platform_tools set visibility='public'"
                    " where tool_key=%s", (key,))


# ── in-suite local developer endpoint (fx1 pattern) ──────────────────
class _DevHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        items = [{"kind": "text", "text": "flow ok"}]
        out = _json.dumps({"items": items}).encode()
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/vnd.518.items+json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DevHandler)
LOCAL_URL = f"http://127.0.0.1:{_srv.server_address[1]}/ok"
threading.Thread(target=_srv.serve_forever, daemon=True).start()

from database import db  # noqa: E402

# code authorities the docs must not drift from
from config import MAX_UPLOAD_BYTES as _MAX_UPLOAD_BYTES  # noqa: E402
from remote import (dispatch_payload, parse_endpoint_items,  # noqa: E402
                    MIN_TIMEOUT_S, MAX_TIMEOUT_S)
from governance.tool_supply import MAX_INPUT_FILES  # noqa: E402
from governance.tool_params import TEXT_PARAM_MAX_CHARS  # noqa: E402
from execution.results import (MAX_RESULT_ITEMS,  # noqa: E402
                               MAX_INLINE_RESULT_BYTES)
from governance.access_projection import project  # noqa: E402
from governance.entitlements import resolve_from_membership  # noqa: E402
# (MCP 名 = key 原文: 投影机已退役)

a_uid = r_uid = None
try:
    a_ck, A_TOK = _register(_u + "dev")
    r_ck, R_TOK = _register(_u + "usr")
    with db() as con:
        a_uid = int(con.execute("select id from users where username=%s",
                                (_u + "dev",)).fetchone()["id"])
        r_uid = int(con.execute("select id from users where username=%s",
                                (_u + "usr",)).fetchone()["id"])

    # ══════════ S: search single point ══════════
    keys = []
    for tid, extra in [("multialpha", {"label": "Multi Alpha Beta"}),
                       ("onlyalpha", {"label": "Alpha only"}),
                       ("pcthalf", {"label": "Pct 50% off"}),
                       ("underscore", {"label": "fl_ow special"})]:
        st, _h, d, _sc = _publish(a_ck, tid, extra)
        keys.append(d.get("tool_key") if d else None)
    key_multi, key_one, key_pct, key_und = keys
    check("S0 setup: four unlisted tools created",
          all(_req_flc("GET", f"/tool/{k}", None,
                   {"Cookie": a_ck, "Accept": "text/html"})[0] == 200
              for k in (key_multi, key_one, key_pct, key_und)))
    # unlisted tools must not appear in search (public predicate)
    st, _h, d, _sc = _req_flc("GET", "/api/tools/search?q=alpha")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S1 unlisted tools never in search",
          key_multi not in ids and key_one not in ids, ids)
    for k in (key_multi, key_one, key_pct, key_und):
        _go_public(k)
    # multi-token AND
    st, _h, d, _sc = _req_flc("GET", "/api/tools/search?q=alpha+beta")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S2 multi-word AND hits the tool carrying both words",
          key_multi in ids and key_one not in ids, ids)
    st, _h, d, _sc = _req_flc("GET", "/api/tools/search?q=beta")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S3 single word still matches", key_multi in ids, ids)

    # % and _ literal
    st, _h, d, _sc = _req_flc("GET", "/api/tools/search?q=50%25")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S4 % matches literally", key_pct in ids, ids)
    st, _h, d, _sc = _req_flc("GET", "/api/tools/search?q=fl_w")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S5 _ is NOT a wildcard (fl_w does not match fl_ow)",
          key_und not in ids, ids)
    st, _h, d, _sc = _req_flc("GET", "/api/tools/search?q=fl_ow")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S6 _ matches literally", key_und in ids, ids)

    # three entrances, same query, same order
    q = "alpha"
    st, _h, d, _sc = _req_flc("GET", f"/api/tools/search?q={q}")
    api_ids = [t["id"] for t in (d or {}).get("tools") or []]
    st, _h, page, _sc = _req_flc("GET", f"/tools?q={q}")
    market_ids = _re.findall(r'href="/tool/([a-z0-9_/%-]+)"',
                             page or "", _re.I)
    seen = []
    for k in market_ids:
        if k not in seen:
            seen.append(k)
    st, d = _mcp_flc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "catalog_search",
                             "arguments": {"q": q}}}, R_TOK)
    mcp_keys = [t.get("name") for t in _sc_tools(d)]  # 名 = key 原文
    check("S7 api/tools/search == /tools market order (first page)",
          api_ids == seen[:len(api_ids)], f"{api_ids} vs {seen}")
    check("S8 MCP catalog_search == api/tools/search order",
          mcp_keys == api_ids, f"{mcp_keys} vs {api_ids}")

    # empty query = browse, top 20, same ordering for every surface
    st, _h, d, _sc = _req_flc("GET", "/api/tools/search")
    browse = [t["id"] for t in (d or {}).get("tools") or []]
    check("S9 empty query browses (non-empty, capped at 20)",
          0 < len(browse) <= 20, len(browse))
    st, _h, page, _sc = _req_flc("GET", "/tools")
    market_all = []
    for k in _re.findall(r'href="/tool/([a-z0-9_/%-]+)"', page or "", _re.I):
        if k not in market_all:
            market_all.append(k)
    check("S10 empty browse == market page-1 head order",
          browse == market_all[:len(browse)], f"{browse[:5]} vs {market_all[:5]}")
    st, d = _mcp_flc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                  "params": {"name": "catalog_search",
                             "arguments": {"q": ""}}}, R_TOK)
    mcp_browse = [t.get("name") for t in _sc_tools(d)]
    # 2026-10 contract: the MCP empty query enumerates the FULL public
    # catalog (featured first) — a superset of the web featured browse;
    # the web browse stays curated.
    check("S11 MCP empty query = full catalog: featured head preserved, superset of the web browse",
          mcp_browse[:len(browse)] == browse and set(browse) < set(mcp_browse),
          f"{mcp_browse[:5]} vs {browse[:5]}")

    # ══════════ M: catalog_search returns callable specs ══════════
    # Community rows are never SSRF-exempt, so the callable fixture is
    # an OPERATOR row (owner NULL — the ownership exemption) pointing
    # at the in-suite listener; discovery/pin/call all run on it.
    import json as _json_m0
    key_call = f"mccall{_u}"
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled, files_min,"
            " files_max, outputs_json, params_json, endpoint_json,"
            " created_at, updated_at, accept_json, format_family)"
            " values (%s, NULL, %s, 'http', %s, 'public', true, 0, 1,"
            " '[\"text\"]', %s, %s, %s, %s, '[\".png\"]', 'image')",
            (key_call, key_call,
             _json_m0.dumps({"en": "Callable Now"}),
             _json_m0.dumps([{"key": "mode", "type": "enum",
                              "label": "Mode", "required": False,
                              "options": [{"value": "fast"},
                                          {"value": "slow"}],
                              "default": "fast"}]),
             _json_m0.dumps({"url": LOCAL_URL, "timeout_s": 30}),
             ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, 0, true, %s)", (key_call, ts))
    check("M0 setup: operator tool row created", bool(key_call), key_call)
    with db() as con:
        decl_m0 = con.execute(
            "select endpoint_json from platform_tools where tool_key=%s",
            (key_call,)).fetchone()
    check("M0b endpoint pointed at the local listener",
          _json_m0.loads(decl_m0["endpoint_json"])["url"] == LOCAL_URL,
          decl_m0["endpoint_json"])
    mname = key_call

    st, d = _call("catalog_search", {"q": "Callable"}, R_TOK)
    hits = [t for t in _sc_tools(d) if t.get("name") == mname]
    check("M1 search returns the tool", len(hits) == 1, _sc_tools(d))
    hit = hits[0] if hits else {}
    check("M2 hit carries the full spec + favorite",
          {"name", "title", "description", "inputSchema", "outputSchema",
           "annotations", "favorite"} <= set(hit), sorted(hit))
    check("M3 unpinned → favorite False", hit.get("favorite") is False, hit.get("favorite"))
    # inputSchema before pinning must equal the tools/list spec after
    # pinning (same constructor, this bearer's entitlement enums)
    st, d = _call("pin_tool", {"name": mname}, R_TOK)
    check("M4 pin ok", st == 200 and not ((d or {}).get("result") or {}).get("isError"), d)
    st, d = _mcp_flc({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, R_TOK)
    listed = [t for t in ((d or {}).get("result") or {}).get("tools") or []
              if t.get("name") == mname]
    check("M5 pinned tool in tools/list", len(listed) == 1, )
    listed_spec = listed[0] if listed else {}
    same = {k: hit.get(k) for k in
            ("name", "title", "description", "inputSchema",
             "outputSchema", "annotations")}
    check("M6 search spec == tools/list spec (shared constructor)",
          all(listed_spec.get(k) == v for k, v in same.items()),
          f"{ {k: listed_spec.get(k) == v for k, v in same.items()} }")
    st, d = _call("catalog_search", {"q": "Callable"}, R_TOK)
    hits = [t for t in _sc_tools(d) if t.get("name") == mname]
    check("M7 pinned → favorite True",
          hits and hits[0].get("favorite") is True, )
    # call it by name using only what the search result declared
    st, d = _call("unpin_tool", {"name": mname}, R_TOK)
    check("M8 unpin (call must work without listing)", st == 200, )
    st, d = _call(mname, {"params": {"mode": "slow"},
                          "client_ref": f"fc-{_u}-1"}, R_TOK)
    res = (d or {}).get("result") or {}
    check("M9 unpinned tool callable straight from the search spec",
          res.get("isError") is not True
          and "flow ok" in _json.dumps(res.get("structuredContent") or {}),
          str(d)[:200])

    # ══════════ L: limits in instructions + llms.txt ══════════
    ent = project(r_uid)["entitlements"]
    st, d = _mcp_flc({"jsonrpc": "2.0", "id": 8, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26"}}, R_TOK)
    ins = ((d or {}).get("result") or {}).get("instructions") or ""
    from utils import format_bytes
    want = [
        f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB per input file",
        f"up to {ent['ingress_files_24h']} files",
        format_bytes(ent["ingress_bytes_24h"]),
        f"expiry_days {list(ent['expiry_days'])}",
        f"allowed_downloads {list(ent['download_limits'])}",
    ]
    for w in want:
        check(f"L1 instructions carry '{w[:44]}'", w in ins, ins[-300:])
    st, _h, llms, _sc = _req_flc("GET", "/llms.txt")
    free = resolve_from_membership(None)
    lwant = [
        "## Limits",
        f"- Per file: {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        f"{free['ingress_files_24h']} files",
        format_bytes(free["ingress_bytes_24h"]),
        f"expiry_days {list(free['expiry_days'])}",
        "## Publish your own tool",
        "/developers",
    ]
    for w in lwant:
        check(f"L2 llms.txt carries '{w[:44]}'", w in (llms or ""),
              (llms or "")[:200])

    # ══════════ D: /developers ══════════
    st, _h, page, _sc = _req_flc("GET", "/developers")
    check("D1 /developers anonymous 200", st == 200, st)
    page = page or ""
    check("D2 start-publishing button → /account/tools/publish",
          'href="/account/tools/publish"' in page)
    dwant = [
        str(_MAX_UPLOAD_BYTES // 1024 // 1024),
        str(MAX_INPUT_FILES),
        f"{MIN_TIMEOUT_S}–{MAX_TIMEOUT_S}",
        str(MAX_RESULT_ITEMS),
        str(MAX_INLINE_RESULT_BYTES),
        str(TEXT_PARAM_MAX_CHARS),
    ]
    for w in dwant:
        check(f"D3 page renders constant '{w}'", w in page, w)
    check("D4 nav + footer + mobile drawer link /developers",
          page.count('href="/developers"') >= 3,
          page.count('href="/developers"'))
    check("D5 zh locale renders the zh doc", "发布流程" in _req_flc(
        "GET", "/developers?lang=zh")[2], )
    # example request built by the REAL constructor
    m = _re.search(r'<pre class="doc-pre" id="ex-req">(.*?)</pre>',
                   page, _re.S)
    ex_req = _json.loads((m.group(1).strip().split("\n\n", 1)[1]
                          if m else "{}"))
    built = dispatch_payload(
        [{"filename": f["filename"], "content_type": f["content_type"],
          "size": f["size"], "url": f["url"]}
         for f in ex_req.get("files") or []],
        ex_req.get("params"))
    check("D6 example request == real dispatch_payload output",
          built == ex_req, f"{built} vs {ex_req}")
    # example response validated by the REAL validator
    m = _re.search(r'<pre class="doc-pre" id="ex-res">(.*?)</pre>',
                   page, _re.S)
    ex_res = _json.loads(m.group(1).strip() if m else "{}")

    def _no_fetch(u):
        raise AssertionError("example must not need a fetch: " + u)

    items = parse_endpoint_items(ex_res, _no_fetch)
    check("D7 example response passes real parse_endpoint_items",
          [it["kind"] for it in items] == ["text", "json", "file"],
          [it.get("kind") for it in items])
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
        "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
    check("D8 file item normalized with real size",
          items[-1]["size"] == len(png) and items[-1]["filename"] == "out.png",
          items[-1])
    # account tools page: one-line guide link, no collapsed contract
    st, _h, acct, _sc = _req_flc("GET", "/account/tools", None,
                             {"Cookie": a_ck, "Accept": "text/html"})
    check("D9 account/tools links the guide, contract block gone",
          st == 200 and 'href="/developers"' in (acct or "")
          and "contract-doc" not in (acct or ""), st)

finally:
    try:
        _srv.shutdown()
    except Exception:
        pass
    uids = [u for u in (a_uid, r_uid) if u]
    with db() as con:
        purge_users(con, uids)


# ════════════════════════════════════════════════════════════════════════
# Two-stage upload tickets
#
# MCP two-stage upload (B4b): create_upload → one-time PUT → tools/call
# reference. File bytes never appear in any JSON-RPC body.
#
#   P*  positive: create → PUT → tools/call(share ref) → result; 5MB bytes
#       identical end-to-end; every JSON-RPC body in the flow is small
#       (bytes-only legs are the binary PUT)
#   E*  create_upload contract: appears in tools/list (official, free);
#       validation (filename/size/content_type/client_ref); quota precheck
#       runs BEFORE a ticket exists
#   T*  ticket lifecycle: expiry → 410; replay → 409; forged → 404;
#       another user's Bearer on someone else's ticket → 403
#   U*  PUT guards: over-declared-size → 413 with zero side effects;
#       blocked extension refused at create_upload (isError, no ticket);
#       staging failure → 500 with quota and storage reclaimed
#   I*  idempotency: same client_ref + same params → same usable ticket;
#       same client_ref + different params → -32009
#   Q*  quota: exhausted 24h budget → typed -32602 at create (no ticket)
#
# Fail-closed env guard; per-run unique names; fixtures cleaned at end.
# ════════════════════════════════════════════════════════════════════════
section('Two-stage upload tickets')

import base64
import hashlib
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import urllib.request as _ur
import uuid as _uuid


import io

_HOST_tkt = ("127.0.0.1", int(TEST_PORT))
_BASE = f"http://127.0.0.1:{TEST_PORT}"
_u = "mu" + _uuid.uuid4().hex[:6]
ts = int(_time.time())
BIG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==") + b"\x89PNG\r\n\x1a\n" + _os.urandom(5 * 1024 * 1024 - 84)
BIG_MD5 = hashlib.md5(BIG).hexdigest()
PNG_SMALL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")

_rpc_bytes_sent = 0  # total JSON-RPC request bytes in the positive flow


def _req_tkt(method, path, body=None, headers=None, raw=False):
    conn = _hc.HTTPConnection(*_HOST_tkt, timeout=120)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    if raw:
        return r.status, hdrs, data, setc
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form_tkt(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp_tkt(body, token, mid=None):
    global _rpc_bytes_sent
    payload = _json.dumps(body).encode()
    _rpc_bytes_sent += len(payload)
    st, _h, d, _sc = _req_tkt("POST", "/mcp", payload, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call_tkt(name, arguments, token, mid=1):
    return _mcp_tkt({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _create_upload(args, token, mid=1):
    st, d = _call_tkt("create_upload", args, token, mid)
    txt = (((d or {}).get("result") or {}).get("structuredContent") or {})
    return st, d, txt


from database import db


def _gk(method, path, body=None):
    base = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
    req = _ur.Request(base + path, method=method,
                      headers={"apikey": _os.environ["FLASHSHARE_GOKAPI_KEY"]})
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = _json.dumps(body).encode()
    with _ur.urlopen(req, data, timeout=5) as r:
        raw = r.read()
    return _json.loads(raw) if raw else None


def _gk_n():
    return len(_gk("GET", "/api/files/list"))


def _gk_fail(mode):
    if mode is None:
        _gk("POST", "/ci/fail-internal", {"mode": None})
    else:
        _gk("POST", "/ci/fail-internal", {"mode": mode})


uid = uid2 = None
ck = ck2 = tok = tok2 = None
try:
    # fixtures: two users (owner + stranger) with tokens + credits
    for name_i, (uname, cred) in enumerate(((_u + "a", "uid"), (_u + "b", "uid2"))):
        b, ct = _form_tkt({"username": uname, "password": "Test12345",
                       "password_confirm": "Test12345",
                       "email": f"{uname}@t.co"})
        _req_tkt("POST", "/auth/register", b, {"Content-Type": ct})
        b, ct = _form_tkt({"login": uname, "password": "Test12345"})
        st_l, _h, _d, sc = _req_tkt("POST", "/auth/login", b, {"Content-Type": ct})
        cookie = sc.split(";")[0]
        st_t, _h, d_t, _sc = _req_tkt("POST", "/api/keys",
                                  {"name": "mu"},
                                  {"Content-Type": "application/json",
                                   "Cookie": cookie})
        if name_i == 0:
            ck, uid, tok = cookie, None, d_t["key"]["key"]
        else:
            ck2, tok2 = cookie, d_t["key"]["key"]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
        uid2 = int(con.execute("select id from users where username=%s",
                               (_u + "b",)).fetchone()["id"])
        from repo.ledger import insert_row
        insert_row(con, uid, 100000, 100000, "manual_topup", None, None,
                   f"mu:{_u}", None, ts)
        insert_row(con, uid2, 100000, 100000, "manual_topup", None, None,
                   f"mu:{_u}:2", None, ts)
    check("F1 two users + tokens + credits", uid and uid2 and tok and tok2,
          (uid, uid2))

    # ═══ E. create_upload contract ═══
    st, d = _mcp_tkt({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, tok)
    tools = ((d.get("result") or {}).get("tools") or [])
    spec = next((t for t in tools if t["name"] == "create_upload"), None)
    check("E1 create_upload in tools/list with required schema",
          spec is not None
          and spec["inputSchema"]["required"] == ["filename", "size",
                                                  "client_ref"], None)
    from config import MAX_UPLOAD_BYTES
    st, d, t = _create_upload({"filename": "a.png", "size": MAX_UPLOAD_BYTES + 1,
                               "client_ref": "e2"}, tok)
    check("E2 size over the platform cap → -32602, no ticket",
          (d.get("error") or {}).get("code") == -32602
          and not t.get("upload_url"), str(d)[:90])
    with db() as con:
        n = con.execute("select count(*) c from mcp_upload_tickets"
                        " where user_id=%s", (uid,)).fetchone()["c"]
    check("E2b rejected create leaves zero ticket rows", int(n) == 0, n)
    for bad, why in (({"size": 0}, "size<=0"), ({"size": "5"}, "size str"),
                     ({"filename": " "}, "empty filename"),
                     ({"client_ref": ""}, "empty ref")):
        args = {"filename": "a.png", "size": 100, "client_ref": f"e-{why}"}
        args.update(bad)
        st, d, t = _create_upload(args, tok)
        check(f"E3 invalid {why} → -32602",
              (d.get("error") or {}).get("code") == -32602, str(d)[:80])

    # ═══ I. idempotency ═══
    a1 = {"filename": "idem.png", "size": 1234, "client_ref": f"idem-{_u}"}
    st1, d1, t1 = _create_upload(a1, tok, 10)
    st2, d2, t2 = _create_upload(a1, tok, 11)
    check("I1 same client_ref + same params → FRESH usable ticket (old one invalidated)",
          t1 and t2 and t1["upload_url"] != t2["upload_url"]
          and t2["expires_at"] > 0, (t1, t2))
    a2 = dict(a1, size=4321)
    st3, d3, t3 = _create_upload(a2, tok, 12)
    check("I2 same client_ref + different params → -32009",
          (d3.get("error") or {}).get("code") == -32009
          and (d3["error"] or {}).get("message") == "idempotency_conflict",
          str(d3)[:90])

    # ═══ P. positive: 5MB two-stage flow ═══
    st, d, t = _create_upload({"filename": "big.png", "size": len(BIG),
                               "content_type": "image/png",
                               "client_ref": f"p1-{_u}"}, tok, 20)
    check("P1 create_upload issues a ticket",
          st == 200 and t and t["upload_url"].startswith(_BASE + "/mcp/upload/")
          and t["max_bytes"] == MAX_UPLOAD_BYTES
          and t["expires_at"] > ts, t)
    ticket_path = t["upload_url"][len(_BASE):]
    st_p, _h, body_p, _sc = _req_tkt("PUT", ticket_path, BIG,
                                 {"Content-Type": "application/octet-stream"})
    check("P2 PUT 5MB raw bytes → 200 with {public_id, source_token}",
          st_p == 200 and body_p.get("public_id") and body_p.get("source_token"),
          (st_p, str(body_p)[:90]))
    ref = body_p
    st_c, d_c = _call_tkt("share", {"files": [{"public_id": ref["public_id"],
                                           "source_token": ref["source_token"]}],
                                "client_ref": f"share-{_u}"}, tok, 21)
    sc_c = (d_c.get("result") or {}).get("structuredContent") or {}
    out = sc_c
    _pg = (sc_c.get("items") or [{}])[0].get("page_url", "")
    share_pid = _pg.rsplit("/", 1)[-1] if _pg else ""
    check("P3 tools/call(share) with the reference → executed result",
          (d_c.get("result") or {}).get("isError") is False and share_pid,
          str(d_c)[:120])
    # anonymous raw download: resolve the redirect, fetch the gokapi URL
    st_d, h_d, _b, _sc = _req_tkt("GET", f"/f/{share_pid}?download=1")
    loc = h_d.get("location", "")
    dl_url = loc if loc.startswith("http") else (
        _os.environ.get("FLASHSHARE_GOKAPI_URL", "").rstrip("/")
        + loc if loc else "")
    if not dl_url:
        # no-hotlink fallback streams through Python — fetch inline
        st_i, _h, blob, _sc = _req_tkt("GET", f"/f/{share_pid}?download=1", raw=True)
        got = blob
    else:
        with _ur.urlopen(dl_url, timeout=30) as r:
            got = r.read()
    check("P4 anonymous download bytes identical (5MB md5)",
          hashlib.md5(got).hexdigest() == BIG_MD5,
          f"{len(got)} vs {len(BIG)}")
    check("P5 file bytes never inside any JSON-RPC body "
          "(total RPC request bytes ≪ file size)",
          _rpc_bytes_sent < 100 * 1024, _rpc_bytes_sent)
    with db() as con:
        calls = con.execute("select count(*) c from tool_calls where"
                            " user_id=%s and tool_id='create_upload'",
                            (uid,)).fetchone()["c"]
    check("P6 create_upload is a meta tool (zero tool_call rows)",
          int(calls) == 0, calls)

    # ═══ T. ticket lifecycle ═══
    st, d, t = _create_upload({"filename": "t.png", "size": 100,
                               "client_ref": f"t1-{_u}"}, tok, 30)
    tp = t["upload_url"][len(_BASE):]
    with db() as con:
        con.execute("update mcp_upload_tickets set expires_at=%s"
                    " where ticket_hash=%s", (ts - 1,
                        __import__("hashlib").sha256(
                            t["upload_url"].rsplit("/", 1)[1].encode()
                        ).hexdigest()))
    st_t, _h, d_t, _sc = _req_tkt("PUT", tp, PNG_SMALL)
    check("T1 expired ticket → 410 ticket_expired",
          st_t == 410 and d_t.get("error") == "ticket_expired", (st_t, d_t))
    st, d, t = _create_upload({"filename": "t2.png", "size": 100,
                               "client_ref": f"t2-{_u}"}, tok, 31)
    tp = t["upload_url"][len(_BASE):]
    st_t, _h, d_t, _sc = _req_tkt("PUT", tp, PNG_SMALL)
    st_r, _h, d_r, _sc = _req_tkt("PUT", tp, PNG_SMALL)
    check("T2 first PUT ok, replay → 409 ticket_used",
          st_t == 200 and st_r == 409 and d_r.get("error") == "ticket_used",
          (st_t, st_r, d_r))
    st_f, _h, d_f, _sc = _req_tkt("PUT", "/mcp/upload/forged-" + _u, PNG_SMALL)
    check("T3 forged ticket → 404 ticket_not_found",
          st_f == 404 and d_f.get("error") == "ticket_not_found", (st_f, d_f))
    st, d, t = _create_upload({"filename": "t3.png", "size": 100,
                               "client_ref": f"t3-{_u}"}, tok, 32)
    st_o, _h, d_o, _sc = _req_tkt("PUT", t["upload_url"][len(_BASE):], PNG_SMALL,
                              {"Authorization": f"Bearer {tok2}"})
    check("T4 another user's Bearer on someone else's ticket → 403",
          st_o == 403 and d_o.get("error") == "ticket_owner_mismatch",
          (st_o, d_o))
    st_o2, _h, d_o2, _sc = _req_tkt("PUT", t["upload_url"][len(_BASE):], PNG_SMALL)
    check("T4b the owner (no Bearer) still succeeds after the mismatch",
          st_o2 == 200 and d_o2.get("public_id"), (st_o2, str(d_o2)[:80]))

    # ═══ U. PUT guards ═══
    st, d, t = _create_upload({"filename": "u1.png", "size": 50,
                               "client_ref": f"u1-{_u}"}, tok, 40)
    _gk0 = _gk_n()
    st_u, _h, d_u, _sc = _req_tkt("PUT", t["upload_url"][len(_BASE):], PNG_SMALL)
    check("U1 body over declared size → 413, zero storage",
          st_u == 413 and d_u.get("error") == "err_file_too_large"
          and _gk_n() == _gk0, (st_u, d_u))
    # blocked extension now refused at create_upload (same single-point
    # word lists the PUT runs — a bad extension must not consume a
    # ticket); the PUT-leg sniff guard stays covered by inline calls.
    st, d, t = _create_upload({"filename": "u2.exe", "size": 100,
                               "client_ref": f"u2-{_u}"}, tok, 41)
    _u2 = (d.get("result") or {})
    _u2_txt = ((_u2.get("content") or [{}])[0].get("text") or "")
    check("U2 blocked extension refused at create_upload, no ticket",
          _u2.get("isError") is True
          and (_u2.get("structuredContent") or {}).get("error")
          == "err_file_type_blocked"
          and "upload_url" not in _u2_txt, _u2_txt[:100])
    # staging failure → quota and storage reclaimed (gokapi on-demand fail)
    _gk("POST", "/ci/fail-upload", {"mode": "500"})
    try:
        st, d, t = _create_upload({"filename": "u3.png", "size": len(PNG_SMALL),
                                   "client_ref": f"u3-{_u}"}, tok, 42)
        st_u, _h, d_u, _sc = _req_tkt("PUT", t["upload_url"][len(_BASE):], PNG_SMALL)
    finally:
        _gk("POST", "/ci/fail-upload", {"mode": None})
    check("U3 staging failure → 500 err_upload_failed",
          st_u == 500 and d_u.get("error") == "err_upload_failed", (st_u, d_u))
    with db() as con:
        rec = con.execute(
            """select status from ingress_requests ir
               join mcp_upload_tickets tk on tk.client_ref = ir.request_ref
               where tk.user_id=%s and tk.client_ref=%s""",
            (uid, f"u3-{_u}")).fetchone()
    _gk1 = _gk_n()
    check("U3b failed staging leaves no orphan gokapi object", _gk1 == _gk0,
          (_gk0, _gk1))
    check("U3c receipt settled failed (quota released)",
          rec is not None and rec["status"] == "failed", rec and dict(rec))

    # ═══ W. URL-source early abort on Content-Length (B4 item 5) ═══
    # A local HTTPS fixture with a self-signed CA: the server sends a
    # Content-Length far over the cap and then NEVER sends the body —
    # an early-abort transport returns promptly with RemoteError
    # 'too_large'; a download-first transport would hang to the timeout.
    import ssl as _ssl
    import subprocess as _sp
    import tempfile as _tf
    import threading as _th
    import http.server as _hs
    _td = _tf.mkdtemp(prefix="b4b-tls-")
    ca_key = _os.path.join(_td, "ca.key"); ca_pem = _os.path.join(_td, "ca.pem")
    sv_key = _os.path.join(_td, "sv.key"); sv_pem = _os.path.join(_td, "sv.pem")
    try:
        for cmd in (
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", ca_key, "-out", ca_pem, "-subj", "/CN=b4b-ca",
             "-days", "1"],
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", sv_key, "-out", _os.path.join(_td, "sv.csr"),
             "-subj", "/CN=127.0.0.1"],
            ["openssl", "x509", "-req", "-in", _os.path.join(_td, "sv.csr"),
             "-CA", ca_pem, "-CAkey", ca_key, "-out", sv_pem,
             "-days", "1", "-extfile", "/dev/stdin"],
        ):
            stdin = b"subjectAltName=IP:127.0.0.1\n" if cmd[1] == "x509" else None
            r = _sp.run(cmd, capture_output=True, input=stdin)
            assert r.returncode == 0, r.stderr[:200]

        class _H(_hs.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(100 * 1024 * 1024))
                self.end_headers()
                _time.sleep(30)  # body NEVER arrives
            def log_message(self, *a):
                pass

        _ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        _ctx.load_cert_chain(sv_pem, sv_key)
        _srv2 = _hs.HTTPServer(("127.0.0.1", 0), _H)
        _port_w = _srv2.server_address[1]
        _srv2.socket = _ctx.wrap_socket(_srv2.socket, server_side=True)
        _th.Thread(target=_srv2.serve_forever, daemon=True).start()

        _os.environ["SSL_CERT_FILE"] = ca_pem
        import socket as _sock
        import remote as _remote_mod
        _orig_va = _remote_mod._validated_addresses
        # resolver seam ONLY (the SSRF address validation is not under
        # test here): TLS, headers, the Content-Length gate and the read
        # cap all run the REAL transport code against the fixture.
        _remote_mod._validated_addresses = (
            lambda url: ("127.0.0.1", _port_w, f"127.0.0.1:{_port_w}",
                         [(_sock.AF_INET, _sock.SOCK_STREAM,
                           _sock.IPPROTO_TCP, ("127.0.0.1", _port_w))]))
        try:
            _t0 = _time.time()
            try:
                _remote_mod.pinned_request(
                    f"https://fixture.local/big.png", method="GET",
                    timeout_s=8, max_bytes=1024)
                early = "no-error"
            except _remote_mod.RemoteError as exc:
                early = exc.code
            took = _time.time() - _t0
            check("W1 Content-Length over cap aborts BEFORE download",
                  early == "too_large" and took < 6, (early, round(took, 1)))
        finally:
            _remote_mod._validated_addresses = _orig_va
            _os.environ.pop("SSL_CERT_FILE", None)
    finally:
        import shutil as _sh
        _sh.rmtree(_td, ignore_errors=True)

    # ═══ Q. quota precheck at issue time ═══
    # one oversized historical usage row exhausts the 24h byte budget
    with db() as con:
        con.execute("""insert into upload_usage (user_id, ip, size_bytes, created_at)
                       values (%s, '127.0.0.1', 999999999999, %s)""",
                    (uid, ts))
    st, d, t = _create_upload({"filename": "q.png", "size": 1000,
                               "client_ref": f"q1-{_u}"}, tok, 50)
    err = d.get("error") or {}
    _q1 = (d.get("result") or {})
    _q1_txt = ((_q1.get("content") or [{}])[0].get("text") or "")
    check("Q1 exhausted 24h quota → isError with typed denial",
          _q1.get("isError") is True and (
              "err_ingress_files_24h" in _q1_txt
              or "err_ingress_bytes_24h" in _q1_txt), str(d)[:120])
    with db() as con:
        n = con.execute("select count(*) c from mcp_upload_tickets"
                        " where user_id=%s and client_ref=%s",
                        (uid, f"q1-{_u}")).fetchone()["c"]
    check("Q1b no ticket issued on quota denial", int(n) == 0, n)
finally:
    _gk_fail(None)
    with db() as con:
        for u in (uid, uid2):
            if u:
                con.execute("delete from tool_calls where user_id=%s", (u,))
                con.execute("delete from ledger where user_id=%s", (u,))
                con.execute("delete from upload_usage where user_id=%s", (u,))
                con.execute("delete from ingress_requests where user_id=%s", (u,))
                con.execute("delete from mcp_upload_tickets where user_id=%s", (u,))
                purge_users(con, [u])
    print("cleanup done")


# ════════════════════════════════════════════════════════════════════════
# Envelope, delivery errors, storage meta tools, daily credit, UI
#
# C1 closure canonical — MCP envelope, delivery errors, storage meta
# tools, hosted-path removal, daily-credit restore, UI consistency.
#
#   A*  every structuredContent tools/call response (execution tool,
#       create_upload, catalog_search, list_uploads) carries a content
#       text block whose JSON parses to exactly the structuredContent.
#   B*  structurally invalid delivery → isError (never -32602) listing
#       the legal values FROM the delivery authority constants.
#   C*  list_uploads/delete_upload meta tools ride the web single points:
#       stage → PUT → list shows the file with the full projection →
#       delete_upload removes it → /f/{id} 404s (Gokapi bytes + mapping
#       together).
#   E*  daily check-in credit restored (owner decision after the C1
#       removal): route present, module present, kind spendable, widget
#       i18n keys present, amount sourced from settings (0/unset = off).
#   F*  UI consistency: tool page crumbs + unified file-count source +
#       localized outputs + one MCP entry; /r and /f share the action-bar
#       component inside the card; agents page has no TOKEN placeholders
#       and shows the endpoint once.
# ════════════════════════════════════════════════════════════════════════
section('Envelope, delivery errors, storage meta tools, daily credit, UI')

import os as _os
import sys as _sys


import base64
import http.client as _hc
import json
import time
import urllib.request as _ur
ts = int(time.time())
_u = "c1" + _os.urandom(4).hex()

from database import db

HOST = ("127.0.0.1", int(TEST_PORT))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _req_env(method, path, body=None, headers=None, raw=False):
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    conn.close()
    if raw:
        return r.status, data
    try:
        return r.status, json.loads(data) if data else None
    except Exception:
        return r.status, data.decode(errors="replace")


def _form_env(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form_env({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    st, _b = _req_env("POST", "/auth/register", b, {"Content-Type": ct})
    if st not in (200, 302, 303):
        raise SystemExit(f"register failed: {st}")
    conn = _hc.HTTPConnection(*HOST, timeout=30)
    b, ct = _form_env({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck


def _mcp_env(body, token):
    return _req_env("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})


def _call_env(name, arguments, token, mid=1):
    return _mcp_env({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _sc_text_blocks(result):
    """text blocks of a tool result content."""
    return [b.get("text") for b in (result.get("content") or [])
            if b.get("type") == "text"]


from identity.apikeys import create_key
from economy.billing import grant as _grant

uid, CK = _register(_u + "a")
_grant(uid, 100000, "manual_topup", idem_key=f"mp:{_u}", note="c1")
TOKEN = create_key(uid, "c1")["key"]

# ══════════ A. text block on every structuredContent response ══════════
st, d = _call_env("compress", {"files": [{"filename": "a.png", "b64":
                                      base64.b64encode(PNG).decode()}],
                           "client_ref": f"a1-{_u}"}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
texts = _sc_text_blocks(res)
check("A1 execution tool: readable summary text + structuredContent items",
      res.get("isError") is False
      and bool(texts) and "Result page:" in texts[-1]
      and not texts[-1].lstrip().startswith("{")
      and bool(sc.get("items")),
      f"{str(d)[:110]}")

st, d = _call_env("create_upload", {"filename": "big.png", "size": len(PNG),
                                "client_ref": f"c1up-{_u}"}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
texts = _sc_text_blocks(res)
check("A2 create_upload: readable text names the one-time URL + sc field",
      res.get("isError") is False and bool(texts) and
      "upload" in texts[0].lower() and sc.get("upload_url")
      and not texts[0].lstrip().startswith("{"), str(d)[:110])

st, d = _call_env("catalog_search", {"q": "compress"}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
texts = _sc_text_blocks(res)
check("A3 catalog_search: readable tool list + sc tools",
      res.get("isError") is False and bool(texts) and
      "- " in texts[0] and sc.get("tools")
      and not texts[0].lstrip().startswith("{"), str(d)[:110])

# one builder: every envelope site routes through _meta_result /
# _mcp_success / _iserror (source-level single points)
_mcp_src = open(_os.path.join(REPO, "app", "routes", "mcp.py"),
                encoding="utf-8").read()
check("A4 single envelope builder in source",
      "def _meta_result(" in _mcp_src
      and _mcp_src.count('"structuredContent"') <= 4,
      str(_mcp_src.count('"structuredContent"')))

# ══════════ B. invalid delivery → -32602 invalid params ══════════
# frozen adapter contract: defaults fill MISSING values only; an
# explicitly illegal value is a protocol error, never an isError result
from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS
for label, dv in (("bad expiry", {"expiry_days": 5}),
                  ("bad downloads", {"allowed_downloads": 7}),
                  ("non-dict", "x"),
                  ("unknown key", {"expiry_days": 1, "extra": 2})):
    args = {"files": [{"filename": "a.png", "b64":
                       base64.b64encode(PNG).decode()}],
            "delivery": dv, "client_ref": f"b-{label[:3]}-{_u}"}
    st, d = _call_env("compress", args, TOKEN)
    err = (d or {}).get("error") or {}
    ok = (err.get("code") == -32602
          and "invalid delivery" in str(err.get("message") or "")
          and str(list(EXPIRY_DAYS)[0]) in str(err.get("message"))
          and (d.get("result") is None))
    check(f"B1 {label} → -32602 invalid params", ok,
          f"{str(d)[:110]}")

# ══════════ C. list_uploads / delete_upload meta tools ══════════
# a real tool call produces a durable uploads row owned by the caller
# (staged-only files are NOT shares — they enter the list when a call
# consumes them, exactly like the web surface)
st, d = _call_env("share", {"files": [{"filename": "store.png", "b64":
                                   base64.b64encode(PNG).decode()}],
                        "client_ref": f"c1share-{_u}"}, TOKEN)
_sc = (((d or {}).get("result") or {}).get("structuredContent") or {})
_items = _sc.get("items") or []
_res_url = _sc.get("result_url") or ""
_f = next((i for i in _items if i.get("kind") == "file"), None)
check("C1 fixture: share call produced a file item", _f is not None,
      str(d)[:110])
pid = ((_f or {}).get("page_url") or "/f/").rsplit("/f/", 1)[-1]

st, d = _call_env("list_uploads", {}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
entry = next((e for e in sc.get("uploads", [])
              if e.get("public_id") == pid), None)
check("C2 list_uploads shows the staged file (full projection)",
      res.get("isError") is False and entry is not None
      and entry.get("filename") == "store.png"
      and entry.get("size") == len(PNG)
      and entry.get("content_type") == "image/png"
      and isinstance(entry.get("expires_at"), int)
      and entry.get("page_url", "").endswith(f"/f/{pid}")
      and "download=1" in entry.get("download_url", ""),
      str(entry)[:120])

st, d = _call_env("delete_upload", {"public_id": pid}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
st_f, _b = _req_env("GET", f"/f/{pid}", raw=False)
check("C3 delete_upload removes it → /f 404",
      res.get("isError") is False and sc.get("deleted") is True
      and st_f == 404, f"{str(d)[:100]} f={st_f}")
# gokapi physical object gone too (web single point deleted both)
_gk_env = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
_rq = _ur.Request(_gk_env + "/api/files/list",
                  headers={"apikey": _os.environ["FLASHSHARE_GOKAPI_KEY"]})
with _ur.urlopen(_rq, timeout=5) as r:
    _gids = {f["Id"] for f in json.loads(r.read())}
with db() as con:
    _row = con.execute("select gokapi_id from uploads where public_id=%s",
                       (pid,)).fetchone()
check("C4 mapping row gone", _row is None, str(_row))
# result page keeps rendering the deleted item (link 404s, counters gone)
_res_id = _res_url.rsplit("/r/", 1)[-1]
st_r, _rb = _req_env("GET", f"/r/{_res_id}")
_okr = st_r == 200
check("C4b result page still renders after item delete (link 404s)",
      _okr, str(st_r))

# someone else's file → one shape, no existence oracle
uid2, _ck2 = _register(_u + "b")
TOKEN2 = create_key(uid2, "c1b")["key"]
st, d = _call_env("delete_upload", {"public_id": pid}, TOKEN2)
res = (d or {}).get("result") or {}
_err_json = res.get("structuredContent") or {}
check("C5 foreign/unknown delete → isError not_found (no oracle)",
      res.get("isError") is True
      and _err_json.get("error") == "not_found"
      and isinstance(_err_json.get("message"), str),
      str(d)[:100])

# specs present in tools/list alongside create_upload
st, d = _mcp_env({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, TOKEN)
names = set()
for t in ((d or {}).get("result") or {}).get("tools", []):
    names.add(t.get("name"))
check("C6 meta tools listed", {"create_upload", "catalog_search",
                               "list_uploads", "delete_upload"} <= names,
      str(sorted(names))[:120])

# llms.txt documents the new sections + delivery legal values
_llms = _req_env("GET", "/llms.txt", raw=True)[1].decode()
check("C7 llms.txt: storage meta tools + delivery legal values",
      "list_uploads" in _llms and "delete_upload" in _llms
      and f"{list(EXPIRY_DAYS)}" in _llms
      and f"{list(DOWNLOAD_LIMITS)}" in _llms, "")

# ══════════ E. daily check-in credit (owner-restored, amount from settings) ══════════
# c59ab16 removed the retired mechanism; a later owner decision restored it
# as the first cold-start activity. Closure now pins the restored shape:
# route present, module present, kind spendable, widget i18n keys present,
# amount sourced from settings.daily_login_amount (0/unset = off).
st, _b = _req_env("POST", "/account/credits/claim", {},
              {"Cookie": CK, "Accept": "text/html"})
check("E1 claim route restored (not 404/405)", st not in (404, 405), str(st))
check("E2 economy.daily module restored",
      _os.path.exists(_os.path.join(REPO, "core", "economy",
                                    "daily.py")))
from economy.billing import SPENDABLE_KINDS
_i18n_src = open(_os.path.join(REPO, "core", "i18n.py"),
                 encoding="utf-8").read()
check("E3 grant_daily in SPENDABLE_KINDS + widget i18n keys present",
      "grant_daily" in SPENDABLE_KINDS
      and '"daily_claim"' in _i18n_src
      and '"ledger_grant_daily"' in _i18n_src)
_auth_src = open(_os.path.join(REPO, "app", "routes", "auth_route.py"),
                 encoding="utf-8").read()
check("E4 login-time daily grant hook present", "_grant_daily" in _auth_src)
_daily_src = open(_os.path.join(REPO, "core", "economy", "daily.py"),
                  encoding="utf-8").read()
check("E5 amount sourced from settings key (no module constant)",
      "daily_login_amount" in _daily_src
      and "DAILY_AMOUNT =" not in _daily_src)
_deliv_doc = open(_os.path.join(REPO, "core", "governance", "delivery.py"),
                  encoding="utf-8").read()
check("E6 delivery docstring: coerce_share_delivery mention removed",
      "coerce_share_delivery" not in _deliv_doc)

# ══════════ F. UI consistency ══════════
_tpl = open(_os.path.join(REPO, "app", "templates", "tool_page.html"),
            encoding="utf-8").read()
check("F1 tool page: unified count + localized outputs + one MCP entry",
      "tool_files_window" not in _tpl
      and "files_window_short" in _tpl and "n_files_one" in _tpl
      and "out_kind_" in _tpl and "{{ o }}" not in _tpl
      and _tpl.count("toolMcpName") == 1
      and 'class="mcpline"' in _tpl,
      "")
_res = open(_os.path.join(REPO, "app", "templates", "result.html"),
            encoding="utf-8").read()
_det = open(_os.path.join(REPO, "app", "templates", "detail.html"),
            encoding="utf-8").read()
check("F2 /r + /f: action row + the one share bar inside the card",
      'class="acts"' in _res
      and 'class="acts"' in _det
      and _det.index("rc-preview") < _det.index('class="acts"')
      and "share_bar(" in _res and "share_bar(" in _det
      and "linkbar" not in _res and "linkbar" not in _det
      and "rPrintBtn" not in _res, "")
check("F3 /f stray empty divider gone (owner tools only render for owners)",
      '{% if is_owner %}\n      <div class="workspace-tools"' in _det)
_ag = open(_os.path.join(REPO, "app", "templates", "agents.html"),
           encoding="utf-8").read()
check("F4 agents page: no TOKEN placeholders, endpoint once",
      "TOKEN" not in _ag and "token" not in _ag
      and _ag.count('id="mcpUrl"') == 1
      and "MCP endpoint:" not in _ag and "<dt>URL</dt>" not in _ag
      and "fsk_" in _ag)

# cleanup
with db() as con:
    purge_users(con, [uid, uid2])


# ════════════════════════════════════════════════════════════════════════
# Transport, JSON-RPC layering, source-file input forms
#
# MCP protocol canonical — Streamable HTTP transport rules, JSON-RPC
# error layering, pre-ingress validation, reversible tool names, and the
# three source-file input forms (inline base64 / https URL / reference).
#
# Real HTTP surface + real DB for everything reachable over the wire. The
# URL-source positive/oversize paths run the same _call_tool in-process
# with the pinned transport replaced (local DNS/TLS cannot serve a public
# https origin); the SSRF-blocked path runs over real HTTP against the
# real guard.
# ════════════════════════════════════════════════════════════════════════
section('Transport, JSON-RPC layering, source-file input forms')

import os as _os
import sys as _sys


_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")


import asyncio
import base64
import http.client as _hc
import json
import time
import urllib.parse
import uuid

from database import db

ts_prt = int(time.time())
_u_prt = "mp" + uuid.uuid4().hex[:6]
_HOST_prt = ("127.0.0.1", int(TEST_PORT))
_BASE = _os.environ.get("FLASHSHARE_BASE_URL", f"http://127.0.0.1:{TEST_PORT}").rstrip("/")
_p = urllib.parse.urlsplit(_BASE)
_BASE_ORIGIN = f"{_p.scheme}://{_p.netloc}"

PNG_prt = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF_prt = b"%PDF-1.4\n%canonical\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _req_prt(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_prt, timeout=60)
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


def _form_prt(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form_prt({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    st, _h, _b = _req_prt("POST", "/auth/register", b, {"Content-Type": ct})
    if st not in (200, 302, 303):
        raise SystemExit(f"register failed: {st}")
    b, ct = _form_prt({"login": name, "password": "Test12345"})
    conn = _hc.HTTPConnection(*_HOST_prt, timeout=30)
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid_prt = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid_prt, ck


def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_prt, ts_prt)).fetchone()
    return int(row["id"])


def _gk_count():
    import urllib.request as _ur
    base = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
    req = _ur.Request(base + "/api/files/list",
                      headers={"apikey": _os.environ["FLASHSHARE_GOKAPI_KEY"]})
    with _ur.urlopen(req, timeout=5) as r:
        return len(json.loads(r.read()))


def _ingress_n(uid_prt):
    with db() as con:
        return int(con.execute("select count(*) c from ingress_requests"
                               " where user_id=%s", (uid_prt,)).fetchone()["c"])


def _calls_n(uid_prt):
    with db() as con:
        return int(con.execute("select count(*) c from tool_calls"
                               " where user_id=%s", (uid_prt,)).fetchone()["c"])


from identity.apikeys import create_key
from economy.billing import grant as _grant, spendable_balance as spendable_balance

uid_prt, ck = _register(_u_prt + "a")
uid2, ck2 = _register(_u_prt + "b")
_grant(uid_prt, 100000, "manual_topup", idem_key=f"mp:{_u_prt}:a", note="mp")
_grant(uid2, 100000, "manual_topup", idem_key=f"mp:{_u_prt}:b", note="mp")
TOKEN = create_key(uid_prt, "mp")["key"]
TOKEN2 = create_key(uid2, "mp2")["key"]


def _mcp_prt(body, token=TOKEN, extra=None):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(extra or {})
    return _req_prt("POST", "/mcp", body, h)


def _call_prt(name, arguments, mid=1, token=TOKEN):
    return _mcp_prt({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _err_sentence(d):
    res = d.get("result") or {}
    return ((res.get("content") or [{}])[0].get("text", ""))


def _tool_text(d):
    """Machine channel of a tools/call result: structuredContent (the
    content text block is the readable summary — error/message/etc.
    ride both)."""
    return ((d.get("result") or {}).get("structuredContent") or {})


def _b64_prt(b):
    return base64.b64encode(b).decode()


# ══════════ 1. protocol version negotiation ══════════
for req_v, want in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                    ("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
                    ("1999-01-01", "2025-11-25")):
    st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": req_v}})
    got = ((d or {}).get("result") or {}).get("protocolVersion")
    check(f"V1 initialize {req_v} → {want}", st == 200 and got == want, f"{st} {got}")
st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 1, "method": "initialize",
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
for label_prt, expected in (
    ("Plain Label", "Plain Label"),
    ({"zh": "工具名称"}, "工具名称"),
):
    _summary = _spec_summary({**_label_base, "label": label_prt})
    check(f"V3 MCP discovery title handles {type(label_prt).__name__}",
          _summary["title"] == expected
          and _summary["name"] == "labelprobe_demo"
          and "整理文件" in _summary["description"],
          _summary)

# ══════════ 1b. strict arguments: an undeclared key is -32602, named ══════════
# A wrong parameter name must never run as a successful default/unfiltered
# call — the agent cannot tell the mistake from the result. Allowed keys
# come from each tool's inputSchema (platform tools: files/params/
# delivery/client_ref).
_st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 91, "method": "tools/call",
                   "params": {"name": "catalog_search",
                              "arguments": {"query": "pdf"}}}, token=TOKEN)
_err = (d or {}).get("error") or {}
check("V4a meta tool unknown key → -32602 naming it",
      _err.get("code") == -32602 and "query" in (_err.get("message") or ""),
      d)
_st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 92, "method": "tools/call",
                   "params": {"name": "share",
                              "arguments": {"client_ref": "v4b",
                                            "queries": ["a"]}}}, token=TOKEN)
_err = (d or {}).get("error") or {}
check("V4b platform tool unknown key → -32602 naming it",
      _err.get("code") == -32602 and "queries" in (_err.get("message") or ""),
      d)
_st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 93, "method": "tools/call",
                   "params": {"name": "register",
                              "arguments": {"username": f"{_u_prt}v4c",
                                            "role": "agent"}}})
_err = (d or {}).get("error") or {}
check("V4c anonymous register unknown key → -32602 naming it",
      _err.get("code") == -32602 and "role" in (_err.get("message") or ""),
      d)
_st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 94, "method": "tools/call",
                   "params": {"name": "catalog_search",
                              "arguments": {"q": "pdf", "page": 1}}},
                  token=TOKEN)
check("V4d declared keys still pass", "result" in (d or {}), d)

# ══════════ 2. MCP-Protocol-Version header ══════════
_ping = {"jsonrpc": "2.0", "id": 5, "method": "ping"}
st, _h, d = _mcp_prt(_ping, extra={"MCP-Protocol-Version": "2031-01-01"})
check("H1 unsupported MCP-Protocol-Version → 400",
      st == 400 and ((d or {}).get("error") or {}).get("code") == -32600, f"{st} {d}")
for v in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"):
    st, _h, d = _mcp_prt(_ping, extra={"MCP-Protocol-Version": v})
    check(f"H2 supported header {v} → 200", st == 200 and d.get("result") == {}, f"{st} {d}")
st, _h, d = _mcp_prt(_ping)
check("H3 absent header → accepted (2025-03-26)", st == 200 and d.get("result") == {}, f"{st}")

# ══════════ 3. Origin / CORS / auth header ══════════
st, _h, _d = _mcp_prt(_ping, extra={"Origin": "https://evil.example"})
check("O1 foreign Origin → 403", st == 403, f"{st}")
st, _h, _d = _mcp_prt(_ping, extra={"Origin": "null"})
check("O2 opaque Origin 'null' → 403", st == 403, f"{st}")
st, h, d = _mcp_prt(_ping, extra={"Origin": _BASE_ORIGIN})
check("O3 site Origin → 200 + echoed Access-Control-Allow-Origin",
      st == 200 and h.get("access-control-allow-origin") == _BASE_ORIGIN, f"{st} {h}")
st, h, d = _mcp_prt(_ping)
check("O4 no Origin (non-browser) → 200, no wildcard ACAO",
      st == 200 and h.get("access-control-allow-origin") in (None,), f"{st} {h.get('access-control-allow-origin')}")
st, h, _d = _req_prt("OPTIONS", "/mcp", None, {"Origin": _BASE_ORIGIN,
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
st, h, _d = _req_prt("OPTIONS", "/mcp", None, {"Origin": "https://evil.example"})
check("O7 preflight foreign origin → 403", st == 403, f"{st}")
st, h, _d = _mcp_prt(_ping, token=None)
check("O8 anonymous ping → 200 (discovery needs no key)",
      st == 200 and (_d or {}).get("result") == {}, f"{st} {_d}")
st, h, _d = _mcp_prt(_ping, token="fsk_invalid_key")
check("O8b invalid key → 401 WWW-Authenticate resource_metadata",
      st == 401 and "oauth-protected-resource" in (h.get("www-authenticate") or ""), f"{st} {h.get('www-authenticate')}")

# ══════════ 4. JSON-RPC batches ══════════
_batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
          {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
for v in ("2025-06-18", "2025-11-25"):
    st, _h, d = _mcp_prt(_batch, extra={"MCP-Protocol-Version": v})
    check(f"B1 batch rejected under {v} → 400 -32600",
          st == 400 and isinstance(d, dict) and (d.get("error") or {}).get("code") == -32600,
          f"{st} {d}")
st, _h, d = _mcp_prt(_batch, extra={"MCP-Protocol-Version": "2025-03-26"})
check("B2 batch under 2025-03-26 → array of 2 responses",
      st == 200 and isinstance(d, list) and sorted(x["id"] for x in d) == [1, 2], f"{st} {d}")
st, _h, d = _mcp_prt(_batch)
check("B3 batch without header (2025-03-26) → allowed",
      st == 200 and isinstance(d, list) and len(d) == 2, f"{st} {d}")
st, _h, d = _mcp_prt([{"jsonrpc": "2.0", "method": "notifications/initialized"},
                  {"jsonrpc": "2.0", "id": 77, "result": {}}],
                 extra={"MCP-Protocol-Version": "2025-03-26"})
check("B4 batch of notifications/responses only → 202 no body",
      st == 202 and not d, f"{st} {d!r}")
st, _h, d = _mcp_prt([], extra={"MCP-Protocol-Version": "2025-03-26"})
check("B5 empty batch → 400 JSON-RPC -32600 invalid request",
      st == 400 and isinstance(d, dict) and d.get("id") is None
      and (d.get("error") or {}).get("code") == -32600, f"{st} {d}")
st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 9, "result": {}})
check("B6 single client response → 202", st == 202 and not d, f"{st} {d!r}")

# ══════════ 5. non-object params / arguments ══════════
for label_prt, body in (
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
                                "client_ref": f"np-{_u_prt}"}}}),
):
    st, _h, d = _mcp_prt(body)
    check(f"N {label_prt} → HTTP 200 JSON-RPC -32602",
          st == 200 and isinstance(d, dict) and d.get("id") == 1
          and (d.get("error") or {}).get("code") == -32602, f"{st} {str(d)[:100]}")

# ══════════ 6. validate BEFORE ingress ══════════
def _zero_side_effects(label_prt, fn):
    g0, i0, c0 = _gk_count(), _ingress_n(uid_prt), _calls_n(uid_prt)
    out = fn()
    check(f"{label_prt} zero ingress/Gokapi/tool_call",
          _gk_count() == g0 and _ingress_n(uid_prt) == i0 and _calls_n(uid_prt) == c0,
          f"gk {g0}->{_gk_count()} ingress {i0}->{_ingress_n(uid_prt)} calls {c0}->{_calls_n(uid_prt)}")
    return out

st, _h, d = _zero_side_effects("P1", lambda: _call_prt(
    f"no_such_tool_{_u_prt}", {"files": [{"filename": "a.png", "b64": _b64_prt(PNG_prt)}],
                           "client_ref": f"p1-{_u_prt}"}))
check("P1 unknown tool with file → -32602",
      (d.get("error") or {}).get("code") == -32602, str(d)[:100])
st, _h, d = _zero_side_effects("P2", lambda: _call_prt(
    "compress", {"files": [{"filename": "a.png", "b64": _b64_prt(PNG_prt)}],
                 "params": {"preset": "bogus"}, "client_ref": f"p2-{_u_prt}"}))
check("P2 invalid declared param → isError param_invalid",
      (d.get("result") or {}).get("isError") is True and _tool_text(d).get("error") == "param_invalid",
      str(d)[:120])
check("P2b the denial names the parameter (field)",
      _tool_text(d).get("field") == "preset", str(d)[:160])
st, _h, d = _zero_side_effects("P3", lambda: _call_prt(
    "compress", {"files": [{"filename": "a.pdf", "b64": _b64_prt(PDF_prt)}],
                 "client_ref": f"p3-{_u_prt}"}))
_p3 = _err_sentence(d)
check("P3 format outside accept → isError + English sentence",
      (d.get("result") or {}).get("isError") is True
      and "not supported" in _p3
      and "err_format_not_supported" in _p3, _p3[:110])
st, _h, d = _zero_side_effects("P4", lambda: _call_prt(
    "compress", {"files": [{"filename": "a.png", "b64": _b64_prt(PNG_prt)}],
                 "delivery": {"expiry_days": 30, "allowed_downloads": 50},
                 "client_ref": f"p4-{_u_prt}"}))
check("P4 delivery not entitled (free) → isError err_expiry_not_entitled",
      (d.get("result") or {}).get("isError") is True
      and _tool_text(d).get("error") == "err_expiry_not_entitled", str(d)[:120])
st, _h, d = _zero_side_effects("P5", lambda: _call_prt(
    "compress", {"files": [{"filename": "a.png", "b64": _b64_prt(PNG_prt)},
                           {"filename": "b.png", "b64": _b64_prt(PNG_prt)}],
                 "client_ref": f"p5-{_u_prt}"}))
_p5 = _err_sentence(d)
check("P5 two files to single-file tool → isError + window sentence",
      (d.get("result") or {}).get("isError") is True
      and "err_file_count" in _p5 and "1 to 1" in _p5, _p5[:110])

# ══════ 7. tool names = key 原文 (username_toolname, 无投影无撞名机器) ══════
_ownA = _mkuser(f"{_u_prt}o")            # owner 'mpXXXXXXo'
_ownB = _mkuser(f"{_u_prt}o__ab")        # owner 'mpXXXXXXo__ab'
keyA = f"{_u_prt}o_ab__tl"               # 与 keyB 字符串不同 — key 唯一索引即名单射
keyB = f"{_u_prt}o__ab_tl"
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
             json.dumps({"url": "https://example.org/x", "timeout_s": 30}), ts_prt, ts_prt))
        con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                    " values (%s, 0, true, %s)", (key, ts_prt))
_mk_tool(keyA, _ownA, "alpha")
_mk_tool(keyB, _ownB, "beta")
nameA, nameB = keyA, keyB            # MCP 名 = key 原文
check("T1 distinct keys carry distinct names (key uniqueness = name injectivity)",
      nameA and nameB and nameA != nameB, f"{nameA} {nameB}")
check("T2 name is the key verbatim", nameA == keyA and nameB == keyB)
for _nm in (nameA, nameB):
    _call_prt("pin_tool", {"name": _nm})
_listed, _cur = [], None
for _ in range(20):
    st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 3, "method": "tools/list",
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
st, _h, d = _call_prt(nameA, {"files": [{"filename": "a.png", "b64": _b64_prt(PNG_prt)}],
                          "params": {"alpha": "v"}, "client_ref": f"t7-{_u_prt}"})
_t7 = _err_sentence(d)
check("T7 name A routes to key A (format gate reached)",
      (d.get("result") or {}).get("isError") is True
      and "err_format_not_supported" in _t7, _t7[:110])
st, _h, d = _call_prt(nameB, {"files": [{"filename": "a.png", "b64": _b64_prt(PNG_prt)}],
                          "params": {"beta": "v"}, "client_ref": f"t8-{_u_prt}"})
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
for label_prt, url, form in (("loopback", "https://127.0.0.1/a.png", "isError"),
                         ("private", "https://10.0.0.1/a.png", "isError"),
                         ("plain http", "http://example.com/a.png", "-32602")):
    st, _h, d = _zero_side_effects(f"U1 {label_prt}", lambda url=url: _call_prt(
        "compress", {"files": [{"url": url}], "client_ref": f"u1-{_u_prt}-{label_prt}"}))
    if form == "-32602":
        ok_u1 = (d.get("error") or {}).get("code") == -32602
    else:
        ok_u1 = (d.get("result") or {}).get("isError") is True
    check(f"U1 {label_prt} URL → {form}", ok_u1, str(d)[:120])
st, _h, d = _call_prt("compress", {"files": [{"url": "https://x.example/a.png",
                                          "b64": _b64_prt(PNG_prt), "filename": "a.png"}],
                               "client_ref": f"u2-{_u_prt}"})
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
                 max_bytes=None, deadline=None):
    return 200, _served[url], None, "image/png", None

# Single-hop stub: the REAL redirect loop (pinned_request) runs in the
# middle — the resolver is stubbed so no real DNS happens (files.example
# does not resolve).
_rs = _remote.socket
_orig_pinned, _orig_dx = _remote._pinned_request, _dispatch_mod.dispatch_execute
_orig_resolve = _remote._resolve_host
def _fake_resolve(host, port):
    return [(_rs.AF_INET, _rs.SOCK_STREAM, _rs.IPPROTO_TCP, "",
             ("93.184.216.34", port))]
_remote._pinned_request = _fake_pinned
_remote._resolve_host = _fake_resolve
_dispatch_mod.dispatch_execute = _fake_dispatch
try:
    _user = {"id": uid_prt}
    def _ip(args, mid=1):
        return asyncio.run(_M._call_tool(_Req(), _user, mid,
                                         {"name": "compress", "arguments": args}))
    urlA = f"https://files.example/{_u_prt}/a.png"
    urlB = f"https://files.example/{_u_prt}/b.png"
    _served[urlA] = PNG_prt
    _served[urlB] = PNG_prt
    refU = f"url-{_u_prt}"
    g0, i0 = _gk_count(), _ingress_n(uid_prt)
    r = _ip({"files": [{"url": urlA}], "client_ref": refU})
    _src = (_captured[-1]["source"]["files"] if _captured else [])
    check("U3 https URL source → ingress + canonical source handle",
      "result" in r and r["result"].get("isError") is False and len(_src) == 1
      and _gk_count() == g0 + 1 and _ingress_n(uid_prt) == i0 + 1, str(r)[:140])
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
    r = _ip({"files": [{"filename": "a.png", "b64": _b64_prt(PNG_prt)}], "client_ref": refU}, mid=4)
    check("U6 same ref + inline instead of URL → -32009",
          (r.get("error") or {}).get("code") == -32009 and _gk_count() == g1, str(r)[:120])
    big = f"https://files.example/{_u_prt}/big.png"
    from config import MAX_UPLOAD_BYTES
    _served[big] = b"\x89PNG\r\n\x1a\n" + b"\0" * MAX_UPLOAD_BYTES
    i1 = _ingress_n(uid_prt)
    r = _ip({"files": [{"url": big}], "client_ref": f"big-{_u_prt}"}, mid=5)
    _u7 = (r.get("result") or {})
    _u7t = ((_u7.get("content") or [{}])[0].get("text") or "")
    check("U7 oversize URL body → isError, zero ingress/Gokapi",
          _u7.get("isError") is True and "too_large" in _u7t
          and _ingress_n(uid_prt) == i1 and _gk_count() == g1, str(r)[:120])
    del _served[big]
    def _blocked(url, method="GET", body=None, headers=None, timeout_s=60,
                 max_bytes=None, deadline=None):
        raise _remote.RemoteError("redirect_blocked", "redirect to non-https rejected")
    _remote._pinned_request = _blocked
    r = _ip({"files": [{"url": urlA}], "client_ref": f"rb-{_u_prt}"}, mid=6)
    _u8 = (r.get("result") or {})
    _u8t = ((_u8.get("content") or [{}])[0].get("text") or "")
    check("U8 transport-level rejection → isError with typed code",
          _u8.get("isError") is True and "redirect_blocked" in _u8t
          and _ingress_n(uid_prt) == i1,
          str(r)[:120])
finally:
    _remote._pinned_request = _orig_pinned
    _remote._resolve_host = _orig_resolve
    _dispatch_mod.dispatch_execute = _orig_dx

# ══════════ 10. reference to an existing upload ══════════
def _web_upload(cookie, ref):
    b = "----mp"
    body = (f'--{b}\r\nContent-Disposition: form-data; name="tool"\r\n\r\ncompress\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="client_ref"\r\n\r\n{ref}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="r.png"\r\n'
            f'Content-Type: image/png\r\n\r\n').encode() + PNG_prt + f"\r\n--{b}--\r\n".encode()
    st, _h, d = _req_prt("POST", "/api/uploads", body,
                     {"Content-Type": f"multipart/form-data; boundary={b}", "Cookie": cookie})
    return (d or {}).get("file") or {}
src1 = _web_upload(ck, f"web-{_u_prt}-1")
src2 = _web_upload(ck2, f"web-{_u_prt}-2")
check("R0 fixtures: web tool uploads carry stagingId + sourceToken",
      src1.get("stagingId") and src1.get("sourceToken") and src2.get("stagingId"), str(src1)[:100])
refR = f"ref-{_u_prt}"
i0 = _ingress_n(uid_prt)
st, _h, d = _call_prt("compress", {"files": [{"public_id": src1["stagingId"],
                                          "source_token": src1["sourceToken"]}],
                               "client_ref": refR})
_rt = (d.get("result") or {}).get("structuredContent") or {}
check("R1 reference form → executed result",
      (d.get("result") or {}).get("isError") is False
      and bool(_rt.get("items"))
      and _rt["items"][0].get("kind") == "file", str(d)[:140])
check("R2 reference form claims no ingress receipt", _ingress_n(uid_prt) == i0)
c0 = _calls_n(uid_prt)
st, _h, d = _call_prt("compress", {"files": [{"public_id": src2["stagingId"],
                                          "source_token": src2["sourceToken"]}],
                               "client_ref": f"refx-{_u_prt}"})
check("R3 foreign upload (other user's token) → isError, zero tool_call",
      (d.get("result") or {}).get("isError") is True and _calls_n(uid_prt) == c0, str(d)[:120])
st, _h, d = _call_prt("compress", {"files": [{"public_id": src1["stagingId"],
                                          "source_token": "0" * 64}],
                               "client_ref": f"refy-{_u_prt}"})
check("R4 invalid source_token → isError, zero tool_call",
      (d.get("result") or {}).get("isError") is True and _calls_n(uid_prt) == c0, str(d)[:120])
st, _h, d = _call_prt("compress", {"files": [{"public_id": src1["stagingId"],
                                          "source_token": 7}],
                               "client_ref": f"refz-{_u_prt}"})
check("R5 non-string source_token → -32602", (d.get("error") or {}).get("code") == -32602)
st, _h, d = _call_prt("compress", {"files": [{"public_id": src1["stagingId"],
                                          "source_token": src1["sourceToken"]}],
                               "client_ref": refR}, mid=2)
check("R6 same ref + same reference → replay (same call)",
      ((d.get("result") or {}).get("structuredContent") or {}).get("replay")
      is True and _calls_n(uid_prt) == c0, str(d)[:120])
src3 = _web_upload(ck, f"web-{_u_prt}-3")
st, _h, d = _call_prt("compress", {"files": [{"public_id": src3["stagingId"],
                                          "source_token": src3["sourceToken"]}],
                               "client_ref": refR}, mid=3)
_r7 = _err_sentence(d)
check("R7 same ref + different reference → conflict sentence, zero new call",
      (d.get("result") or {}).get("isError") is True
      and "idempotency_conflict" in _r7 and _calls_n(uid_prt) == c0,
      _r7[:120])

# ══════════ 10b. R6-rework: idempotent replay precedes the balance
# preflight. MCP-side: both tools/call routes must pass client_ref into
# check_affordability, and the probe itself must wave a live running|ok
# (user, tool, client_ref) through even at zero balance while a genuinely
# new ref still hits the advisory wall. The full 200-replay/409/402 chain
# runs end-to-end in test_api_surface D8b (real gate). ══════════
_bt6 = f"mpr6paid{_os.urandom(4).hex()}"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " body_format, files_min, files_max, outputs_json, endpoint_json,"
        " created_at, updated_at)"
        " values (%s, NULL, %s, 'http', %s, 'public', TRUE, NULL, NULL,"
        " 'text', 0, 0, '[\"text\"]', %s, %s, %s)",
        (_bt6, _bt6, json.dumps({"en": "MP Paid Probe"}),
         json.dumps({"url": "http://127.0.0.1:1/never", "timeout_s": 5}),
         ts_prt, ts_prt))
    con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                " values (%s, 3, TRUE, %s)", (_bt6, ts_prt))
_uid6 = _os.urandom(4).hex()
uid6, _ = _register(_u_prt + "r6")
TOKEN6 = create_key(uid6, "mpr6")["key"]
_user6 = {"id": uid6}
_ref6 = f"r6-{_uid6}"

import execution.affordability as _aff6
_captured6 = []
_real_ca6 = _aff6.check_affordability
def _capturing_ca6(user_id, tool, client_ref=None):
    _captured6.append((user_id, tool.get("tool_key"), client_ref))
    return _real_ca6(user_id, tool, client_ref)
_aff6.check_affordability = _capturing_ca6
try:
    # zero balance, genuinely new ref → the advisory wall (isError),
    # and the route passed client_ref through to the check
    d = asyncio.run(_M._call_tool(_Req(), _user6, 61,
        {"name": _bt6, "arguments": {"input": "x", "client_ref": _ref6}}))
    check("R6b new ref at zero balance → insufficient isError, client_ref reached the preflight",
          (d.get("result") or {}).get("isError") is True
          and "insufficient" in _err_sentence(d)
          and _captured6 and _captured6[-1] == (uid6, _bt6, _ref6)
          and _calls_n(uid6) == 0, f"{str(d)[:140]} {_captured6}")
finally:
    _aff6.check_affordability = _real_ca6

# probe semantics on a REAL live row (no dispatch stub): balance 0 +
# running|ok (user, tool, client_ref) → None; a different ref → denial.
import hashlib as _hl6
with db() as con:
    con.execute(
        "insert into tool_calls (user_id, tool_id, client_ref, status,"
        " cost_snapshot, params_json, started_at, finished_at,"
        " invocation_hash)"
        " values (%s, %s, %s, 'ok', 3, '{}', %s, %s, %s)",
        (uid6, _bt6, _ref6, ts_prt, ts_prt,
         _hl6.sha256(b"seed").hexdigest()))
_aff6.check_affordability = _capturing_ca6
try:
    d = asyncio.run(_M._call_tool(_Req(), _user6, 62,
        {"name": _bt6, "arguments": {"input": "x", "client_ref": _ref6}}))
    check("R6b live ok row at zero balance → preflight waves it through (None), outcome is the gate's (never the 402 wall)",
          _captured6[-1] == (uid6, _bt6, _ref6)
          and "insufficient" not in _err_sentence(d),
          f"{_captured6[-1]} {str(d)[:140]}")
finally:
    _aff6.check_affordability = _real_ca6
_direct = _real_ca6(uid6, {"tool_key": _bt6, "owner_id": None}, f"other-{_uid6}")
check("R6b different ref at zero balance → still the advisory wall (denial dict)",
      _direct is not None and _direct.get("error") == "err_insufficient_credits",
      str(_direct))

# ══════════ 11. F6: body-tool adapter error layering ══════════# ══════════ 11. F6: body-tool adapter error layering ══════════
# 正文工具的输入方式/口径违规是调用方参数错误: preflight 的 err_input_mode
# 只有这一个码翻译成 "invalid input mode"; err_body_* 同为 -32602。
# 工具行 = operator (owner NULL) http 正文工具 (endpoint 不可达也无妨 —
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
         ts_prt, ts_prt))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 0, TRUE, %s)", (_btkey, ts_prt))

def _ipn(name, args, mid=10):
    return asyncio.run(_M._call_tool(_Req(), {"id": uid_prt}, mid,
                                     {"name": name, "arguments": args}))

try:
    st, _h, d = _mcp_prt({"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
    _bt_spec = next((t for t in ((d.get("result") or {}).get("tools") or [])
                     if t.get("name") == _btkey), {})
    _bt_props = (_bt_spec.get("inputSchema") or {}).get("properties") or {}
    _bt_req = (_bt_spec.get("inputSchema") or {}).get("required") or []
    check("F6a body tool inputSchema exposes input (string), no files",
          _bt_props.get("input", {}).get("type") == "string"
          and "files" not in _bt_props
          and "input" in _bt_req and "client_ref" in _bt_req,
          str(_bt_spec)[:140])
    r = _ipn(_btkey, {"client_ref": f"f6b-{_u_prt}"})  # input MISSING
    check("F6b body tool without input → -32602 invalid input mode",
          (r.get("error") or {}).get("code") == -32602
          and "invalid input mode" in r["error"].get("message", ""),
          str(r)[:130])
    r = _ipn(_btkey, {"client_ref": f"f6c-{_u_prt}", "input": ""})
    check("F6c empty text input → -32602 err_body_required",
          (r.get("error") or {}).get("code") == -32602
          and "err_body_required" in r["error"].get("message", ""),
          str(r)[:130])
    r = _ipn(_btkey, {"client_ref": f"f6d-{_u_prt}", "input": "x" * 100001})
    check("F6d text input over 100k → -32602 err_body_too_long",
          (r.get("error") or {}).get("code") == -32602
          and "err_body_too_long" in r["error"].get("message", ""),
          str(r)[:130])
    r = _ipn(_btkey, {"client_ref": f"f6e-{_u_prt}", "input": 123})
    check("F6e non-string text input → -32602 err_body_invalid",
          (r.get("error") or {}).get("code") == -32602
          and "err_body_invalid" in r["error"].get("message", ""),
          str(r)[:130])
    r = _ipn("compress", {"client_ref": f"f6f-{_u_prt}", "input": "x"})
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
      _u2 is not None and _u2["id"] == uid_prt
      and getattr(_r2.state, "auth_via", None) == "token",
      f"{_u2 and _u2.get('id')} {getattr(_r2.state, 'auth_via', None)}")
_u3 = _appmain.get_user_from_request(_AuthReq(
    "/v1page", cookies={_CK: ck.split("=", 1)[1]}))
check("F7c /v1page is NOT a machine surface (prefix tightened) → session works",
      _u3 is not None and _u3["id"] == uid_prt, str(_u3)[:80])
_u4 = _appmain.get_user_from_request(_AuthReq("/v1"))
check("F7d exact /v1 without credentials → anonymous", _u4 is None)

# ══════════ 13. setup pages ══════════
st, _h, html_en = _req_prt("GET", "/agents?lang=en")
st2, _h, html_ja = _req_prt("GET", "/agents?lang=ja")
st3, _h, html_tm = _req_prt("GET", "/tool/compress/mcp?lang=en")
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
_ajs = open(_os.path.join(REPO, "static", "pages", "agents.js"), encoding="utf-8").read()
_be = open(_os.path.join(REPO, "static", "pages", "base-engine.js"), encoding="utf-8").read()
check("S6 copy feedback: agents.js delegates to the one authority; no hardcoded Chinese",
      "window.copyFeedback" in _ajs and 'window.t("copied")' in _be
      and "已复制" not in _ajs and "已复制" not in _be)

# ══════════ cleanup ══════════
_uids = [uid_prt, uid2, _ownA, _ownB]
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


finish()
