#!/usr/bin/env python3
"""the coherence pass coherence fixes — behavior tests over the real call paths.

  X1*   P0-1: http tool with files succeeds via BOTH surfaces (MCP
        tools/call and web /api/uploads + /api/tools/execute); source
        field is `url`; billing exactly once per logical call.
  X2/X3 P1: files_min 0 survives read-back and PATCH; a half window
        PATCH keeps the other bound; min>max rejected.
  X4    P2-4: a later item failing staging deletes every object staged
        for the earlier items (zero residue, typed refund).
  X5    P2-5: ticket re-issue after failure/expiry succeeds; a replay
        after success returns the same handles.
  X6    P2-6: create_upload returns structuredContent matching its
        outputSchema.
  X7    P3-7: content-sniff and accept mismatch share ONE expression
        (isError tool result with the typed English sentence).
  X11   P3-11: result_public_id uniqueness; ticket housekeeping purge.
"""
import base64
import http.server
import json as _json
import os as _os
import secrets as _secrets
import sys as _sys
import threading
import time
import uuid

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


import http.client as _hc
import urllib.parse

_HOST = ("127.0.0.1", int(_TEST_PORT))
_BASE = f"http://127.0.0.1:{_TEST_PORT}"
_u = "fx1" + uuid.uuid4().hex[:8]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mNk"
    "+M+QzGdlGU0AAAvKBwGAWjR9awAAAABJRU5ErkJggg==")


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
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


