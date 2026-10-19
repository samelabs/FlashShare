#!/usr/bin/env python3
"""Prod-hardening canonical — behavior locks for the hardening batch.

  H1  client IP single read point: exactly one definition (app.web_auth.
      client_ip) reads X-Real-IP; auth/mcp/mcp_upload/dispatch all go
      through it; header present wins, absent/empty falls back to the
      direct peer address.
  H2  /mcp request body cap (16 MB): Content-Length over the cap is
      rejected on the header alone (413 + JSON-RPC -32600 with the
      create_upload guidance); a chunked request WITHOUT Content-Length
      is stopped by the streaming count; an inline-file call inside the
      cap executes normally; initialize + llms.txt state the cap.
  H3  stored HTTP-tool credential clear: PATCH {auth_header:"",auth_token:""}
      removes the pair from the binding (remote calls stop sending the
      header); one-sided credential PATCH → 422 bad_auth_header; absent
      keys keep the stored credential.
  H4  password change/set concurrency: compare-and-set writes — two
      concurrent changes from the same old password leave exactly one
      winner (typed err_old_password_wrong for the loser); two
      concurrent first sets leave exactly one winner
      (err_password_already_set); a failed change never clears sessions.

Fail-closed env guard; per-run unique names; fixtures cleaned at end.
"""
import base64 as _b64
import http.client as _hc
import http.server as _hs
import json as _json
import os as _os
import sys as _sys
import threading as _th
import time as _time
import urllib.parse as _urlparse

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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:150]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
_u = "ph" + _os.urandom(4).hex()
ts = int(_time.time())
PNG = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=120)
    payload = body if isinstance(body, (bytes, type(None))) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, data.decode(errors="replace"), setc


def _form(fields):
    return _urlparse.urlencode(fields).encode(), "application/x-www-form-urlencoded"


