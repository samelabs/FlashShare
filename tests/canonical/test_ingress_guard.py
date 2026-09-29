#!/usr/bin/env python3
"""Ingress guard canonical — actionable content/extension mismatch
reports (A), the MCP tools/call entry rate limit (B) and local
execution-pool backpressure (C).

  A*  detect_ext is the single magic-number authority (fixed signatures
      + ISO-BMFF brands); every ingress surface that rejects on sniff
      returns the SAME structure: the error code verbatim, the
      sanitized filename, the detected real format (or null) and a
      stem-based rename suggestion (or null). Batch uploads are still
      rejected as a whole, naming the offending file.
  B*  Every tools/call counts against ONE per-user sliding window
      (60 calls / 60 s) — meta tools and catalog search included;
      the 61st call inside the window answers isError err_rate_limited.
  C*  The local execution pool caps in-flight invocations at
      4 × workers; a full pool refuses BEFORE the claim (err_busy, no
      tool_calls row, no charge) and every run() exit path releases
      its slot (in-flight back to 0).

Fail-closed env guard; per-run unique names; fixtures cleaned at end.
"""
import asyncio
import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:170]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
_BASE = f"http://127.0.0.1:{_TEST_PORT}"
_u = "ig" + _uuid.uuid4().hex[:6]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
# minimal ISO-BMFF headers: 4-byte box size, ftyp, 4-byte major brand
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40
AVIF = b"\x00\x00\x00\x18ftypavif" + b"\x00" * 40
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 40
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
GARBAGE = b"NOTAFILE" + b"\x7f" * 24


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


def _multipart(files, fields):
    """files: list of (filename, content_type, bytes)."""
    out = b""
    for fn, ct, blob in files:
        out += (f'--B\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n').encode()
        out += blob + b"\r\n"
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


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "ig"},
                             {"Content-Type": "application/json", "Cookie": ck})
    from database import db
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


def _mismatch_ok(payload, code, filename, detected, suggested):
    return (payload.get("error") == code
            and payload.get("filename") == filename
            and payload.get("detected") == detected
            and payload.get("suggested_name") == suggested)