# ── in-suite http tool endpoint: single-shape items contract ──
class _ToolHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = _json.loads(self.rfile.read(n) or b"{}")
        files = body.get("files") or []
        items = [{"kind": "file", "filename": f.get("filename") or "out.png",
                  "content_type": "image/png",
                  "data_b64": base64.b64encode(PNG).decode()}
                 for f in files] or [{"kind": "text", "text": "no files"}]
        out = _json.dumps({"items": items}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
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

ts = now()
uid = None
TOK = None
OP_TOOL = f"fxhttp{_u}"
USER_TOOL = f"fxuser{_u}"
created_uids = []

try:
    # ── fixtures: operator http tool (cost 2) + a plain user ──
    from web_auth import create_browser_session
    from config import COOKIE_NAME
    op = "fxop" + _u
    with db() as con:
        op_uid = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s,%s,'x','FX',"
            "'samelabs',%s,%s) returning id",
            (op, f"{op}@t.co", ts, ts)).fetchone()["id"]
        created_uids.append(op_uid)
    ck = f"{COOKIE_NAME}={create_browser_session(op_uid)}"
    st, _h, d, _sc = _req("POST", "/samelabs/tools/create", {
        "tool_id": OP_TOOL, "label": "FX Http Echo",
        "url": TOOL_URL, "timeout_s": 30,
        "files_min": 1, "files_max": 5, "outputs": ["file"],
        "cost": 2}, {"Content-Type": "application/json", "Cookie": ck})
    check("F1 operator http tool created", st == 200 and (d or {}).get("ok"),
          f"{st} {str(d)[:80]}")

    b, ct = _form({"username": _u + "a", "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{_u}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u + "a", "password": "Test12345"})
    st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    CK = sc.split(";")[0]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
        created_uids.append(uid)
        from repo.ledger import insert_row
        insert_row(con, uid, 10000, 10000, "manual_topup", None, None,
                   f"fx1:{_u}", None, ts)
    st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "fx1"},
                            {"Content-Type": "application/json", "Cookie": CK})
    TOK = d_k["key"]["key"]
    check("F2 user + key + credits", bool(TOK), TOK)

    # ═══ X1: http tool with files — MCP surface ═══
    A64 = base64.b64encode(PNG).decode()
    st, d = _call(OP_TOOL, {"files": [
        {"filename": "in-a.png", "b64": A64},
        {"filename": "in-b.png", "b64": A64}],
        "client_ref": f"x1-mcp-{_u}"}, TOK, 10)
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
    st, d = _call(OP_TOOL, {"files": [
        {"filename": "in-a.png", "b64": A64},
        {"filename": "in-b.png", "b64": A64}],
        "client_ref": f"x1-mcp-{_u}"}, TOK, 11)
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
         f'x1-web-up-{_u}\r\n').encode()
    for i in range(2):
        b += (f'--B\r\nContent-Disposition: form-data; name="file"; '
              f'filename="w{i}.png"\r\nContent-Type: image/png\r\n\r\n'
              ).encode() + PNG + b"\r\n"
    b += b"--B--\r\n"
    ct = "multipart/form-data; boundary=B"
    st, _h, d_up, _sc = _req("POST", "/api/uploads", b,
                             {"Content-Type": ct, "Cookie": CK})
    _staged = (d_up or {}).get("files") or [(d_up or {}).get("file")]
    staged_ids = [f.get("stagingId") for f in _staged if f]
    check("X1e web staging returns url-contract handles",
          st == 200 and len(staged_ids) == 2 and all(staged_ids), f"{st} {str(d_up)[:80]}")
    st, _h, d_x, _sc = _req("POST", "/api/tools/execute", {
        "tool_id": OP_TOOL,
        "source": {"files": [{"public_id": _staged[0]["stagingId"],
                              "source_token": _staged[0].get("sourceToken", "")},
                             {"public_id": _staged[1]["stagingId"],
                              "source_token": _staged[1].get("sourceToken", "")}]},
        "params": {},
        "delivery": {"expiry_days": 1, "allowed_downloads": 50},
        "client_ref": f"x1-web-{_u}"},
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
    create_tool({
        "tool_key": f"{_u}a/{USER_TOOL}", "tool_id": USER_TOOL,
        "owner_id": uid, "execution": "http", "tool_type": "produce",
        "label": {"en": "FX User Tool"}, "icon": "wand",
        "inputs": {"files": {"min": 0, "max": 3, "accept": None}},
        "outputs": ["text"],
        "params": [{"key": "note", "type": "text", "required": True,
                    "label": {"en": "Note"}}],
        "binding": {"url": TOOL_URL, "timeout_s": 30},
        "visibility": "private", "enabled": True})
    with db() as con:
        con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                    " values (%s, 1, TRUE, %s)",
                    (f"{_u}a/{USER_TOOL}", ts))
    st, _h, d_list, _sc = _req("GET", "/api/usertools", {}, {"Cookie": CK})
    entry = next((t for t in (d_list or {}).get("tools", [])
                  if t.get("tool_id") == USER_TOOL), None)
    check("X2 files_min 0 survives read-back (not coerced to 1)",
          entry and entry.get("files_min") == 0 and entry.get("files_max") == 3,
          str(entry and {k: entry.get(k) for k in ('files_min', 'files_max')}))
    st, _h, d_p, _sc = _req("PATCH", f"/api/usertools/{USER_TOOL}",
                            {"label": {"en": "FX v2"}},
                            {"Content-Type": "application/json", "Cookie": CK})
    st, _h, d_list, _sc = _req("GET", "/api/usertools", {}, {"Cookie": CK})
    entry = next((t for t in (d_list or {}).get("tools", [])
                  if t.get("tool_id") == USER_TOOL), None)
    check("X3a PATCH without window keeps stored bounds (0/3)",
          st == 200 and entry and entry.get("files_min") == 0
          and entry.get("files_max") == 3,
          str(entry and {k: entry.get(k) for k in ('files_min', 'files_max')}))
    st, _h, d_p, _sc = _req("PATCH", f"/api/usertools/{USER_TOOL}",
                            {"files_min": 2},
                            {"Content-Type": "application/json", "Cookie": CK})
    st, _h, d_list, _sc = _req("GET", "/api/usertools", {}, {"Cookie": CK})
    entry = next((t for t in (d_list or {}).get("tools", [])
                  if t.get("tool_id") == USER_TOOL), None)
    check("X3b PATCH files_min only keeps stored max (2/3)",
          st == 200 and entry and entry.get("files_min") == 2
          and entry.get("files_max") == 3,
          str(entry and {k: entry.get(k) for k in ('files_min', 'files_max')}))
    st, _h, d_p, _sc = _req("PATCH", f"/api/usertools/{USER_TOOL}",
                            {"files_max": 5},
                            {"Content-Type": "application/json", "Cookie": CK})
    st, _h, d_list, _sc = _req("GET", "/api/usertools", {}, {"Cookie": CK})
    entry = next((t for t in (d_list or {}).get("tools", [])
                  if t.get("tool_id") == USER_TOOL), None)
    check("X3c PATCH files_max only keeps stored min (2/5)",
          st == 200 and entry and entry.get("files_min") == 2
          and entry.get("files_max") == 5,
          str(entry and {k: entry.get(k) for k in ('files_min', 'files_max')}))
    st, _h, d_p, _sc = _req("PATCH", f"/api/usertools/{USER_TOOL}",
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
            {"kind": "file", "data": PNG, "filename": "a.png",
             "content_type": "image/png", "size": len(PNG)},
            {"kind": "file", "data": PNG, "filename": "b.png",
             "content_type": "image/png", "size": len(PNG)},
        ]}
    _tools.register(
        tool_id=f"fxlocal{_u}", tool_type="produce",
        label={"en": "FX Local Two"}, icon="wand",
        handler=_two_files_handler,
        files_min=0, files_max=1, outputs=("file",))
    with db() as con:
        con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                    " values (%s, 0, TRUE, %s)", (f"fxlocal{_u}", ts))

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

    _tool_row = {"id": f"fxlocal{_u}", "tool_key": f"fxlocal{_u}",
                 "type": "produce", "owner_id": None,
                 "execution": "local", "files_max": 1}
    _user_row = {"id": uid, "username": _u + "a"}

    class _Inv:
        tool_id = f"fxlocal{_u}"
        files = []
        params = {}
        delivery = {"expiry_days": 1, "allowed_downloads": 5}
        client_ref = f"x4-{_u}"

    def _ptool(tool_id, sources, params):
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
            (f"x4-{_u}", uid)).fetchone()
    check("X4c call settled failed (refund path)",
          row is not None and row["status"] == "failed", str(row))

    # ═══ X5: ticket re-issue + replay idempotency (real HTTP) ═══
    def _create_upload(ref, fname="t.png", size=None):
        return _call("create_upload",
                     {"filename": fname, "size": size or len(PNG),
                      "client_ref": ref}, TOK, _secrets.randbelow(10**6))
    REF = f"tk-{_u}"
    st, d = _create_upload(REF)
    _u1 = (((d.get("result") or {}).get("content") or [{}])[0].get("text") or "")
    t1 = _json.loads(_u1)["upload_url"].rsplit("/", 1)[-1]
    # burn the ticket with a WRONG body (declared size exceeded → 413)
    st_put, _h, d_put, _sc = _req(
        "PUT", f"/mcp/upload/{t1}", PNG + b"x" * 10, {})
    check("X5a bad PUT rejected before any receipt",
          st_put == 413, f"{st_put} {str(d_put)[:60]}")
    # re-issue with the SAME ref and correct bytes → success
    st, d = _create_upload(REF)
    _u2 = _json.loads((((d.get("result") or {}).get("content") or [{}])[0]
                       .get("text") or ""))["upload_url"]
    t2 = _u2.rsplit("/", 1)[-1]
    check("X5b same-ref re-issue after failed ticket is a NEW ticket",
          t2 != t1, (t1, t2))
    st_put, _h, d_ok, _sc = _req("PUT", f"/mcp/upload/{t2}", PNG, {})
    check("X5c retried PUT with same ref succeeds (no 409)",
          st_put == 200 and (d_ok or {}).get("public_id"),
          f"{st_put} {str(d_ok)[:80]}")
    first_pid = (d_ok or {}).get("public_id")
    first_tok = (d_ok or {}).get("source_token")
    # success + same ref again → replay returns the SAME handles
    st, d = _create_upload(REF)
    _u3 = _json.loads((((d.get("result") or {}).get("content") or [{}])[0]
                       .get("text") or ""))["upload_url"]
    t3 = _u3.rsplit("/", 1)[-1]
    st_put, _h, d_rp, _sc = _req("PUT", f"/mcp/upload/{t3}", PNG, {})
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
    TXT_AS_PNG = b"this is plain text, not a png"
    st, d = _call(OP_TOOL, {"files": [
        {"filename": "fake.png", "b64": base64.b64encode(TXT_AS_PNG).decode()}],
        "client_ref": f"x7a-{_u}"}, TOK, 20)
    r7a = d.get("result") or {}
    t7a = ((r7a.get("content") or [{}])[0].get("text") or "")
    check("X7a content-sniff mismatch → isError (not -32602)",
          r7a.get("isError") is True and "err_format_not_supported" in t7a
          and "not supported" in t7a, t7a[:110])
    st, d = _call("compress", {"files": [
        {"filename": "a.pdf", "b64": base64.b64encode(b"%PDF-fake").decode()}],
        "client_ref": f"x7b-{_u}"}, TOK, 21)
    r7b = d.get("result") or {}
    t7b = ((r7b.get("content") or [{}])[0].get("text") or "")
    check("X7b accept mismatch → isError with the same sentence shape",
          r7b.get("isError") is True and "err_format_not_supported" in t7b
          and "not supported" in t7b, t7b[:110])
    check("X7c both expressions identical shape (isError, code present)",
          (r7a.get("isError") is True) == (r7b.get("isError") is True)
          and "err_format_not_supported" in t7a and "err_format_not_supported" in t7b)

    # ═══ X11: index uniqueness + ticket purge ═══
    with db() as con:
        con.execute(
            "insert into tool_calls (client_ref, user_id, tool_id, status,"
            " cost_snapshot, started_at, finished_at, invocation_hash,"
            " result_public_id, result_json) values (%s,%s,'share','ok',0,"
            "%s,%s,%s,'fx1dup" + _u + "', '{}')",
            (f"i1-{_u}", uid, ts, ts, "a" * 64))
        try:
            con.execute(
                "insert into tool_calls (client_ref, user_id, tool_id, status,"
                " cost_snapshot, started_at, finished_at, invocation_hash,"
                " result_public_id, result_json) values (%s,%s,'share','ok',0,"
                "%s,%s,%s,'fx1dup" + _u + "', '{}')",
                (f"i2-{_u}", uid, ts, ts, "b" * 64))
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
            (__import__("hashlib").sha256(("fxexp" + _u).encode()).hexdigest(),
             uid, f"purge-exp-{_u}", ts - 100, ts - 200,
             __import__("hashlib").sha256(("fxused" + _u).encode()).hexdigest(),
             uid, f"purge-used-{_u}", ts + 600,
             ts - 25 * 3600, ts - 26 * 3600,
             __import__("hashlib").sha256(("fxlive" + _u).encode()).hexdigest(),
             uid, f"purge-live-{_u}", ts + 600, ts))
    n = _tickets.purge(ts)
    with db() as con:
        left = [r["ticket_hash"] for r in con.execute(
            "select ticket_hash from mcp_upload_tickets where client_ref like %s",
            ("purge-%" + _u,)).fetchall()]
    check("X11b ticket purge drops expired-unused + >24h-used, keeps live",
          n >= 2 and left == [__import__("hashlib").sha256(("fxlive" + _u).encode()).hexdigest()], str(left))

    # ═══ B-item tests (the coherence passb) ═══
    # B2: MCP inputSchema required hierarchy (+ jsonschema validation)
    st, d = _mcp({"jsonrpc": "2.0", "id": 30, "method": "tools/list",
                  "params": {}}, TOK)
    _tools_l = {t["name"]: t for t in (d.get("result") or {}).get("tools", [])}
    # MCP name projection: '/' → '__' (governance.catalog.mcp_names)
    from governance.catalog import mcp_name as _mname
    _spec = _tools_l.get(_mname(f"{_u}a/{USER_TOOL}")) or {}
    _schema = _spec.get("inputSchema") or {}
    # the fixture tool is 0..3 files → files optional; only params is
    # required at top level. Schema validity is driven per-tool.
    _ok_arg = {"params": {"note": "hi"}, "client_ref": f"b2v-{_u}"}
    _bad_arg = {"params": {}, "client_ref": f"b2v-{_u}"}
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
        from governance.catalog import mcp_names as _mname2
        _comp = _tools_l.get("compress") or {}
        _comp_schema = _comp.get("inputSchema") or {}
        _comp_ok = {"files": [{"filename": "a.png", "b64": A64}],
                    "params": {}, "client_ref": f"b2v2-{_u}"}
        _comp_bad = {"params": {}, "client_ref": f"b2v2-{_u}"}
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
    REF3 = f"b3-{_u}"

    def _one_issue(_):
        st, d = _call("create_upload",
                      {"filename": "c.png", "size": len(PNG),
                       "client_ref": REF3}, TOK, 400 + _)
        try:
            sc = (d.get("result") or {}).get("structuredContent") or {}
            _txt = _json.loads((((d.get("result") or {}).get("content") or [{}])[0]
                                .get("text") or "{}"))
            return sc.get("upload_url") or _txt.get("upload_url")
        except Exception:
            return None
    with _cf.ThreadPoolExecutor(max_workers=8) as _ex:
        _urls = list(_ex.map(_one_issue, range(8)))
    with db() as con:
        _live = con.execute(
            "select count(*) c from mcp_upload_tickets"
            " where user_id=%s and client_ref=%s and used_at is null"
            " and expires_at > %s", (uid, REF3, ts)).fetchone()["c"]
    check("B3 concurrent create_upload → exactly one live ticket",
          _live == 1, f"live={_live}")
    check("B3b all 8 responses return a usable upload_url (one live ticket)",
          all(u for u in _urls), str(_urls)[:100])

    # B4: a hung internal Gokapi read must not pin the event loop —
    # while one inline preview is held, unrelated endpoints answer fast.
    st, d = _call("share", {"files": [{"filename": "hold.png", "b64": A64}],
                            "client_ref": f"b4-{_u}"}, TOK, 31)
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
                c = _sock.create_connection(_HOST, timeout=15)
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
        cwd=_REPO, env=_env2,
        stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)
    try:
        import time as _time2
        _ok_up = False
        for _ in range(40):
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
            for k, v in {"username": f"rl{_i}{_u}", "password": "Test12345",
                         "password_confirm": "Test12345",
                         "email": f"rl{_i}{_u}@t.co"}.items():
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
                ("rl%" + _u + "%",)).fetchall()])

finally:
    with db() as con:
        con.execute("delete from operator_audit where actor_user_id = ANY(%s)",
                    (created_uids,))
        for t in created_uids:
            purge_users(con, [t])
        con.execute("delete from platform_tools where tool_key like %s",
                    (f"%{_u}%",))
        con.execute("delete from mcp_upload_tickets where client_ref like %s",
                    ("%" + _u + "%",))
        con.execute("delete from tool_calls where client_ref like %s",
                    (f"%{_u}%",))
    _srv.shutdown()
    print("cleanup done")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — the coherence pass coherence ({COUNT} checks)")