def _mcp(body, token):
    st, d, _sc = _req("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


from database import db  # noqa: E402

# ══════════ H1. client IP single read point ══════════
_py_files = []
for _root, _dirs, _files in _os.walk(_REPO):
    if "__pycache__" in _root or "/tests/" in _root.replace("\\", "/"):
        continue
    if _root.startswith(_os.path.join(_REPO, "app")) or _root.startswith(_os.path.join(_REPO, "core")):
        _py_files += [_os.path.join(_root, f) for f in _files if f.endswith(".py")]
# the READ expression, not the word: docstrings mention the header too
_READ = 'headers.get("X-Real-IP"'
_xrip = {p: open(p, encoding="utf-8").read().count(_READ)
         for p in _py_files}
_xrip = {p: n for p, n in _xrip.items() if n}
check("H1a X-Real-IP read in exactly one module (app/web_auth.py)",
      list(_xrip) == [_os.path.join(_REPO, "app", "web_auth.py")] and
      list(_xrip.values()) == [1], _xrip)

from app.web_auth import client_ip  # noqa: E402
_consumers = ["app/routes/auth_route.py", "app/routes/mcp.py",
              "app/routes/mcp_upload.py", "core/execution/dispatch.py"]
for _mod in _consumers:
    _src = open(_os.path.join(_REPO, _mod), encoding="utf-8").read()
    check(f"H1b {_mod} routes through client_ip (no own X-Real-IP read)",
          "client_ip" in _src and "X-Real-IP" not in _src)
import execution.dispatch as _dispatch  # noqa: E402
check("H1c dispatch re-exports the single client_ip for its callers",
      _dispatch.client_ip is client_ip)

from starlette.requests import Request as _StarRequest  # noqa: E402


def _mkreq(headers=None, host="203.0.113.7"):
    scope = {"type": "http", "http_version": "1.1", "method": "POST",
             "path": "/mcp", "query_string": b"",
             "headers": [(k.lower().encode(), v.encode())
                         for k, v in (headers or {}).items()],
             "client": (host, 51000)}
    return _StarRequest(scope)


check("H1d X-Real-IP present → header value",
      client_ip(_mkreq({"X-Real-IP": "198.51.100.7"})) == "198.51.100.7")
check("H1e X-Real-IP absent → direct peer address",
      client_ip(_mkreq()) == "203.0.113.7")
check("H1f empty X-Real-IP counts as missing",
      client_ip(_mkreq({"X-Real-IP": "   "})) == "203.0.113.7")

# ══════════ H2. /mcp request body cap ══════════
_uids = []
_uname = _u + "a"
b, ct = _form({"username": _uname, "password": "Test12345",
               "password_confirm": "Test12345", "email": f"{_uname}@t.co"})
_req("POST", "/auth/register", b, {"Content-Type": ct})
b, ct = _form({"login": _uname, "password": "Test12345"})
_st, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
CK = sc.split(";")[0]
_st, _d, _sc = _req("POST", "/api/keys", {"name": "ph"},
                    {"Content-Type": "application/json", "Cookie": CK})
TOK = _d["key"]["key"]
with db() as con:
    _uid_a = int(con.execute("select id from users where username=%s",
                             (_uname,)).fetchone()["id"])
_uids.append(_uid_a)
check("H2g fixture user + api key", bool(TOK), TOK)

_CAP_MSG = "request body too large; upload large files with create_upload"

# (a) Content-Length over the cap is rejected on the HEADER alone — the
# request below declares 16MB+1 and sends no body: a body-reading
# implementation would hang against the socket timeout instead.
try:
    conn = _hc.HTTPConnection(*_HOST, timeout=15)
    conn.putrequest("POST", "/mcp")
    conn.putheader("Host", f"127.0.0.1:{_TEST_PORT}")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Authorization", f"Bearer {TOK}")
    conn.putheader("Content-Length", str(16 * 1024 * 1024 + 1))
    conn.endheaders()
    r = conn.getresponse()
    _cl_over = (r.status, _json.loads(r.read() or b"null"))
    conn.close()
except Exception as exc:   # noqa: BLE001 — a hang surfaces as a failure
    _cl_over = (None, repr(exc)[:120])
check("H2a Content-Length over cap → 413 without reading the body",
      _cl_over[0] == 413, _cl_over)
check("H2b rejection is JSON-RPC -32600 with the create_upload guidance",
      (_cl_over[1] or {}).get("error", {}).get("code") == -32600
      and (_cl_over[1] or {}).get("error", {}).get("message") == _CAP_MSG,
      _cl_over[1])

# (b) no Content-Length (chunked): the streaming count must stop the read
try:
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    _chunks = [b"a" * 8192] * 2049          # 16 MiB exactly + one 8 KiB chunk
    conn.request("POST", "/mcp", body=iter(_chunks), headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOK}",
        "Transfer-Encoding": "chunked"}, encode_chunked=True)
    r = conn.getresponse()
    _no_cl_over = (r.status, _json.loads(r.read() or b"null"))
    conn.close()
except Exception as exc:   # noqa: BLE001
    _no_cl_over = (None, repr(exc)[:120])
check("H2c chunked request over cap (no Content-Length) → 413 -32600",
      _no_cl_over[0] == 413
      and (_no_cl_over[1] or {}).get("error", {}).get("code") == -32600,
      _no_cl_over)

# (d) inline file inside the cap executes normally
st, d = _mcp({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
              "params": {"name": "share", "arguments": {
                  "files": [{"filename": "a.png",
                             "b64": _b64.b64encode(PNG).decode()}],
                  "client_ref": f"ph2in-{_u}"}}}, TOK)
_sc18 = ((d or {}).get("result") or {}).get("structuredContent") or {}
check("H2d inline base64 file inside the cap → executed result",
      st == 200 and ((d or {}).get("result") or {}).get("isError") is False
      and _sc18.get("items"), f"{st} {str(d)[:120]}")

st, d = _mcp({"jsonrpc": "2.0", "id": 11, "method": "initialize",
              "params": {"protocolVersion": "2025-03-26"}}, TOK)
_instr = ((d or {}).get("result") or {}).get("instructions") or ""
check("H2e initialize instructions state the 16 MB body cap",
      "16 MB" in _instr and "create_upload" in _instr, _instr[:120])
st, d, _sc = _req("GET", "/llms.txt")
check("H2f llms.txt states the inline/body cap",
      st == 200 and "capped at 16 MB" in str(d)
      and "request body cap is 16 MB" in str(d), str(d)[:80] if st != 200 else "")