uids = []
ck_a = tok_a = tok_b = None
try:
    # ═══ A0. detect_ext unit (single magic-number authority) ═══
    from security import detect_ext
    for label, head, want in (
            ("PNG", PNG, ".png"), ("JPEG", b"\xff\xd8\xe0", ".jpg"),
            ("PDF", b"%PDF-1.7", ".pdf"), ("ZIP", b"PK\x03\x04xx", ".zip"),
            ("HEIC brand", HEIC[:12], ".heic"),
            ("mif1 brand", b"\x00\x00\x00\x18ftypmif1", ".heic"),
            ("AVIF brand", AVIF[:12], ".avif"),
            ("qt brand", MOV[:12], ".mov"),
            ("isom brand", MP4[:12], ".mp4"),
            ("M4V brand", b"\x00\x00\x00\x18ftypM4V ", ".mp4"),
            ("WAV in RIFF", b"RIFFxxxxWAVE", ".wav"),
            ("unknown", GARBAGE, None),
            ("short head", b"\x00\x00\x00", None)):
        check(f"A0 detect_ext {label} → {want}",
              detect_ext(head) == want, detect_ext(head))

    uid_a, ck_a, tok_a = _register(_u + "a")
    uid_b, _ck_b, tok_b = _register(_u + "b")
    uid_c, _ck_c, _tok_c = _register(_u + "c")
    uids = [uid_a, uid_b, uid_c]
    check("F1 three fixtures registered", all(uids), uids)

    # ═══ A1. web /api/uploads: a.jpg carrying HEIC magic ═══
    body, ct = _multipart([("a.jpg", "image/jpeg", HEIC)],
                          {"tool": "share", "client_ref": f"ig-a1-{_u}"})
    st, _h, d, _sc = _req("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    check("A1 web 400 with full mismatch structure",
          st == 400 and isinstance(d, dict) and d.get("ok") is False
          and _mismatch_ok(d, "err_content_mismatch_image", "a.jpg",
                           ".heic", "a.heic"), (st, d))

    # ═══ A2. web: unrecognizable magic → detected/suggested null ═══
    body, ct = _multipart([("a.jpg", "image/jpeg", GARBAGE)],
                          {"tool": "share", "client_ref": f"ig-a2-{_u}"})
    st, _h, d, _sc = _req("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    check("A2 web unknown magic → detected null, no suggestion",
          st == 400 and _mismatch_ok(d, "err_content_mismatch_image",
                                     "a.jpg", None, None), (st, d))

    # ═══ A3. web: batch rejected as a whole, offender named ═══
    body, ct = _multipart([("good.png", "image/png", PNG),
                          ("b.jpg", "image/jpeg", HEIC)],
                          {"tool": "share", "client_ref": f"ig-a3-{_u}"})
    st, _h, d, _sc = _req("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    check("A3 web batch rejection names the offending file",
          st == 400 and _mismatch_ok(d, "err_content_mismatch_image",
                                     "b.jpg", ".heic", "b.heic"), (st, d))

    # ═══ A4. web: unknown extension keeps its code in the same shape ═══
    body, ct = _multipart([("x.bin", "application/octet-stream", GARBAGE)],
                          {"tool": "share", "client_ref": f"ig-a4-{_u}"})
    st, _h, d, _sc = _req("POST", "/api/uploads", body,
                          {"Content-Type": ct, "Cookie": ck_a})
    check("A4 web unknown ext → err_format_not_supported, detected null",
          st == 400 and _mismatch_ok(d, "err_format_not_supported",
                                     "x.bin", None, None), (st, d))

    # ═══ A5. MCP ticket PUT: a.jpg carrying HEIC magic ═══
    # two tickets: each consumed PUT is one-shot even on sniff rejection
    st, d = _call("create_upload", {"filename": "a.jpg", "size": len(HEIC),
                                    "client_ref": f"ig-a5b-{_u}"}, tok_a)
    ticket = (((d or {}).get("result") or {}).get("structuredContent") or {})
    tp = ticket.get("upload_url", "")
    check("A5a create_upload issues the ticket", tp.startswith(_BASE), ticket)
    st_p, _h, d_p, _sc = _req("PUT", tp[len(_BASE):], HEIC)
    check("A5b PUT 415 with full mismatch structure",
          st_p == 415 and isinstance(d_p, dict) and d_p.get("ok") is False
          and _mismatch_ok(d_p, "err_content_mismatch_image", "a.jpg",
                           ".heic", "a.heic"), (st_p, d_p))
    st, d = _call("create_upload", {"filename": "a.jpg", "size": len(GARBAGE),
                                    "client_ref": f"ig-a5c-{_u}"}, tok_a)
    ticket = (((d or {}).get("result") or {}).get("structuredContent") or {})
    tp = ticket.get("upload_url", "")
    st_p, _h, d_p, _sc = _req("PUT", tp[len(_BASE):], GARBAGE)
    check("A5c PUT unknown magic → detected null (415)",
          st_p == 415 and _mismatch_ok(d_p, "err_content_mismatch_image",
                                       "a.jpg", None, None), (st_p, d_p))

    # ═══ A6. MCP tools/call inline source ═══
    st, d = _call("share", {"files": [{"filename": "a.jpg", "b64": _b64(HEIC)}],
                            "client_ref": f"ig-a6-{_u}"}, tok_a)
    _t = _tool_text(d)
    check("A6 MCP inline isError with full mismatch structure",
          (d.get("result") or {}).get("isError") is True
          and _mismatch_ok(_t, "err_content_mismatch_image", "a.jpg",
                           ".heic", "a.heic")
          and "Rename the file to a.heic" in _t.get("message", ""), _t)
    st, d = _call("share", {"files": [{"filename": "a.jpg",
                                       "b64": _b64(GARBAGE)}],
                            "client_ref": f"ig-a6b-{_u}"}, tok_a)
    _t = _tool_text(d)
    check("A6b MCP inline unknown magic → detected null, no rename hint",
          (d.get("result") or {}).get("isError") is True
          and _mismatch_ok(_t, "err_content_mismatch_image", "a.jpg",
                           None, None)
          and "Rename the file" not in _t.get("message", ""), _t)

    # ═══ A7. MCP URL source (in-process, pinned transport replaced) ═══
    import remote as _remote
    from app.routes import mcp as _M

    class _Req:
        class client:
            host = "127.0.0.1"
        headers = {}

    _orig_pinned = _remote.pinned_request
    _remote.pinned_request = lambda url, **k: (200, HEIC)
    try:
        r = asyncio.run(_M._call_tool(_Req(), {"id": uid_a}, 1, {
            "name": "share",
            "arguments": {"files": [{"url": f"https://files.example/{_u}/a.jpg"}],
                          "client_ref": f"ig-a7-{_u}"}}))
        _t = _tool_text(r)
        check("A7 MCP url source isError with full mismatch structure",
              (r.get("result") or {}).get("isError") is True
              and _mismatch_ok(_t, "err_content_mismatch_image", "a.jpg",
                               ".heic", "a.heic"), _t)
    finally:
        _remote.pinned_request = _orig_pinned
    fn_noext = _M._url_upload("https://files.example/plain", None, PNG)[0]
    check("A7b extension-less URL filename gets the detected ext",
          fn_noext == "plain.png", fn_noext)

    # ═══ B. MCP tools/call entry rate limit (fresh user, fresh window) ═══
    from economy.antibuse import mcp_call_throttled, MCP_CALL_LIMIT
    _probe = uid_b + 10 ** 9
    for _i in range(MCP_CALL_LIMIT):
        if mcp_call_throttled(_probe):
            break
    check("B0 unit: limiter admits exactly the window then refuses",
          _i == MCP_CALL_LIMIT - 1 and mcp_call_throttled(_probe) is True, _i)

    seen_ok = 0
    for i in range(MCP_CALL_LIMIT):
        # meta-tool branch and catalog-search branch both count: mix them
        name, args = (("list_uploads", {"offset": 0}) if i % 2 == 0
                      else ("catalog_search", {"q": "compress"}))
        _st, d = _call(name, args, tok_b, mid=i + 1)
        if not ((d or {}).get("error") or {}):
            seen_ok += 1
    check("B1 the first 60 mixed tools/call in the window succeed",
          seen_ok == MCP_CALL_LIMIT, seen_ok)
    _st, d = _call("list_uploads", {"offset": 0}, tok_b, mid=501)
    _t = _tool_text(d)
    check("B2 61st tools/call within 60s → isError err_rate_limited",
          (d.get("result") or {}).get("isError") is True
          and _t.get("error") == "err_rate_limited", str(d)[:130])
    _st, d = _call("catalog_search", {"q": "x"}, tok_b, mid=502)
    _t = _tool_text(d)
    check("B3 every branch is covered (search throttled too)",
          (d.get("result") or {}).get("isError") is True
          and _t.get("error") == "err_rate_limited", str(d)[:130])
    _st, d = _mcp({"jsonrpc": "2.0", "id": 503, "method": "tools/list"}, tok_b)
    check("B4 tools/list is NOT a tools/call (unthrottled)",
          not ((d or {}).get("error") or {})
          and len(((d or {}).get("result") or {}).get("tools", [])) > 0,
          str(d)[:110])

    # ═══ C. local execution pool backpressure (in-process, user c) ═══
    from execution.pool import (LOCAL_MAX_INFLIGHT, inflight_count,
                                release, try_acquire)
    from execution.gate import run as gate_run
    from execution.invocation import ToolInvocation
    from execution.results import StagedItems
    from execution import ExecutionError
    from governance.catalog import resolve as _resolve

    check("C0 unit: try_acquire up to the cap, then False, then recovers",
          all(try_acquire() for _ in range(LOCAL_MAX_INFLIGHT))
          and try_acquire() is False
          and (release() or True) and (release() or True)
          and inflight_count() == LOCAL_MAX_INFLIGHT - 2
          and all(release() or True for _ in range(LOCAL_MAX_INFLIGHT - 2))
          and inflight_count() == 0, inflight_count())

    tool, terr = _resolve("share", uid_c)
    tool["id"] = tool["tool_key"]
    check("C1 fixture: share resolves as a local tool",
          terr is None and tool.get("execution") == "local", (terr, tool and tool.get("execution")))

    def _inv(ref, params=None):
        return ToolInvocation("share", [], params or {},
                              {"expiry_days": 1, "allowed_downloads": 50},
                              ref)

    async def _ok_orch():
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "ok"}])

    async def _fail_orch():
        raise ExecutionError("tool_failed")

    def _calls_n(uid):
        from database import db as _db
        with _db() as con:
            return int(con.execute(
                "select count(*) c from tool_calls where user_id=%s",
                (uid,)).fetchone()["c"])

    # C2 success path
    payload, err, status = asyncio.run(
        gate_run({"id": uid_c}, tool, _inv(f"ig-c2-{_u}"), _ok_orch))
    check("C2 success path settles ok and releases the slot",
          err is None and payload.get("ok") is True
          and inflight_count() == 0, (status, err))

    # C3 replay path (same ref, same fingerprint)
    payload, err, _s = asyncio.run(
        gate_run({"id": uid_c}, tool, _inv(f"ig-c2-{_u}"), _ok_orch))
    check("C3 replay path returns the stored result, slot released",
          err is None and payload.get("replay") is True
          and inflight_count() == 0, err)

    # C4 conflict path (same ref, different fingerprint)
    _p, err, status = asyncio.run(
        gate_run({"id": uid_c}, tool,
                 _inv(f"ig-c2-{_u}", params={"x": 1}), _ok_orch))
    check("C4 conflict path 409, slot released",
          status == 409 and err.get("error") == "idempotency_conflict"
          and inflight_count() == 0, (status, err))

    # C5 tool failure path
    _p, err, status = asyncio.run(
        gate_run({"id": uid_c}, tool, _inv(f"ig-c5-{_u}"), _fail_orch))
    check("C5 ExecutionError path refunds and releases the slot",
          status == 400 and err.get("error") == "tool_failed"
          and inflight_count() == 0, (status, err))

    # C6 busy path: slots held externally → err_busy BEFORE the claim
    _before = _calls_n(uid_c)
    for _ in range(LOCAL_MAX_INFLIGHT):
        try_acquire()
    _p, err, status = asyncio.run(
        gate_run({"id": uid_c}, tool, _inv(f"ig-c6-{_u}"), _ok_orch))
    check("C6 full pool → err_busy 503, zero tool_calls rows",
          status == 503 and err.get("ok") is False
          and err.get("error") == "err_busy"
          and _calls_n(uid_c) == _before, (status, err))
    for _ in range(LOCAL_MAX_INFLIGHT):
        release()
    check("C6b external release drains the pool to 0",
          inflight_count() == 0, inflight_count())

    # C7 the MCP human-text map carries the busy sentence
    _msg = _M._error_message("err_busy")
    check("C7 err_busy English sentence mentions the same client_ref",
          "busy" in _msg and "client_ref" in _msg, _msg)
finally:
    if uids:
        from database import db
        with db() as con:
            for u in uids:
                con.execute("delete from tool_calls where user_id=%s", (u,))
                con.execute("delete from ledger where user_id=%s", (u,))
                con.execute("delete from upload_usage where user_id=%s", (u,))
                con.execute("delete from ingress_requests where user_id=%s", (u,))
                con.execute("delete from mcp_upload_tickets where user_id=%s", (u,))
            purge_users(con, uids)

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
