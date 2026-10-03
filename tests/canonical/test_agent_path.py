#!/usr/bin/env python3
"""Agent-path canonical — the MCP surface an agent actually walks.

  A*  Auth failure is self-describing: no/invalid API key → 401 with
      JSON body {error: unauthorized, message pointing at /account/keys}
      and the WWW-Authenticate header kept.
  B*  create_upload refuses unsupported extensions BEFORE issuing a
      ticket (same single-point word lists the PUT runs): isError with
      the PUT's code and zero ticket rows.
  C*  Agent-delivered text formats: a .log shares fine; a .log carrying
      a NUL byte is rejected (text sniff).
  D*  isError results are ONE text block whose text parses as JSON with
      error + message — for platform-level format refusal and for
      tool-accept mismatch alike.
  E*  Image echo suppression: share of a small PNG emits NO image block
      (bytes identical to this call's input); compress of the same PNG
      still does (bytes changed).
  F*  Discovery mentions the human-link path: initialize instructions
      and the share description both carry result_url; /llms.txt too.
"""
import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid

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

_REPO = _os.path.abspath(_os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
_sys.path.insert(0, _REPO)
_sys.path.insert(0, _os.path.join(_REPO, "core"))
_sys.path.insert(0, _os.path.join(_REPO, "app"))
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
from tests.support.cleanup import purge_users  # noqa: E402

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
_u = "ap" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n%%EOF\n"


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


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp(body, token):
    st, _h, d, _sc = _req("POST", "/mcp", body, {
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
    """content[0].text parsed as JSON (the unified isError shape)."""
    try:
        return _json.loads(
            (((d or {}).get("result") or {}).get("content")
             or [{}])[0].get("text") or "{}")
    except Exception:
        return {}


from database import db

uid = None
try:
    # ═══ A. no key = anonymous discovery; a bad key = self-describing 401
    st, h, d = (lambda r: (r[0], r[1], r[2]))(_req(
        "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"Content-Type": "application/json"}))
    check("A0 no key → ping answers (anonymous discovery, no 401)",
          st == 200 and isinstance(d, dict) and d.get("result") == {},
          f"{st} {d}")
    st, h, d = (lambda r: (r[0], r[1], r[2]))(_req(
        "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"Content-Type": "application/json",
         "Authorization": "Bearer fsk_not_a_real_key"}))
    check("A1 invalid key → 401 with JSON body",
          st == 401 and isinstance(d, dict), f"{st} {d}")
    check("A2 body error=unauthorized, message points at register + /account/keys",
          isinstance(d, dict) and d.get("error") == "unauthorized"
          and "register" in str(d.get("message", ""))
          and "/account/keys" in str(d.get("message", "")), d)
    check("A3 WWW-Authenticate kept",
          h.get("www-authenticate") == 'Bearer realm="518"',
          h.get("www-authenticate"))
    st, _h, d = (lambda r: (r[0], r[1], r[2]))(_req(
        "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"Content-Type": "application/json",
         "Authorization": "Bearer fsk_wrongkey"}))
    check("A4 wrong key → same shape", st == 401
          and isinstance(d, dict) and d.get("error") == "unauthorized", d)

    # user + key + credits for the rest
    b, ct = _form({"username": _u + "a", "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{_u}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u + "a", "password": "Test12345"})
    st_l, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "ap"},
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
    st, _h, body = (lambda r: (r[0], r[1], r[2]))(_req("GET", "/llms.txt"))
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

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