# ══════════ H3. stored HTTP-tool credential clear ══════════
class _HookHandler(_hs.BaseHTTPRequestHandler):
    seen = []          # lower-cased header dicts of every request

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        out = _json.dumps({"items": [{"kind": "text", "text": "ok"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


_hook_srv = _hs.ThreadingHTTPServer(("127.0.0.1", 0), _HookHandler)
_hook_port = _hook_srv.server_address[1]
_th.Thread(target=_hook_srv.serve_forever, daemon=True).start()

_tid = f"authclr{_u}"
_tkey = f"{_uname}_{_tid}"
_secret = "ph-secret-" + _os.urandom(4).hex()
st, d, _sc = _req("POST", "/api/usertools", {
    "tool_id": _tid, "label": "PH AuthClear", "url": "https://93.184.216.34/x",
    "timeout_s": 30, "files_min": 0, "files_max": 1,
    "format_families": "image", "accept": [".png"], "outputs": ["text"],
    "cost": 0, "auth_header": "X-Api-Key", "auth_token": _secret,
}, {"Content-Type": "application/json", "Cookie": CK})
check("H3a tool created WITH credentials",
      st == 200 and (d or {}).get("ok"), f"{st} {str(d)[:120]}")


def _decl():
    with db() as con:
        row = con.execute("select endpoint_json from platform_tools"
                          " where tool_key=%s", (_tkey,)).fetchone()
    return _json.loads(row["endpoint_json"])


def _patch(body):
    return _req("PATCH", f"/api/usertools/{_tid}", body,
                {"Content-Type": "application/json", "Cookie": CK})


st, d, _sc = _patch({"auth_header": "X-Other-Key"})
check("H3b one-sided credential PATCH (header only) → 422 bad_auth_header",
      st == 422 and (d or {}).get("error") == "bad_auth_header", f"{st} {d}")
st, d, _sc = _patch({"auth_token": ""})
check("H3c one-sided credential PATCH (empty token only) → 422",
      st == 422 and (d or {}).get("error") == "bad_auth_header", f"{st} {d}")
st, d, _sc = _patch({"timeout_s": 61})
check("H3d credential keys absent from PATCH → stored pair preserved",
      st == 200 and "auth_token" in _decl(), f"{st} {d} decl={_decl()}")

# point the (still credentialed) tool at the local hook; user rows are
# untrusted for https — flipping trusted makes the fixture reachable
with db() as con:
    _decl2 = _decl()
    _decl2["url"] = f"http://127.0.0.1:{_hook_port}/t"
    con.execute("update platform_tools set trusted=TRUE, endpoint_json=%s"
                " where tool_key=%s", (_json.dumps(_decl2), _tkey))

_mcp_name = _tkey  # 名 = key 原文


def _call_hook(ref):
    _HookHandler.seen.clear()
    st, d = _mcp({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                  "params": {"name": _mcp_name,
                             "arguments": {"client_ref": ref}}}, TOK)
    return st, d


st, d = _call_hook(f"ph3a-{_u}")
_ok = ((d or {}).get("result") or {}).get("isError") is False
_h = _HookHandler.seen[0] if _HookHandler.seen else {}
check("H3e remote call carries the stored credential header",
      _ok and len(_HookHandler.seen) == 1
      and _h.get("x-api-key") == _secret,
      f"{st} {str(d)[:120]} headers={_h}")

st, d, _sc = _patch({"auth_header": "", "auth_token": ""})
_cleared = _decl()
check("H3f both keys empty → 200 and the pair leaves the binding",
      st == 200 and "auth_header" not in _cleared
      and "auth_token" not in _cleared and _cleared.get("url"),
      f"{st} {d} decl={_cleared}")
from governance.user_tools import _public_item  # noqa: E402
with db() as con:
    _row3 = con.execute(
        "select * from platform_tools where tool_key=%s", (_tkey,)).fetchone()
check("H3g owner projection reports has_auth=false after the clear",
      _public_item(dict(_row3))["has_auth"] is False if _row3 else False)

st, d = _call_hook(f"ph3b-{_u}")
_ok = ((d or {}).get("result") or {}).get("isError") is False
_h = _HookHandler.seen[0] if _HookHandler.seen else {}
check("H3h remote call after the clear sends NO credential header",
      _ok and "x-api-key" not in _h,
      f"{st} {str(d)[:120]} headers={_h}")
_hook_srv.shutdown()

# ══════════ H4. password change/set compare-and-set ══════════
from identity.auth import (change_password as _chg, set_password as _set,  # noqa: E402
                           hash_password as _hash, verify_password as _verify)
from app.web_auth import create_browser_session  # noqa: E402
from repo.sessions import user_by_session_token  # noqa: E402
from security import token_hash  # noqa: E402
from repo import users as users_repo  # noqa: E402
from utils import now as _now  # noqa: E402
from concurrent.futures import ThreadPoolExecutor as _TPE  # noqa: E402


def _mkpwd_user(name, password=None):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, %s, 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co",
             _hash(password) if password else None, ts, ts)).fetchone()
    return int(row["id"])


def _pw(uid):
    with db() as con:
        return users_repo.by_id(con, uid)["password_hash"]


# a wrong old password is typed AND leaves sessions alone
_uid4 = _mkpwd_user(_u + "p1", "OldPass123")
_uids.append(_uid4)
_sess4 = create_browser_session(_uid4)
try:
    _chg(_uid4, "WrongPass123", "NewPass123")
    check("H4a wrong old password → err_old_password_wrong", False, "no raise")
except ValueError as exc:
    check("H4a wrong old password → err_old_password_wrong",
          str(exc) == "err_old_password_wrong", exc)
with db() as con:
    _alive = user_by_session_token(con, token_hash(_sess4), _now()) is not None
check("H4b failed change does NOT clear sessions", _alive)

# concurrent change from the same old password: exactly one winner
_uid5 = _mkpwd_user(_u + "p2", "OldPass123")
_uids.append(_uid5)
_bar = _th.Barrier(2)


def _chg_attempt(new):
    _bar.wait()
    try:
        _chg(_uid5, "OldPass123", new)
        return None
    except ValueError as exc:
        return str(exc)


with _TPE(max_workers=2) as ex:
    _f1 = ex.submit(_chg_attempt, "WinnerAAA123")
    _f2 = ex.submit(_chg_attempt, "WinnerBBB123")
    _r1, _r2 = _f1.result(timeout=60), _f2.result(timeout=60)
check("H4c concurrent changes: exactly one wins, loser typed err_old_password_wrong",
      {_r1, _r2} == {None, "err_old_password_wrong"},
      (_r1, _r2))
check("H4d stored hash is the winner's new password",
      _verify("WinnerAAA123", _pw(_uid5))
      or _verify("WinnerBBB123", _pw(_uid5)))

# concurrent first set on a NULL hash: exactly one winner
_uid6 = _mkpwd_user(_u + "p3")
_uids.append(_uid6)


def _set_attempt(new):
    _bar2.wait()
    try:
        _set(_uid6, new)
        return None
    except ValueError as exc:
        return str(exc)


_bar2 = _th.Barrier(2)
with _TPE(max_workers=2) as ex:
    _f1 = ex.submit(_set_attempt, "FirstAAA123")
    _f2 = ex.submit(_set_attempt, "FirstBBB123")
    _r1, _r2 = _f1.result(timeout=60), _f2.result(timeout=60)
check("H4e concurrent set: exactly one wins, loser typed err_password_already_set",
      {_r1, _r2} == {None, "err_password_already_set"},
      (_r1, _r2))

# repo-level CAS contract: stale hash / already-set → 0 rows
with db() as con:
    _n = users_repo.update_password_hash_if_current(
        con, _uid6, _hash("StaleAAA123"), "not-the-current-hash", _now())
check("H4f repo CAS with a stale hash affects 0 rows", _n == 0, _n)
with db() as con:
    _n = users_repo.update_password_hash_if_unset(
        con, _uid6, _hash("SecondAAA123"), _now())
check("H4g repo first-set on a set account affects 0 rows", _n == 0, _n)

# ── cleanup ──
with db() as con:
    con.execute("delete from platform_tools where tool_key=%s", (_tkey,))
    con.execute("delete from pricing where tool_id=%s", (_tkey,))
    con.execute("delete from tool_calls where user_id = ANY(%s)", (_uids,))
    con.execute("delete from ledger where user_id = ANY(%s)", (_uids,))
    purge_users(con, _uids)
print("cleanup done")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — prod hardening ({COUNT} checks)")
