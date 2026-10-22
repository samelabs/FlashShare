#!/usr/bin/env python3
"""Cross-chain platform invariants — schema domains and writers, the
unified architecture (one vault format, one membership model, one
accept contract), request-boundary hardening, and audit-fix locks.
"""
from _harness import PG, REPO, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Client IP, /mcp body cap, credential clear, password CAS
#
# Prod-hardening canonical — behavior locks for the hardening batch.
#
#   H1  client IP single read point: exactly one definition (app.web_auth.
#       client_ip) reads X-Real-IP; auth/mcp/mcp_upload/dispatch all go
#       through it; header present wins, absent/empty falls back to the
#       direct peer address.
#   H2  /mcp request body cap (16 MB): Content-Length over the cap is
#       rejected on the header alone (413 + JSON-RPC -32600 with the
#       create_upload guidance); a chunked request WITHOUT Content-Length
#       is stopped by the streaming count; an inline-file call inside the
#       cap executes normally; initialize + llms.txt state the cap.
#   H3  stored HTTP-tool credential clear: PATCH {auth_header:"",auth_token:""}
#       removes the pair from the binding (remote calls stop sending the
#       header); one-sided credential PATCH → 422 bad_auth_header; absent
#       keys keep the stored credential.
#   H4  password change/set concurrency: compare-and-set writes — two
#       concurrent changes from the same old password leave exactly one
#       winner (typed err_old_password_wrong for the loser); two
#       concurrent first sets leave exactly one winner
#       (err_password_already_set); a failed change never clears sessions.
#
# Fail-closed env guard; per-run unique names; fixtures cleaned at end.
# ════════════════════════════════════════════════════════════════════════
section('Client IP, /mcp body cap, credential clear, password CAS')

import base64 as _b64
import http.client as _hc
import http.server as _hs
import json as _json
import os as _os
import sys as _sys
import threading as _th
import time as _time
import urllib.parse as _urlparse


_HOST = ("127.0.0.1", int(TEST_PORT))
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
for _root, _dirs, _files in _os.walk(REPO):
    if "__pycache__" in _root or "/tests/" in _root.replace("\\", "/"):
        continue
    if _root.startswith(_os.path.join(REPO, "app")) or _root.startswith(_os.path.join(REPO, "core")):
        _py_files += [_os.path.join(_root, f) for f in _files if f.endswith(".py")]
# the READ expression, not the word: docstrings mention the header too
_READ = 'headers.get("X-Real-IP"'
_xrip = {p: open(p, encoding="utf-8").read().count(_READ)
         for p in _py_files}
_xrip = {p: n for p, n in _xrip.items() if n}
check("H1a X-Real-IP read in exactly one module (app/web_auth.py)",
      list(_xrip) == [_os.path.join(REPO, "app", "web_auth.py")] and
      list(_xrip.values()) == [1], _xrip)

from app.web_auth import client_ip  # noqa: E402
_consumers = ["app/routes/auth_route.py", "app/routes/mcp.py",
              "app/routes/mcp_upload.py", "core/execution/dispatch.py"]
for _mod in _consumers:
    _src = open(_os.path.join(REPO, _mod), encoding="utf-8").read()
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
    conn.putheader("Host", f"127.0.0.1:{TEST_PORT}")
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
        self.send_header("Content-Type",
                         "application/vnd.518.items+json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


_hook_srv = _hs.ThreadingHTTPServer(("127.0.0.1", 0), _HookHandler)
_hook_port = _hook_srv.server_address[1]
_th.Thread(target=_hook_srv.serve_forever, daemon=True).start()

_tid = f"authclr{_u}"
_secret = "ph-secret-" + _os.urandom(4).hex()
st, d, _sc = _req("POST", "/api/usertools", {
    "label": "PH AuthClear", "url": "https://93.184.216.34/x",
    "timeout_s": 30, "files_min": 0, "files_max": 1,
    "format_families": "image", "accept": [".png"], "outputs": ["text"],
    "cost": 0, "auth_header": "X-Api-Key", "auth_token": _secret,
}, {"Content-Type": "application/json", "Cookie": CK})
_tkey = (d or {}).get("tool_key")
check("H3a tool created WITH credentials",
      st == 200 and (d or {}).get("ok") and _tkey, f"{st} {str(d)[:120]}")


def _decl():
    with db() as con:
        row = con.execute("select endpoint_json from platform_tools"
                          " where tool_key=%s", (_tkey,)).fetchone()
    return _json.loads(row["endpoint_json"])


def _patch(body):
    return _req("PATCH", f"/api/usertools/{_tkey}", body,
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


# ════════════════════════════════════════════════════════════════════════
# State-schema invariants
#
# State-schema invariant canonical — one field, one domain, one writer.
#
# Behavior tests over the real routes + real DB constraints (fixtures
# cleaned; consecutive runs on one TEST_DB stay green):
#   Plan period_days: create/update reject <=0 (422), legal values persist
#   verbatim, direct SQL <=0 hits the DB CHECK, Tier no longer rewrites 0
#   to 30; numeric CHECKs reject negative values at the DB level.
#   Package: create/update both reject credits<=0; price=0 stays legal.
#   Pricing: writer rejects negative cost; DB rejects negative cost; 0 ok.
#   tool_calls: status domain + cost_snapshot>=0 + running/finished_at
#   time semantics at the DB level; existing claim/finalize writers stay
#   legal.
#   Withdrawals: status domain at DB level; pending/approved/rejected/paid
#   rows all legal.
#   Orders: negative amount / negative snapshot rejected; invalid status
#   rejected; existing statuses legal.
#   Plan invalid value → 422 with ZERO Creem call / ZERO audit / ZERO
#   mutation (provider ordering preserved).
# ════════════════════════════════════════════════════════════════════════
section('State-schema invariants')

import os as _os
import sys as _sys


import time as _time
ts_ssi = int(_time.time())
_u = "ss" + _os.urandom(4).hex()

from database import db

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_ssi, ts_ssi)).fetchone()
    return int(row["id"])

from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)

op = _mkuser(_u + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess = create_browser_session(op)

# Creem isolation (mirror fakes — any PATCH is a failure signal for
# invalid-value tests)
from economy import creem as _creem
_PATCH_LOG = []
_MIRROR = {"prod_ss1": {"price": 999, "currency": "USD",
                        "billing_type": "recurring",
                        "billing_period": "every-month"}}
_creem.CREEM_API_KEY = "test-key"
_orig_fp = _creem._fetch_product
_orig_patch = _creem._patch
_creem._fetch_product = lambda pid: dict(_MIRROR[pid]) if pid in _MIRROR else None
def _spy_patch(path, payload):
    _PATCH_LOG.append((path, payload))
    return {"object": "product", "id": path.rsplit("/", 1)[-1],
            "price": payload["price"], "currency": payload["currency"],
            "billing_type": "recurring", "billing_period": "every-month"}
_creem._patch = _spy_patch

def _db_rejects(sql, params=None):
    try:
        with db() as con:
            con.execute(sql, params or ())
        return False
    except Exception:
        return True

# ══════════ A. Plan period_days 三层统一 ══════════
def _so():
    with db() as con:
        return int(con.execute(
            "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p"
            " WHERE p.code <> 'free'").fetchone()["so"])

plan_ok = None
r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"ssplus_{_u}", "name": "SS Plus", "monthly_credits": 100,
    "price_cents": 999, "period_days": 45, "sort_order": _so(),
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
plan_ok = r.json().get("plan", {}).get("id")
check("A1 create 合法 period_days=45 原值落库",
      r.status_code == 200 and plan_ok is not None, f"{r.status_code}")
with db() as con:
    pd = con.execute("select period_days from plans where id=%s",
                     (plan_ok,)).fetchone()["period_days"]
check("A1b DB period_days == 45 (无 30 改写)", int(pd) == 45, str(pd))

r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"sszero_{_u}", "name": "SS Zero", "monthly_credits": 100,
    "price_cents": 999, "period_days": 0, "sort_order": _so() + 1,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
check("A2 create period_days=0 → 422 (不再静默 30)",
      r.status_code == 422, str(r.status_code))

r = client.patch(f"/samelabs/commerce/products/plans/{plan_ok}", json={"period_days": 0},
                 cookies={COOKIE_NAME: op_sess})
check("A3 update period_days=0 → 422",
      r.status_code == 422, str(r.status_code))
r = client.patch(f"/samelabs/commerce/products/plans/{plan_ok}", json={"period_days": 15},
                 cookies={COOKIE_NAME: op_sess})
check("A4 update 合法 period_days=15 原值落库", r.status_code == 200)
with db() as con:
    pd = con.execute("select period_days from plans where id=%s",
                     (plan_ok,)).fetchone()["period_days"]
check("A4b DB period_days == 15", int(pd) == 15, str(pd))

check("A5 direct SQL period_days=0 → DB CHECK reject",
      _db_rejects("update plans set period_days=0 where id=%s", (plan_ok,)))

# Tier read-model: DB 行事实直读 (构造 period_days=31 行, Tier 必须报 31)
with db() as con:
    con.execute(
        "insert into plans (code, name, monthly_credits, period_days, period,"
        " price_cents, currency, sort_order, active, ingress_files_24h,"
        " ingress_bytes_24h, expiry_days, download_limits, created_at,"
        " updated_at) values (%s, 'SS Tier', 50, 31, 'monthly', 499, 'USD',"
        " %s, TRUE, 100, 104857600, ARRAY[]::INTEGER[], ARRAY[]::INTEGER[],"
        " %s, %s)",
        (f"sstier_{_u}", _so() + 2, ts_ssi, ts_ssi))
from governance.plan_catalog import by_code, catalog
catalog(refresh=True)   # 目录缓存刷新 (生产 route 写后同机制)
tier = by_code(f"sstier_{_u}")
check("A6 Tier 读 period_days=31 (无 or-30 fallback)",
      tier is not None and tier.period_days == 31,
      str(tier and tier.period_days))

check("A7 monthly_credits 负数 direct SQL reject",
      _db_rejects("update plans set monthly_credits=-1 where id=%s", (plan_ok,)))
check("A8 price_cents 负数 direct SQL reject",
      _db_rejects("update plans set price_cents=-1 where id=%s", (plan_ok,)))
check("A9 ingress_files 负数 direct SQL reject",
      _db_rejects("update plans set ingress_files_24h=-1 where id=%s", (plan_ok,)))
check("A10 sort_order 负数 direct SQL reject",
      _db_rejects("update plans set sort_order=-1 where id=%s", (plan_ok,)))

# A11: invalid Plan value → 422 + ZERO Creem + ZERO audit + ZERO mutation
_PATCH_LOG.clear()
_n_aud = None
with db() as con:
    _n_aud = int(con.execute("select count(*) c from operator_audit"
                             ).fetchone()["c"])
r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"ssbad_{_u}", "name": "SS Bad", "monthly_credits": 100,
    "price_cents": 999, "period_days": 0, "creem_product_id": "prod_ss1",
    "sort_order": _so() + 3, "ingress_files_24h": 500,
    "ingress_bytes_24h": 524288000, "expiry_days": [7],
    "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
with db() as con:
    _n_aud2 = int(con.execute("select count(*) c from operator_audit"
                              ).fetchone()["c"])
    _row = con.execute("select 1 from plans where code=%s",
                       (f"ssbad_{_u}",)).fetchone()
check("A11 invalid plan (period 0, 绑品) → 422 + 零 Creem PATCH + 零 audit + 零落库",
      r.status_code == 422 and not _PATCH_LOG
      and _n_aud2 == _n_aud and _row is None,
      f"{r.status_code} patches={len(_PATCH_LOG)} aud={_n_aud2-_n_aud}")

# ══════════ B. Package writer 一致性 ══════════
# credits 4990 @ 499 = exact commercial-floor boundary (credits <= price*10)
r = client.post("/samelabs/commerce/products/packages", json={
    "code": f"sspkg_{_u}", "name": "SS Pkg", "credits": 4990,
    "price_cents": 499,
}, cookies={COOKIE_NAME: op_sess})
pkg_id = r.json().get("package", {}).get("id")
check("B1 package create 合法", r.status_code == 200 and pkg_id is not None,
      str(r.status_code))
r = client.post("/samelabs/commerce/products/packages", json={
    "code": f"sspkg0_{_u}", "name": "SS Zero", "credits": 0, "price": 499,
}, cookies={COOKIE_NAME: op_sess})
check("B2 create credits=0 → 422", r.status_code == 422, str(r.status_code))
r = client.patch(f"/samelabs/commerce/products/packages/{pkg_id}", json={"credits": 0},
                 cookies={COOKIE_NAME: op_sess})
check("B3 update credits=0 → 422 (与 create 一致)", r.status_code == 422,
      str(r.status_code))
check("B4 direct SQL credits=0 → DB CHECK reject",
      _db_rejects("update packages set credits=0 where id=%s", (pkg_id,)))
check("B5 direct SQL price_cents<0 → DB CHECK reject",
      _db_rejects("update packages set price_cents=-1 where id=%s", (pkg_id,)))
# commercial price floor: a package with Credits cannot be priced at $0
# (credits <= price_cents*10 → credits>0 forces price>0)
r = client.patch(f"/samelabs/commerce/products/packages/{pkg_id}", json={"price_cents": 0},
                 cookies={COOKIE_NAME: op_sess})
check("B6 price=0 → 422 credits_per_usd_exceeded (floor 关闭免费 package)",
      r.status_code == 422 and r.json().get("error") == "credits_per_usd_exceeded",
      str(r.status_code))

# ══════════ C. Pricing 不变量 ══════════
from governance.pricing import set_pricing
import json as _json
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility,"
        " enabled, created_at, updated_at, accept_json, format_family)"
        " values (%s, NULL, %s, 'local', %s,"
        " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
        (f"sstool_{_u}", f"sstool_{_u}", _json.dumps({"en": "SS"}), ts_ssi, ts_ssi))
try:
    set_pricing(f"sstool_{_u}", -5, True)
    c1 = False
except ValueError:
    c1 = True
check("C1 set_pricing 负 cost → ValueError", c1)
check("C2 direct SQL 负 cost → DB CHECK reject",
      _db_rejects("insert into pricing (tool_id, cost, enabled, updated_at)"
                  " values (%s, -1, TRUE, %s)", (f"sstool2_{_u}", ts_ssi)))
r = client.post(f"/samelabs/tools/sstool_{_u}/pricing",
                json={"cost": 0, "enabled": True},
                cookies={COOKIE_NAME: op_sess})
check("C3 cost=0 免费语义仍合法", r.status_code == 200, str(r.status_code))

# ══════════ D. tool_calls DB 不变量 ══════════
tu = _mkuser(_u + "tc")
with db() as con:
    anchor = con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, commission_rate_snapshot, invocation_hash,"
        " started_at) values (%s, %s, %s, 'running', 10, 30, %s, %s)"
        " returning id",
        (f"cr_{_u}", tu, f"sstool_{_u}", "1" * 64, ts_ssi)).fetchone()
call_id = int(anchor["id"])
check("D1 invalid status direct SQL → reject",
      _db_rejects("update tool_calls set status='bogus' where id=%s",
                  (call_id,)))
check("D2 negative cost_snapshot → reject",
      _db_rejects("update tool_calls set cost_snapshot=-1 where id=%s",
                  (call_id,)))
check("D3 running + finished_at non-null → reject",
      _db_rejects("update tool_calls set finished_at=%s where id=%s",
                  (ts_ssi, call_id)))
from repo.ledger import finalize_call_ok, finalize_call_failed
with db() as con:
    finalize_call_failed(con, call_id, "failed", "test", ts_ssi)
check("D4 running → failed (finished_at 写入) 现有 writer 合法", True)
check("D5 terminal + 清空 finished_at → reject",
      _db_rejects("update tool_calls set finished_at=NULL where id=%s",
                  (call_id,)))
with db() as con:
    a2 = con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, commission_rate_snapshot, invocation_hash,"
        " started_at) values (%s, %s, %s, 'running', 0, 30, %s, %s)"
        " returning id",
        (f"cr2_{_u}", tu, f"sstool_{_u}", "2" * 64, ts_ssi)).fetchone()
with db() as con:
    finalize_call_ok(con, int(a2["id"]), "pub", "{}", ts_ssi)
check("D6 running → ok 现有 writer 合法", True)
check("D7 insufficient 终态合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at, finished_at) values"
          " (%s, %s, %s, 'insufficient', 0, %s, %s)",
          (f"cr3_{_u}", tu, f"sstool_{_u}", ts_ssi, ts_ssi)))
check("D8 commission snapshot -1 → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at) values"
          " (%s, %s, %s, 'running', 10, -1, %s)",
          (f"cr4_{_u}", tu, f"sstool_{_u}", ts_ssi)))
check("D8b commission snapshot 101 → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at) values"
          " (%s, %s, %s, 'running', 10, 101, %s)",
          (f"cr5_{_u}", tu, f"sstool_{_u}", ts_ssi)))
check("D9 commission snapshot 0/100 边界合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, invocation_hash,"
          " started_at) values (%s, %s, %s, 'running', 10, 0, %s, %s)",
          (f"cr6_{_u}", tu, f"sstool_{_u}", "3" * 64, ts_ssi))
      and not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, invocation_hash,"
          " started_at) values (%s, %s, %s, 'running', 10, 100, %s, %s)",
          (f"cr7_{_u}", tu, f"sstool_{_u}", "4" * 64, ts_ssi)))
check("D10 running + NULL commission snapshot → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at) values (%s, %s, %s, 'running', 10, %s)",
          (f"cr8_{_u}", tu, f"sstool_{_u}", ts_ssi)))
check("D11 historical terminal (failed) NULL snapshot 合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at, finished_at) values"
          " (%s, %s, %s, 'failed', 10, %s, %s)",
          (f"cr9_{_u}", tu, f"sstool_{_u}", ts_ssi, ts_ssi)))
check("D12 running + NULL invocation_hash → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at) values"
          " (%s, %s, %s, 'running', 10, 30, %s)",
          (f"cr10_{_u}", tu, f"sstool_{_u}", ts_ssi)))
check("D12b ok + NULL invocation_hash → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at,"
          " finished_at, result_public_id, result_json) values"
          " (%s, %s, %s, 'ok', 10, 30, %s, %s, 'pub', '{}')",
          (f"cr11_{_u}", tu, f"sstool_{_u}", ts_ssi, ts_ssi)))
check("D13 failed + NULL hash (historical) 合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at, finished_at) values"
          " (%s, %s, %s, 'failed', 10, %s, %s)",
          (f"cr12_{_u}", tu, f"sstool_{_u}", ts_ssi, ts_ssi)))

# ══════════ E. withdrawals 状态域 ══════════
wu = _mkuser(_u + "wd")
with db() as con:
    con.execute(
        "insert into ledger (user_id, delta, balance_after, kind, created_at)"
        " values (%s, 20000, 20000, 'provider_earn', %s)", (wu, ts_ssi))
    w1 = con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot)"
        " values (%s, 100, 'pending', %s, 'USDT-TRC20:ssi') returning id",
        (wu, ts_ssi)).fetchone()
check("E1 pending 合法 (bare pending, review/paid fields NULL)", w1 is not None)
# S4 per-status CHECKs: approved/rejected need review fields; paid needs
# review+paid fields+non-empty payout_ref; bare inserts must REJECT.
for st in ("approved", "rejected", "paid"):
    check(f"E2 bare {st} (no review fields) → DB reject",
          _db_rejects("insert into withdrawals (user_id, amount, status,"
                      " created_at, payout_channel_snapshot)"
                      " values (%s, 100, %s, %s, 'USDT-TRC20:ssi')",
                      (wu, st, ts_ssi)))
with db() as con:
    con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot, reviewed_at, reviewed_by)"
        " values (%s, 100, 'rejected', %s, 'USDT-TRC20:ssi', %s, %s)",
        (wu, ts_ssi, ts_ssi, wu))
check("E2b rejected WITH review fields 合法", True)
with db() as con:
    con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot, reviewed_at, reviewed_by, paid_at,"
        " paid_by, payout_ref)"
        " values (%s, 100, 'paid', %s, 'USDT-TRC20:ssi', %s, %s, %s, %s,"
        " 'REF-E2c')", (wu, ts_ssi, ts_ssi, wu, ts_ssi, wu))
check("E2c paid WITH review+paid fields+ref 合法", True)
check("E3 invalid withdrawal status → DB reject",
      _db_rejects("insert into withdrawals (user_id, amount, status,"
                  " created_at, payout_channel_snapshot)"
                  " values (%s, 100, 'bogus', %s, 'USDT-TRC20:ssi')",
                  (wu, ts_ssi)))
check("E4 blank payout_channel_snapshot → DB reject",
      _db_rejects("insert into withdrawals (user_id, amount, status,"
                  " created_at, payout_channel_snapshot)"
                  " values (%s, 100, 'pending', %s, '  ')", (wu, ts_ssi)))

# ══════════ F. orders 不变量 ════════
ou = _mkuser(_u + "ord")
check("F1 negative amount_cents → reject",
      _db_rejects("insert into orders (user_id, kind, item_id, provider,"
                  " status, amount_cents, currency, created_at)"
                  " values (%s, 'package', 1, 'creem', 'pending', -1,"
                  " 'USD', %s)", (ou, ts_ssi)))
check("F2 negative credits_snapshot → reject",
      _db_rejects("insert into orders (user_id, kind, item_id, provider,"
                  " status, amount_cents, currency, credits_snapshot,"
                  " created_at) values (%s, 'package', 1, 'creem',"
                  " 'pending', 100, 'USD', -5, %s)", (ou, ts_ssi)))
check("F3 invalid order status → reject",
      _db_rejects("insert into orders (user_id, kind, item_id, provider,"
                  " status, amount_cents, currency, created_at)"
                  " values (%s, 'package', 1, 'creem', 'bogus', 100,"
                  " 'USD', %s)", (ou, ts_ssi)))
ok_statuses = True
for st in ("pending", "paid", "failed", "refunded", "disputed"):
    # canonical field combinations: snapshot>0; paid carries paid_at(+granted);
    # reversals carry refunded_at (granted reflects the prior paid fact)
    granted = st in ("paid", "refunded", "disputed")
    paid_at = ts_ssi if st in ("paid", "refunded", "disputed") else "NULL"
    refunded_at = ts_ssi if st in ("refunded", "disputed") else "NULL"
    if _db_rejects("insert into orders (user_id, kind, item_id, provider,"
                   " provider_order_id, status, amount_cents, currency,"
                   " credits_snapshot, granted, paid_at, refunded_at,"
                   " created_at) values (%s, 'package', 1, 'creem',"
                   " %s, %s, 100, 'USD', 100, %s, %s, %s, %s)",
                   (ou,
                    f"ord_f4_{st}" if st != "pending" and st != "failed" else None,
                    st, granted,
                    paid_at if paid_at != "NULL" else None,
                    refunded_at if refunded_at != "NULL" else None, ts_ssi)):
        ok_statuses = False
check("F4 五个既有 order 状态均合法 (canonical 组合)", ok_statuses)

# ═══ F5: package terminal 状态禁止 provisional ch:* identity (writer
# contract: successful payment closes on the REAL provider order id;
# ch:* is pending/failed-only) ═══
def _pkg_terminal_row(status, prov):
    # per-row UNIQUE provider id (a shared id would turn a later
    # UNIQUE violation into a fake "reject" and mask the real verdict)
    return ("insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency,"
            " credits_snapshot, granted, paid_at, refunded_at,"
            " created_at) values (%s, 'package', 1, 'creem', %s, %s,"
            " 100, 'USD', 100, TRUE, %s, %s, %s)",
            (ou, f"{prov}_{status}", status, ts_ssi,
             ts_ssi if status in ("refunded", "disputed") else None, ts_ssi))

check("F5a package paid + provider_order_id ch:* → DB reject",
      _db_rejects(*_pkg_terminal_row("paid", "ch:chk_terminal")))
check("F5b package refunded + ch:* → DB reject (terminal 范围含 reversal)",
      _db_rejects(*_pkg_terminal_row("refunded", "ch:chk_terminal")))
check("F5c package disputed + ch:* → DB reject",
      _db_rejects(*_pkg_terminal_row("disputed", "ch:chk_terminal")))
# pending/failed + ch:* 仍合法 (provisional namespace 的唯一合法阶段;
# 独立 user 避开 one-pending-per-(user,item) partial unique)
ou2 = _mkuser(_u + "ord2")
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " credits_snapshot, created_at) values (%s, 'package', 1,"
        " 'creem', %s, 'pending', 100, 'USD', 100, %s)",
        (ou2, f"ch:pend_{_u}", ts_ssi))
check("F5d pending + ch:* 仍合法 (provisional 阶段)",
      not _db_rejects("update orders set status='failed' where"
                      " provider_order_id=%s", (f"ch:pend_{_u}",)))
# real canonical provider id 的 paid package 仍合法
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " credits_snapshot, granted, paid_at, created_at)"
        " values (%s, 'package', 1, 'creem', %s, 'paid', 100, 'USD',"
        " 100, TRUE, %s, %s)",
        (ou, f"ord_real_{_u}", ts_ssi, ts_ssi))
check("F5e paid + real canonical provider id 仍合法",
      not _db_rejects("update orders set refunded_at=%s where"
                      " provider_order_id=%s", (None, f"ord_real_{_u}")))

# ══════════ cleanup (FK-safe, prefix-scoped only) ══════════
# 分立事务: 每步独立提交, 单点失败不回滚其余清理
with db() as con:
    for uid in (tu, wu, ou, ou2, op):
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
# ══════════ D. 工具链不变量 (S3/S4 契约: tag 单形状 + seed 命名空间) ══════════
from governance.catalog import validate_catalog_rows as _vcr
from governance.catalog import seed_from_registry as _seed

# D1: 文件工具缺 tag → 启动不变量 fail loud
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " files_min, files_max, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', %s, 'public', TRUE,"
        " '[\".png\"]', NULL, 1, 1, %s, %s)",
        (f"ssnotag_{_u}", f"ssnotag_{_u}", _json.dumps({"en": "NT"}), ts_ssi, ts_ssi))
try:
    _vcr(); _d1 = False
except RuntimeError as _e:
    _d1 = "has no tag" in str(_e)
check("D1 文件工具缺 tag → 启动不变量报错", _d1)
with db() as con:
    con.execute("delete from platform_tools where tool_key=%s", (f"ssnotag_{_u}",))

# D2: 正文工具带 tag → 启动不变量 fail loud
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " body_format, files_min, files_max, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', %s, 'public', TRUE,"
        " NULL, 'image', 'text', 0, 0, %s, %s)",
        (f"sstexttag_{_u}", f"sstexttag_{_u}", _json.dumps({"en": "TT"}), ts_ssi, ts_ssi))
try:
    _vcr(); _d2 = False
except RuntimeError as _e:
    _d2 = "carries a tag" in str(_e)
check("D2 正文工具带 tag → 启动不变量报错", _d2)
with db() as con:
    con.execute("delete from platform_tools where tool_key=%s", (f"sstexttag_{_u}",))

# D2b: 正文工具缺 body_format → fail loud
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " files_min, files_max, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', %s, 'public', TRUE,"
        " NULL, NULL, 0, 0, %s, %s)",
        (f"ssnobody_{_u}", f"ssnobody_{_u}", _json.dumps({"en": "NB"}), ts_ssi, ts_ssi))
try:
    _vcr(); _d2b = False
except RuntimeError as _e:
    _d2b = "body_format" in str(_e)
check("D2b 正文工具缺 body_format → 启动不变量报错", _d2b)
with db() as con:
    con.execute("delete from platform_tools where tool_key=%s", (f"ssnobody_{_u}",))

# D2c: 文件工具带 body_format → fail loud
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " body_format, files_min, files_max, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', %s, 'public', TRUE,"
        " '[\".png\"]', 'image', 'json', 1, 1, %s, %s)",
        (f"ssfilebody_{_u}", f"ssfilebody_{_u}", _json.dumps({"en": "FB"}), ts_ssi, ts_ssi))
try:
    _vcr(); _d2c = False
except RuntimeError as _e:
    _d2c = "body_format" in str(_e)
check("D2c 文件工具带 body_format → 启动不变量报错", _d2c)
with db() as con:
    con.execute("delete from platform_tools where tool_key=%s", (f"ssfilebody_{_u}",))

# D2d: 文件工具带 body_placeholder → fail loud
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " body_placeholder_json, files_min, files_max, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', %s, 'public', TRUE,"
        " '[\".png\"]', 'image', %s, 1, 1, %s, %s)",
        (f"ssfileph_{_u}", f"ssfileph_{_u}", _json.dumps({"en": "FP"}),
         _json.dumps({"en": "hint"}), ts_ssi, ts_ssi))
try:
    _vcr(); _d2d = False
except RuntimeError as _e:
    _d2d = "body_placeholder" in str(_e)
check("D2d 文件工具带 body_placeholder → 启动不变量报错", _d2d)
with db() as con:
    con.execute("delete from platform_tools where tool_key=%s", (f"ssfileph_{_u}",))

# D2e: 合法正文行 (files 0..0 + body_format + placeholder) → 通过;
# 同一行非法 body_format 值被 DB CHECK 拒绝
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " body_format, body_placeholder_json, files_min, files_max,"
        " created_at, updated_at)"
        " values (%s, NULL, %s, 'local', %s, 'public', TRUE,"
        " NULL, NULL, 'text', %s, 0, 0, %s, %s)",
        (f"ssbody_{_u}", f"ssbody_{_u}", _json.dumps({"en": "BD"}),
         _json.dumps({"en": "Paste text"}), ts_ssi, ts_ssi))
try:
    _vcr(); _d2e = True
except RuntimeError as _e:
    _d2e = False
check("D2e 合法正文行 (text + placeholder) → 启动不变量通过", _d2e)
check("D2f body_format 词表外 → DB CHECK reject",
      _db_rejects(
          "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
          " label_json, visibility, enabled, files_min, files_max,"
          " body_format, created_at, updated_at)"
          " values (%s, NULL, %s, 'local', %s, 'public', TRUE, 0, 0,"
          " 'xml', %s, %s)",
          (f"ssbadbf_{_u}", f"ssbadbf_{_u}", _json.dumps({"en": "BX"}), ts_ssi, ts_ssi)))
with db() as con:
    con.execute("delete from platform_tools where tool_key=%s", (f"ssbody_{_u}",))

# D3: seed 撞上同 key 社区行 → RuntimeError, 社区行不被改写
_mkey = f"sscollide_{_u}"
_morig = '{"en": "COMMUNITY"}'
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family,"
        " files_min, files_max, created_at, updated_at)"
        " values (%s, %s, %s, 'http', %s, 'public', TRUE,"
        " '[\".png\"]', 'image', 1, 1, %s, %s)",
        (_mkey, op, _mkey, _morig, ts_ssi, ts_ssi))
# 临时把注册表里的裸词 key 换成碰撞 key 模拟内置撞名 (不动真实注册表:
# 直接调 seed_from_registry 前无法注入 — 改为断言守卫逻辑: 注册一个
# 与社区行同名的本地工具再 seed)
import tools as _tools_mod
_tools_mod.register(
    tool_id=_mkey, label={"en": "SYSTEM"}, handler=lambda a, b: {"items": []},
    accept={".png"}, tag="image")
try:
    _seed(); _d3 = False
except RuntimeError as _e:
    _d3 = "seed collision" in str(_e)
check("D3 seed 撞社区行 → RuntimeError (fail loud)", _d3)
with db() as con:
    row = con.execute(
        "select label_json, owner_id from platform_tools where tool_key=%s",
        (_mkey,)).fetchone()
check("D3b 碰撞后社区行未被改写",
      row is not None and row["label_json"] == _morig and row["owner_id"] == op,
      str(dict(row)) if row else "row gone")
_tools_mod._registry[:] = [t for t in _tools_mod._registry if t["id"] != _mkey]
with db() as con:
    con.execute("delete from platform_tools where tool_key=%s", (_mkey,))
_vcr()  # 收尾态干净

with db() as con:
    con.execute("delete from tool_calls where user_id=%s", (tu,))
with db() as con:
    con.execute("delete from ledger where user_id=%s", (wu,))
    con.execute("delete from withdrawals where user_id=%s", (wu,))
with db() as con:
    con.execute("delete from orders where user_id in (%s, %s)", (ou, ou2))
with db() as con:
    con.execute("delete from pricing where tool_id like %s", (f"sstool%_{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"sstool%_{_u}%",))
with db() as con:
    con.execute("delete from plans where code like %s", (f"ss%_{_u}",))
    con.execute("delete from packages where code like %s", (f"sspkg%_{_u}",))
for uid in (tu, wu, ou, ou2, op):
    with db() as con:
        con.execute("delete from sessions where user_id=%s", (uid,))
        con.execute("delete from api_keys where user_id=%s", (uid,))
        purge_users(con, [uid])
print("cleanup: state-schema fixtures removed")


# ════════════════════════════════════════════════════════════════════════
# Audit-fix behavior locks
#
# Audit-fix canonical — batch/audit-fix behavior locks (items 1-14).
#
# G1  housekeeping reconciliation order: a share landing between the
#     local-row read and the Gokapi snapshot is NOT mistaken for stale.
# G2  subscription upgrade idem includes the txn ref: same-period
#     upgrade(txn A) → refund → upgrade(txn B) grants B for real; the
#     reversed-period gate still blocks replay after a BASE reversal.
# G3  worker contract codes are ToolError: oversize local output → typed
#     4xx err_output_too_large + exact refund (not 500 tool_failed);
#     "Unknown tool" stays a plain ValueError.
# G4  supply validator rejects control characters in label/desc (any
#     locale); every first-party seed declaration still passes.
# G6  zip-preview.js carries its own content stamp, independent of the
#     pages digest.
# G7  reciprocal provider calls (A↔B) settle concurrently 20×2 with
#     zero deadlock and full value conservation.
# G8  takedown audit rides INSIDE the delete transaction (failure rolls
#     both back; concurrent takedowns → one audit, one delete).
# G9  owner price PATCH preserves the operator's pricing switch.
# G10 PATCH enabled with a non-bool → 422 bad_enabled (not 500).
# G11 reserved usernames + folded handle uniqueness (Platform rejected,
#     alice01 vs Alice01, X auto-name falls back to x_{id}).
# G12 no-declaration tools: params serialization capped at
#     TEXT_PARAM_MAX_CHARS, rejected before claim/charge.
# G13 MCP third-party annotation on community tools (tools/list +
#     catalog_search), first-party unannotated, llms.txt unchanged.
# G14 MCP explicit "files": null → -32602; absent files key → default.
# (G5 publish.js escaping is locked by tests/ci/test_publish_escape_
# contract.js + the browser runtime gate publish-edit scenario.)
# ════════════════════════════════════════════════════════════════════════
section('Audit-fix behavior locks')

import os as _os
import sys as _sys


import asyncio as _asyncio
import base64 as _b64
import json as _json
import time as _time
ts_af = int(_time.time())
_u_af = "ax" + _os.urandom(4).hex()

from database import db

def _mkuser_af(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_af, ts_af)).fetchone()
    return int(row["id"])

_uids_af = []

# ══════════ G1. housekeeping reconciliation order ══════════
from sharing import housekeeping as _hk
from sharing import uploads as _sharing_uploads

_g1_user = _mkuser_af(_u_af + "g1")
_uids_af.append(_g1_user)

def _g1_row(public_id, gokapi_id):
    with db() as con:
        con.execute(
            "insert into uploads (user_id, ip, public_id, gokapi_id,"
            " file_name, content_type, size_bytes, expiry_days,"
            " allowed_downloads, expire_at, url_download, url_hotlink,"
            " created_at) values (%s, '127.0.0.1', %s, %s, 'f.txt',"
            " 'text/plain', 10, 7, 50, %s, '', '', %s)",
            (_g1_user, public_id, gokapi_id, ts_af + 86400, ts_af))

_g1_row(f"pub_{_u_af}_stale", f"gk_{_u_af}_stale")          # storage truly gone
_new_pid = f"pub_{_u_af}_fresh"
_orig_list = _hk.list_gokapi_files
def _landing_between_list(**kw):
    # called AFTER the local-row read: a share lands whose gokapi_id is
    # (legitimately) absent from the snapshot we are about to return
    _g1_row(_new_pid, f"gk_{_u_af}_fresh")
    return (True, [])                                   # snapshot knows nothing
_hk.list_gokapi_files = _landing_between_list
try:
    _hk.housekeeping_once()
finally:
    _hk.list_gokapi_files = _orig_list
with db() as con:
    stale_gone = con.execute(
        "select 1 from uploads where public_id=%s", (f"pub_{_u_af}_stale",)
    ).fetchone() is None
    fresh_kept = con.execute(
        "select 1 from uploads where public_id=%s", (_new_pid,)
    ).fetchone() is not None
check("G1a landing between read and snapshot survives",
      fresh_kept, f"fresh_kept={fresh_kept}")
check("G1b genuinely-absent storage still reconciled", stale_gone)

# ══════════ G2. subscription upgrade idem carries txn ref ══════════
from economy import creem as _creem
from economy import reconcile as _rec
from economy.subscription import handle_event as _hev
from economy.billing import spendable_balance
from repo import orders as orders_repo

_TXNS = {}
def _fake_fetch_transaction(txn_id):
    t = _TXNS.get(txn_id)
    return dict(t) if t else None
_rec.fetch_transaction = _fake_fetch_transaction

import hmac as _hmac
import hashlib as _hashlib
_creem.CREEM_WEBHOOK_SECRET = "ax-test-secret"
def _signed(event):
    raw = _json.dumps(event).encode()
    sig = _hmac.new(b"ax-test-secret", raw, _hashlib.sha256).hexdigest()
    return raw, sig

from fastapi.testclient import TestClient
from app.main import app as _app
client_af = TestClient(_app)

def _fire(event_type, txn_id):
    obj = {"id": ("ref_" if event_type == "refund.created" else "disp_") + txn_id,
           "object": "refund" if event_type == "refund.created" else "dispute",
           "transaction": {"id": txn_id}}
    event = {"id": f"evt_{txn_id}", "eventType": event_type, "object": obj}
    raw, sig = _signed(event)
    r = client_af.post("/webhooks/creem", content=raw,
                    headers={"creem-signature": sig,
                             "Content-Type": "application/json"})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text

from app.web_auth import create_browser_session, COOKIE_NAME
_op = _mkuser_af(_u_af + "op")
_uids_af.append(_op)
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (_op,))
_op_sess = create_browser_session(_op)

_MIRROR_af = {}
def _fp(pid):
    d = _MIRROR_af.get(pid)
    return dict(d) if d else None
def _pt(path, payload):
    pid = path.rsplit("/", 1)[-1]
    d = _MIRROR_af[pid]
    _MIRROR_af[pid] = {**d, "price": payload["price"],
                    "currency": payload["currency"]}
    return {"object": "product", "id": pid, "price": payload["price"],
            "currency": payload["currency"], "billing_type": "recurring",
            "billing_period": "every-month"}
_cm = _sys.modules["economy.creem"]
_cm._fetch_product = _fp
_cm._patch = _pt
_cm.CREEM_API_KEY = "test-key"

def _mkplan(code, prod, price, monthly):
    with db() as _c:
        so = int(_c.execute(
            "select coalesce(max(sort_order), 0) + 1 s from plans"
        ).fetchone()["s"])
    _MIRROR_af[prod] = {"price": price, "currency": "USD",
                     "billing_type": "recurring",
                     "billing_period": "every-month"}
    r = client_af.post("/samelabs/commerce/products/plans", json={
        "code": f"{code}_{_u_af}", "name": code.upper(),
        "monthly_credits": monthly, "price_cents": price,
        "creem_product_id": prod, "sort_order": so,
        "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
        "expiry_days": [7], "download_limits": [50],
    }, cookies={COOKIE_NAME: _op_sess})
    assert r.status_code == 200, r.text
    return int(r.json()["plan"]["id"])

def _iso(v):
    import datetime
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).isoformat()

_plus_code = f"axplus_{_u_af}"
_pro_code = f"axpro_{_u_af}"
_plus_id = _mkplan("axplus", f"prod_{_u_af}_plus", 999, 100)
_pro_id = _mkplan("axpro", f"prod_{_u_af}_pro", 2999, 500)
with db() as con:
    _plus_prod = con.execute("select creem_product_id from plans where id=%s",
                             (_plus_id,)).fetchone()["creem_product_id"]
    _pro_prod = con.execute("select creem_product_id from plans where id=%s",
                            (_pro_id,)).fetchone()["creem_product_id"]

PS = ts_af - 5 * 86400
PE = ts_af + 25 * 86400

def _sub_snap(status, product, sub_id, txn=None):
    s = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": _iso(PS),
         "current_period_end_date": _iso(PE),
         "product": {"id": product}}
    if txn:
        s["last_transaction_id"] = txn
    return s

def _seed_sub_user(name, plan_id, sub_id):
    uid = _mkuser_af(name)
    _uids_af.append(uid)
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid', 999,"
            " 'USD', %s, %s)",
            (uid, plan_id, "sub:" + sub_id, ts_af, ts_af))
        con.execute(
            "insert into memberships (user_id, plan_id,"
            " provider_subscription_id, status, started_at, renews_at,"
            " last_grant_period_start, updated_at)"
            " values (%s, %s, %s, 'active', %s, %s, %s, %s)",
            (uid, plan_id, "sub:" + sub_id, PS, PE, PS, ts_af))
    return uid

_g2 = _mkuser_af(_u_af + "g2")
_uids_af.append(_g2)
_sub2 = f"sub_{_u_af}g2"
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency, paid_at,"
        " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid', 999,"
        " 'USD', %s, %s)",
        (_g2, _plus_id, "sub:" + _sub2, ts_af, ts_af))
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, last_grant_period_start, updated_at)"
        " values (%s, %s, %s, 'active', %s, %s, %s, %s)",
        (_g2, _plus_id, "sub:" + _sub2, PS, PE, PS, ts_af))

txn_base, txn_a, txn_b = f"txn_{_u_af}base", f"txn_{_u_af}a", f"txn_{_u_af}b"
_hev("subscription.paid", _sub_snap("active", _plus_prod, _sub2, txn=txn_base))
_hev("subscription.update", _sub_snap("active", _pro_prod, _sub2))
_hev("subscription.paid", _sub_snap("active", _pro_prod, _sub2, txn=txn_a))
check("G2a base(100) + upgrade top-up(400) → 500",
      spendable_balance(_g2) == 500, spendable_balance(_g2))

_TXNS[txn_a] = {"object": "transaction", "id": txn_a, "status": "refunded",
                "type": "invoice", "subscription": _sub2,
                "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", txn_a)
check("G2b upgrade refund (webhook) → -400 net",
      sc == 200 and spendable_balance(_g2) == 100, f"{sc} {body} {spendable_balance(_g2)}")

_hev("subscription.paid", _sub_snap("active", _pro_prod, _sub2, txn=txn_b))
with db() as con:
    b_rows = con.execute(
        "select delta, idem_key from ledger where user_id=%s"
        " and kind='grant_plan_upgrade' and ref_id=%s",
        (_g2, txn_b)).fetchall()
    net = orders_repo.period_plan_credits(con, _g2, PS)
check("G2c second paid upgrade (txn B) grants for real",
      len(b_rows) == 1 and int(b_rows[0]["delta"]) == 400,
      str([dict(r) for r in b_rows]))
check("G2d idem key carries the txn ref",
      b_rows and b_rows[0]["idem_key"].endswith(":" + txn_b),
      b_rows and b_rows[0]["idem_key"])
check("G2e period net converges to plan total (500)",
      net == 500, net)
_hev("subscription.paid", _sub_snap("active", _pro_prod, _sub2, txn=txn_b))
check("G2f same-txn replay stays idempotent (no double grant)",
      spendable_balance(_g2) == 500, spendable_balance(_g2))

# reversed-period gate regression: BASE payment reversal still blocks replay
_g3 = _seed_sub_user(_u_af + "g3", _plus_id, f"sub_{_u_af}g3")
_sub3 = f"sub_{_u_af}g3"
_hev("subscription.paid", _sub_snap("active", _plus_prod, _sub3,
                                    txn=f"txn_{_u_af}g3"))
_TXNS[f"txn_{_u_af}g3"] = {"object": "transaction", "id": f"txn_{_u_af}g3",
                        "status": "refunded", "type": "invoice",
                        "subscription": _sub3,
                        "period_start": _iso(PS), "period_end": _iso(PE)}
sc, _b = _fire("refund.created", f"txn_{_u_af}g3")
_gate_blocked = False
try:
    _hev("subscription.paid", _sub_snap("active", _plus_prod, _sub3,
                                        txn=f"txn_{_u_af}g3"))
except KeyError as exc:
    _gate_blocked = str(exc) == "'paid_period_reversed'"
check("G2g base reversal still blocks same-period replay",
      _gate_blocked, "gate did not raise paid_period_reversed")

# ══════════ G3. worker contract codes are ToolError ══════════
import io as _io
import zipfile as _zipfile
import http.client as _hc
import urllib.parse as _urlparse
from tools import load_all as _load_all, ToolError as _TE
from execution.worker import process_tool as _process_tool
_load_all()

def _zip_blob(seed):
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_STORED) as z:
        z.writestr(f"r{seed}.bin", _os.urandom(3_200_000))
    return buf.getvalue()

try:
    _process_tool("zip_pack",
                  [(_zip_blob(i), f"{i}.zip", "application/zip", 0)
                   for i in range(20)], {})
    check("G3a direct worker oversize output → ToolError err_output_too_large",
          False, "no raise")
except _TE as exc:
    check("G3a direct worker oversize output → ToolError err_output_too_large",
          str(exc) == "err_output_too_large", str(exc))
except ValueError as exc:
    check("G3a direct worker oversize output → ToolError err_output_too_large",
          False, f"plain ValueError: {exc}")
try:
    _process_tool("no_such_tool_ax", [], {})
    check("G3b unknown tool stays ValueError", False, "no raise")
except _TE:
    check("G3b unknown tool stays ValueError", False, "raised ToolError")
except ValueError:
    check("G3b unknown tool stays ValueError", True)

# HTTP end-to-end against the LIVE app (the process pool lives in the
# server process): 20 incompressible zips → zip_pack output >50MB →
# typed 4xx err_output_too_large + exact refund, not 500 tool_failed.
_HOST_af = ("127.0.0.1", int(TEST_PORT))
def _live(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_af, timeout=300)
    conn.request(method, path, body, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        parsed = _json.loads(data) if data else None
    except Exception:
        parsed = data.decode(errors="replace")
    return r.status, parsed, setc

def _mp_body(files, fields):
    bnd = "axb" + _u_af
    out = b""
    for k, v in fields.items():
        out += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\""
                f"\r\n\r\n{v}\r\n").encode()
    for fn, blob in files:
        out += (f"--{bnd}\r\nContent-Disposition: form-data;"
                f" name=\"file\"; filename=\"{fn}\"\r\n"
                f"Content-Type: application/zip\r\n\r\n").encode()
        out += blob + b"\r\n"
    out += f"--{bnd}--\r\n".encode()
    return out, f"multipart/form-data; boundary={bnd}"

_g3name = _u_af + "g3u"
_form = _urlparse.urlencode({"username": _g3name, "email": f"{_g3name}@t.co",
                             "password": "Test12345",
                             "password_confirm": "Test12345"})
st, d, _sc = _live("POST", "/auth/register", _form,
                   {"Content-Type": "application/x-www-form-urlencoded"})
_form = _urlparse.urlencode({"login": _g3name, "password": "Test12345"})
st, d, g3_setc = _live("POST", "/auth/login", _form,
                       {"Content-Type": "application/x-www-form-urlencoded"})
_g3_ck = g3_setc.split(";")[0]
with db() as con:
    _g3u = int(con.execute("select id from users where username=%s",
                           (_g3name,)).fetchone()["id"])
_uids_af.append(_g3u)
from economy.billing import grant as _grant
_grant(_g3u, 1000, "manual_topup", note="ax g3")

_zips = [(f"{i}.zip", _zip_blob(100 + i)) for i in range(20)]
_body, _ct = _mp_body(_zips, {"client_ref": f"axg3up-{_u_af}", "tool": "zip_pack"})
st, d, _sc = _live("POST", "/api/uploads", _body,
                   {"Content-Type": _ct, "Cookie": _g3_ck})
assert st == 200, f"{st} {str(d)[:200]}"
up_files = (d or {}).get("files") or [(d or {}).get("file")]
balance_before = spendable_balance(_g3u)
st, d, _sc = _live("POST", "/api/tools/execute", _json.dumps({
    "tool_id": "zip_pack",
    "source": {"files": [{"public_id": f["stagingId"],
                          "source_token": f["sourceToken"]}
                         for f in up_files]},
    "params": {},
    "delivery": {"expiry_days": 7, "allowed_downloads": 50},
    "client_ref": f"axg3run-{_u_af}",
}).encode(), {"Content-Type": "application/json", "Cookie": _g3_ck})
check("G3c HTTP oversize local output → 400 err_output_too_large",
      st == 400 and (d or {}).get("error") == "err_output_too_large",
      f"{st} {str(d)[:160]}")
check("G3d exact refund (balance unchanged)",
      spendable_balance(_g3u) == balance_before,
      f"{balance_before}→{spendable_balance(_g3u)}")
with db() as con:
    row = con.execute(
        "select status, error from tool_calls where user_id=%s"
        " and client_ref=%s", (_g3u, f"axg3run-{_u_af}")).fetchone()
check("G3e tool_call terminal failed with the typed code",
      row is not None and row["status"] == "failed"
      and row["error"] == "err_output_too_large",
      dict(row) if row else None)

# ══════════ G4. supply validator rejects control characters ══════════
from governance.tool_supply import (validate_tool_declaration as _vtd,
                                    DeclarationError as _DE)

def _decl(**kw):
    d = {"tool_key": f"cf_{_u_af}", "tool_id": f"cf_{_u_af}", "owner_id": None,
         "execution": "local",
         "label": {"en": "CF"}, "desc": None,
         "format_family": "image",
         "inputs": {"files": {"min": 0, "max": 1, "accept": [".png"]}},
         "outputs": ["text"], "params": None, "visibility": "unlisted",
         "enabled": True}
    d.update(kw)
    return d

def _rejected(decl):
    try:
        _vtd(decl)
        return False
    except _DE as exc:
        return str(exc) == "bad_label"

check("G4a desc with \\n rejected", _rejected(_decl(desc={"en": "a\nb"})))
check("G4b label with \\t rejected", _rejected(_decl(label={"en": "a\tb"})))
check("G4c one bad locale in a dict rejected",
      _rejected(_decl(desc={"en": "ok", "zh": "line\rbreak"})))
check("G4d clean multi-locale desc passes",
      _vtd(_decl(desc={"en": "ok", "zh": "好"}))["desc"] == {"en": "ok", "zh": "好"})

from tools import _registry as _reg
_seeds_ok = True
_seed_err = ""
for t in _reg:
    try:
        _vtd({"tool_key": t["id"], "tool_id": t["id"], "owner_id": None,
              "execution": "local",
              "label": t["label"], "desc": t["desc"],
              "format_family": t.get("tag"),
              "inputs": {"files": {"min": t["files_min"], "max": t["files_max"],
                                   "accept": sorted(t["accept"]) if t["accept"] else None}},
              "outputs": t["outputs"], "params": t["params"],
              "visibility": "public", "enabled": True})
    except _DE as exc:
        _seeds_ok = False
        _seed_err = f"{t['id']}: {exc}"
check("G4e every first-party seed declaration still passes",
      _seeds_ok and len(_reg) >= 9, _seed_err or f"registry={len(_reg)}")

_g4u = _mkuser_af(_u_af + "g4u")
_uids_af.append(_g4u)
_g4_sess = create_browser_session(_g4u)
r = client_af.post("/api/usertools", json={
    "label": "G4", "desc": "line1\nline2",
    "url": "https://93.184.216.34/x", "timeout_s": 30,
    "files_min": 0, "files_max": 1, "format_families": "image",
    "accept": [".png"], "outputs": ["text"], "cost": 0,
}, cookies={COOKIE_NAME: _g4_sess})
check("G4f HTTP publish with newline desc → 422 bad_label",
      r.status_code == 422 and r.json().get("error") == "bad_label",
      f"{r.status_code} {r.text[:120]}")

# ══════════ G6. zip-preview.js own content stamp ══════════
import subprocess as _sp
_zip_path = _os.path.join(REPO, "static", "zip-preview.js")
def _versions():
    out = _sp.run([_sys.executable, "-c",
                   "import sys; sys.path.insert(0, '.');"
                   " sys.path.insert(0, 'core'); sys.path.insert(0, 'app');"
                   " from app.web_ctx import ZIP_PREVIEW_VERSION, PAGES_VERSION;"
                   " print(ZIP_PREVIEW_VERSION, PAGES_VERSION)"],
                  cwd=REPO, capture_output=True, text=True, check=True)
    return tuple(out.stdout.split())
try:
    v1, p1 = _versions()
    with open(_zip_path, "a", encoding="utf-8") as f:
        f.write("\n// stamp-audit-probe\n")
    v2, p2 = _versions()
finally:
    _src = open(_zip_path, encoding="utf-8").read()
    with open(_zip_path, "w", encoding="utf-8") as f:
        f.write(_src.replace("\n// stamp-audit-probe\n", ""))
check("G6a zip stamp changes with the file's content", v1 != v2, f"{v1} vs {v2}")
check("G6b pages digest independent of zip-preview.js", p1 == p2, f"{p1} vs {p2}")
_detail_src = open(_os.path.join(REPO, "static", "pages", "detail.js"),
                   encoding="utf-8").read()
check("G6c detail.js loads zip-preview with its own stamp",
      "zipPreviewVersion" in _detail_src and "pagesVersion || \"\"" not in
      _detail_src.split("zip-preview.js")[1][:200])

# ══════════ G7. reciprocal provider settlement (lock order) ══════════
from concurrent.futures import ThreadPoolExecutor as _TPE
from execution import gate as _gate
from execution.results import StagedItems, StagedFileItem
from execution.invocation import ToolInvocation, SourceFile

def _mkstaged(tag):
    return StagedItems(
        order=["file"], inline=[],
        files=[StagedFileItem(gokapi_id=f"gk_{_u_af}_{tag}", filename="out.bin",
                              content_type="application/octet-stream", size=10,
                              expiry_days=7, allowed_downloads=5,
                              expire_at=ts_af + 86400, url_hotlink="")],
        ip="127.0.0.1")

def _mk_local_tool(tool_id, owner_id, cost):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled,"
            " created_at, updated_at, accept_json, format_family)"
            " values (%s, %s, %s, 'local', %s,"
            " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
            (tool_id, owner_id, tool_id,
             _json.dumps({"en": "AX"}), ts_af, ts_af))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts_af))

_a, _b = _mkuser_af(_u_af + "pa"), _mkuser_af(_u_af + "pb")
_uids_af += [_a, _b]
_grant(_a, 2000, "manual_topup", note="ax")
_grant(_b, 2000, "manual_topup", note="ax")
_t_a, _t_b = f"axtool_{_u_af}a", f"axtool_{_u_af}b"
_mk_local_tool(_t_a, _a, 25)
_mk_local_tool(_t_b, _b, 25)

def _run_gate(payer, tool_id, owner, ref):
    loop = _asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_gate.run(
            {"id": payer},
            {"id": tool_id, "tool_key": tool_id,
             "owner_id": owner},
            ToolInvocation(tool_id=tool_id, files=[SourceFile("p", "t")],
                           params={},
                           delivery={"expiry_days": 7, "allowed_downloads": 5},
                           client_ref=ref),
            lambda: _asyncio.sleep(0, result=_mkstaged(ref))))
    finally:
        loop.close()

_rounds = 20
_g7_fail = []
with _TPE(max_workers=2) as ex:
    futs = []
    for i in range(_rounds):
        futs.append(ex.submit(_run_gate, _a, _t_b, _b, f"axg7-{_u_af}-{i}-ab"))
        futs.append(ex.submit(_run_gate, _b, _t_a, _a, f"axg7-{_u_af}-{i}-ba"))
    for f in futs:
        try:
            res = f.result(timeout=120)
            if not (res and res[0] and res[0].get("ok")):
                _g7_fail.append(f"non-ok: {res}")
        except Exception as exc:   # noqa: BLE001
            _g7_fail.append(repr(exc)[:160])
check("G7a 20×2 reciprocal concurrent settlements, zero deadlock/failure",
      not _g7_fail, "; ".join(_g7_fail[:3]))
with db() as con:
    calls = con.execute(
        "select count(*) c from tool_calls where client_ref like %s",
        (f"axg7-{_u_af}-%",)).fetchone()["c"]
    cons = con.execute(
        "select count(*) c from ledger where idem_key like 'call:%%'"
        " and user_id in %s", ((_a, _b),)).fetchone()["c"]
check("G7b all 40 calls settled ok", int(calls) == 40, calls)
check("G7c every call consumed exactly once (40 charges)", int(cons) == 40, cons)

# ══════════ G8. takedown audit inside the delete transaction ══════════
from backoffice import content as _content_cmd
_g8u = _mkuser_af(_u_af + "g8u")
_uids_af.append(_g8u)
_g1_row(f"pub_{_u_af}_g8", f"gk_{_u_af}_g8")
with db() as con:
    _g8_id = int(con.execute(
        "select id from uploads where public_id=%s", (f"pub_{_u_af}_g8",)
    ).fetchone()["id"])

_op2 = _mkuser_af(_u_af + "op2")
_uids_af.append(_op2)
_rolled_back = False
try:
    from sharing import uploads as _su
    # the row was inserted by the G1 helper — its owner is _g1_user
    _su.delete_upload(_g8_id, _g1_user,
                      audit=lambda con, row: (_ for _ in ()).throw(
                          RuntimeError("audit-failure-probe")))
except RuntimeError:
    with db() as con:
        row = con.execute("select 1 from uploads where id=%s", (_g8_id,)).fetchone()
        audits = con.execute(
            "select count(*) c from operator_audit where actor_user_id=%s"
            " and action='upload.delete'", (_op2,)).fetchone()["c"]
    _rolled_back = row is not None and int(audits) == 0
check("G8a audit failure rolls the delete back (no row, no audit)",
      _rolled_back)

def _take():
    try:
        return _content_cmd.delete_upload(_op2, _g8_id)["deleted"]
    except KeyError:
        return False
with _TPE(max_workers=2) as ex:
    res = [f.result(timeout=60) for f in
           [ex.submit(_take), ex.submit(_take)]]
with db() as con:
    audits = int(con.execute(
        "select count(*) c from operator_audit where actor_user_id=%s"
        " and action='upload.delete' and target_id=%s",
        (_op2, str(_g8_id))).fetchone()["c"])
    gone = con.execute("select 1 from uploads where id=%s",
                       (_g8_id,)).fetchone() is None
check("G8b concurrent takedowns: one delete, one audit",
      sum(1 for d in res if d) == 1 and audits == 1 and gone,
      f"deleted={res} audits={audits} gone={gone}")

# ══════════ G9/G10. usertools price/enabled patch semantics ══════════
_g9u = _mkuser_af(_u_af + "g9u")
_uids_af.append(_g9u)
_g9_sess = create_browser_session(_g9u)
r = client_af.post("/api/usertools", json={
    "label": "G9", "url": "https://93.184.216.34/x",
    "timeout_s": 30, "files_min": 0, "files_max": 1,
    "format_families": "image", "accept": [".png"], "outputs": ["text"],
    "cost": 5}, cookies={COOKIE_NAME: _g9_sess})
assert r.status_code == 200, r.text[:200]
_key = r.json().get("tool_key")
r = client_af.post(f"/samelabs/tools/{_key}/pricing",
                json={"cost": 5, "enabled": False},
                cookies={COOKIE_NAME: _op_sess})
check("G9a operator pricing takedown applied",
      r.status_code == 200 and r.json()["pricing"]["enabled"] is False, r.text[:120])
r = client_af.patch(f"/api/usertools/{_key}", json={"cost": 9},
                 cookies={COOKIE_NAME: _g9_sess})
from governance.pricing import get_pricing as _get_pricing
_pr = _get_pricing(_key)
check("G9b owner price PATCH keeps the operator takedown",
      r.status_code == 200 and _pr["enabled"] is False and _pr["cost"] == 9,
      f"{r.status_code} {dict(_pr)}")
r = client_af.patch(f"/api/usertools/{_key}", json={"enabled": "yes"},
                 cookies={COOKIE_NAME: _g9_sess})
check("G10 PATCH enabled non-bool → 422 bad_enabled",
      r.status_code == 422 and r.json().get("error") == "bad_enabled",
      f"{r.status_code} {r.text[:120]}")

# ══════════ G11. reserved + folded usernames ══════════
_reg_seq = [0]
def _register_json(username):
    _reg_seq[0] += 1
    return client_af.post("/auth/register", data={
        "username": username, "email": f"{username.lower()}{_reg_seq[0]}@{_u_af}.t.co",
        "password": "Test12345", "password_confirm": "Test12345",
    }, headers={"Accept": "application/json"})

r = _register_json("Platform")
check("G11a reserved handle (case variant) rejected",
      r.json().get("error") == "err_username_reserved",
      f"{r.status_code} {r.text[:120]}")
_handle = _u_af + "person"
r = _register_json(_handle)
with db() as con:
    _uids_af.append(int(con.execute(
        "select id from users where lower(username)=lower(%s)",
        (_handle,)).fetchone()["id"]))
check("G11b0 control: first registration succeeds",
      r.json().get("ok") is True, f"{r.status_code} {r.text[:120]}")
r = _register_json(_handle.swapcase())
check("G11b folded duplicate handle rejected as username",
      r.json().get("error") == "err_username_taken",
      f"{r.status_code} {r.text[:120]}")
with db() as con:
    idx = con.execute(
        "select indexname from pg_indexes where tablename = 'users'"
        " and indexname = 'users_username_lower_uk'").fetchone()
check("G11c folded username unique index exists", idx is not None)
from identity.auth import upsert_x_user as _upsert_x
_x_id = "9" * 12
_x_uid = _upsert_x({"id": _x_id, "username": "Platform",
                    "name": "X Platform", "profile_image_url": ""})
_uids_af.append(_x_uid)
with db() as con:
    _x_name = con.execute("select username from users where id=%s",
                          (_x_uid,)).fetchone()["username"]
check("G11d X handle hitting a reserved name falls back to x_{id}",
      _x_name == f"x_{_x_id}", _x_name)

# ══════════ G12. no-declaration params cap ══════════
_g12u = _mkuser_af(_u_af + "g12u")
_uids_af.append(_g12u)
_g12_sess = create_browser_session(_g12u)
_grant(_g12u, 500, "manual_topup", note="ax g12")
_bal12 = spendable_balance(_g12u)
r = client_af.post("/api/tools/execute", json={
    "tool_id": "share", "source": {"files": []},
    "params": {"x": "A" * 100_001},
    "delivery": {"expiry_days": 1, "allowed_downloads": 50},
    "client_ref": f"axg12-{_u_af}",
}, cookies={COOKIE_NAME: _g12_sess})
check("G12a no-decl params over TEXT_PARAM_MAX_CHARS → 422 param_too_long",
      r.status_code == 422 and r.json().get("error") == "param_too_long",
      f"{r.status_code} {r.text[:120]}")
with db() as con:
    claimed = con.execute(
        "select 1 from tool_calls where user_id=%s and client_ref=%s",
        (_g12u, f"axg12-{_u_af}")).fetchone()
check("G12b rejected before claim (no tool_calls row)", claimed is None)
check("G12c rejected before charge (balance untouched)",
      spendable_balance(_g12u) == _bal12)

# ══════════ G13. MCP third-party annotation ══════════
_g13o = _mkuser_af(_u_af + "g13o")
_uids_af.append(_g13o)
_g13_sess = create_browser_session(_g13o)
r = client_af.post("/api/usertools", json={
    "label": "AX Thirdparty", "desc": "community probe",
    "url": "https://93.184.216.34/x", "timeout_s": 30,
    "files_min": 0, "files_max": 1, "format_families": "image",
    "accept": [".png"], "outputs": ["text"], "cost": 0,
}, cookies={COOKIE_NAME: _g13_sess})
_key13 = r.json().get("tool_key")
assert r.status_code == 200, r.text[:200]
with db() as con:
    con.execute("update platform_tools set visibility='public'"
                " where tool_key=%s", (_key13,))
r = client_af.post("/api/keys", json={"name": "ax"},
                cookies={COOKIE_NAME: _g13_sess})
_tok13 = r.json()["key"]["key"]
mname13 = _key13  # 名 = key 原文

def _mcp_af(body, token):
    r = client_af.post("/mcp", json=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return r.json()

_d = _mcp_af({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _tok13)
_tools = (((_d or {}).get("result") or {}).get("tools") or [])
_by_name = {t["name"]: t.get("description", "") for t in _tools}
check("G13a community tool listed with third-party annotation",
      mname13 in _by_name
      and f"Third-party tool by @{_u_af}g13o." in _by_name.get(mname13, ""),
      _by_name.get(mname13, "")[:160])
check("G13b first-party tool carries no annotation",
      "share" in _by_name and "Third-party tool" not in _by_name["share"],
      _by_name.get("share", "")[:160])
_d = _mcp_af({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": "catalog_search",
                      "arguments": {"q": "AX Thirdparty"}}}, _tok13)
_found = ((((_d or {}).get("result") or {}).get("structuredContent")
           or {}).get("tools") or [])
_annotated = [t for t in _found if t.get("name") == mname13]
check("G13c catalog_search community result annotated",
      len(_annotated) == 1 and "Third-party tool by @" in
      (_annotated[0].get("description") or ""),
      str(_found)[:200])
# llms.txt lists the recommended (featured) tools
with db() as con:
    con.execute("update platform_tools set featured = true"
                " where tool_key=%s", (_key13,))
r = client_af.get("/llms.txt")
_llms = r.text
check("G13d llms.txt unchanged (by @, no third-party marker)",
      ("Third-party tool" not in _llms)
      and (f"- {mname13}: AX Thirdparty by @{_u_af}g13o [free]" in _llms),
      [l for l in _llms.splitlines() if mname13 in l])

# ══════════ G14. MCP explicit files:null vs missing ══════════
r = client_af.post("/api/keys", json={"name": "ax2"},
                cookies={COOKIE_NAME: _g13_sess})
_tok14 = r.json()["key"]["key"]
_d = _mcp_af({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
           "params": {"name": "share", "arguments": {
               "files": None, "params": {},
               "client_ref": f"axg14n-{_u_af}",
               "delivery": {"expiry_days": 1, "allowed_downloads": 50}}}},
          _tok14)
_err = ((_d or {}).get("error") or {})
check("G14a explicit files:null → -32602",
      _err.get("code") == -32602, _d)
_d = _mcp_af({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
           "params": {"name": "share", "arguments": {
               "params": {},
               "client_ref": f"axg14m-{_u_af}",
               "delivery": {"expiry_days": 1, "allowed_downloads": 50}}}},
          _tok14)
_err = ((_d or {}).get("error") or {})
_is_error = (((_d or {}).get("result") or {}).get("isError") is True)
check("G14b absent files key defaults (no -32602; proceeds to the count gate)",
      _err.get("code") != -32602 and _is_error,
      _d)

# ── cleanup ──
with db() as con:
    con.execute("delete from platform_tools where tool_key in %s",
                ((_t_a, _t_b, _key, _key13),))
    con.execute("delete from pricing where tool_id in %s",
                ((_t_a, _t_b, _key, _key13),))
    purge_users(con, _uids_af)   # drops memberships/orders first
    con.execute("delete from plans where code in %s",
                ((_plus_code, _pro_code),))


# ════════════════════════════════════════════════════════════════════════
# Architecture unification
#
# Architecture unification canonical — one architecture, one protocol,
# one data model, one secret format, PostgreSQL as the only app database.
# No legacy fallback / dual read / dual write / migration-on-startup for
# retired schemas. U1..U5 prove the unified architecture holds.
# ════════════════════════════════════════════════════════════════════════
section('Architecture unification')

import os as _os
import sys as _sys
sys = _sys


# ═══ U1: Vault single format ═══
import secrets_vault
from cryptography.fernet import Fernet
KEY = Fernet.generate_key()
_os.environ["TOOL_VAULT_KEY"] = KEY.decode()
import importlib
importlib.reload(secrets_vault)

s = secrets_vault.seal("topsecret")
check("U1 seal → enc1 format", s.startswith("enc1:"))
check("U1 unseal(enc1) → secret", secrets_vault.unseal(s) == "topsecret")
check("U1 unseal('') → ''", secrets_vault.unseal("") == "")

raised = None
try:
    secrets_vault.unseal("plaintext-secret")
except Exception as e:
    raised = e
check("U1 unseal(non-enc1 non-empty) → typed VaultDecryptError (fail closed)",
      isinstance(raised, secrets_vault.VaultDecryptError), raised)

# zero network is structural: unseal touches no transport module
_vsrc = open(_os.path.join(REPO, "core", "secrets_vault.py"), encoding="utf-8").read()
check("U1 vault 无 network/transport 引用",
      "http" not in _vsrc.lower() and "socket" not in _vsrc.lower())

# ═══ U2: memberships 单一模型 ═══
import psycopg2
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_cur.execute("select to_regclass('memberships') is not null")
check("U2 memberships 表存在", _cur.fetchone()[0] is True)

_dbsrc = open(_os.path.join(REPO, "core", "database.py"), encoding="utf-8").read()

# U2b: startup schema 只有 current — 零 ALTER/DROP/RENAME/backfill
check("U2b database.py 无 ALTER TABLE", "ALTER TABLE" not in _dbsrc)
check("U2b 无 DROP COLUMN", "DROP COLUMN" not in _dbsrc)
check("U2b 无 RENAME COLUMN", "RENAME COLUMN" not in _dbsrc)
check("U2b 无 DROP INDEX (旧 index 清理)", "DROP INDEX" not in _dbsrc)
check("U2b 无 historical UPDATE backfill (plans)", "UPDATE plans" not in _dbsrc)

# U2c: fresh schema 的 current 列直接由 CREATE TABLE 获得
def _cols(table, names):
    _cur.execute(
        "select column_name from information_schema.columns "
        "where table_name=%s", (table,))
    have = {r[0] for r in _cur.fetchall()}
    return all(n in have for n in names), sorted(set(names) - have)

ok, miss = _cols("users", ["payout_channel"])
check("U2c users.payout_channel 在 CREATE TABLE", ok, miss)
ok, miss = _cols("plans", ["period_days", "period", "badge_key",
                           "ingress_files_24h", "ingress_bytes_24h",
                           "expiry_days", "download_limits", "creem_product_id"])
check("U2c plans entitlement/badge/period 列齐", ok, miss)
ok, miss = _cols("platform_tools", ["desc_json", "sort_order", "featured"])
check("U2c platform_tools declaration 列齐", ok, miss)
# U2d 格式/tag 参考表 (表驱动): 九 tag (all 恒末位) / 每格式一行归属唯一
# tag / all 无直属行 / 运行态内存结构与表一致
_cur.execute("select key from tags order by sort_order")
_tag_keys = [r[0] for r in _cur.fetchall()]
check("U2d tags = 八场景 + all (all 末位)",
      _tag_keys == ["image", "document", "data", "text", "audio",
                    "video", "cad3d", "archive", "all"], _tag_keys)
_cur.execute("select count(*), count(distinct ext),"
             " count(*) filter (where tag = 'all'),"
             " count(*) filter (where tag not in (select key from tags))"
             " from formats")
_nf, _nd, _n_all, _n_orphan = _cur.fetchone()
from governance.formats import FAMILIES, FAMILY_ORDER, VOCAB_EXTS  # noqa: E402
check("U2d formats 每扩展名一行 = VOCAB 全边界, all 无直属行, 无孤儿",
      _nf == _nd == len(VOCAB_EXTS) == 119 and _n_all == 0 and _n_orphan == 0,
      (_nf, _nd, len(VOCAB_EXTS), _n_all, _n_orphan))
check("U2d 运行态 FAMILIES 由表装载 (八场景, all 不入)",
      list(FAMILIES) == FAMILY_ORDER == _tag_keys[:-1])
_cur.execute("select indexname from pg_indexes where tablename='tool_calls'")
_idx = {r[0] for r in _cur.fetchall()}
check("U2c tool_calls 当前 unique live idx 存在", "tool_calls_client_ref_live_idx" in _idx)

# ═══ U3: accept 唯一 executable input contract (真实 route) ═══
import time as _time
ts_au = int(_time.time())
_u_au = "au" + __import__("uuid").uuid4().hex[:7]
_cur.execute(
    "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
    " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id", (_u_au, f"{_u_au}@t.co", ts_au, ts_au))
_uid = _cur.fetchone()[0]

from app.main import app
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
client_au = TestClient(app)
sess = create_browser_session(_uid)

# resolver pin (无真 DNS): pub.example → public IP
import remote as _remote_mod
import socket as _socket
def _fake_resolve(host, port):
    if host == "pub.example":
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    raise OSError("no such host in test")
_remote_mod._resolve_host = _fake_resolve

def _post(body):
    base = {"label": "AU", "url": "https://pub.example/x",
            "format_families": "image"}
    base.update(body)
    return client_au.post("/api/usertools", json=base, cookies={COOKIE_NAME: sess})

r = _post({})  # 无 accept
check("U3 missing accept → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"accept": None})
check("U3 accept=None → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"accept": []})
check("U3 accept=[] → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"accept": [".png"]})
check("U3 explicit valid accept → 200",
      r.status_code == 200 and r.json().get("ok"), f"{r.status_code} {r.text[:160]}")
r = _post({"accept": ["application/pdf"]})  # MIME 不再解释
check("U3 MIME token (非 canonical ext) → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")
r = _post({"accept": [".exe"]})  # 出族
check("U3 out-of-family ext → 422 bad_accept",
      r.status_code == 422 and r.json().get("error") == "bad_accept", f"{r.status_code} {r.text[:80]}")

# ═══ U3b: stored accept semantics (extensions_for_accept 单点) ═══
from governance.formats import extensions_for_accept as _efa
check("U3b accept=None → None (explicit any)", _efa(None) is None)
try:
    _efa([]); check("U3b accept=[] rejected", False, "no raise")
except ValueError:
    check("U3b accept=[] rejected", True)
try:
    _efa(["application/pdf"]); check("U3b MIME token rejected", False, "no raise")
except KeyError:
    check("U3b MIME token rejected", True)
try:
    _efa(".pdf"); check("U3b bare string rejected", False, "no raise")
except TypeError:
    check("U3b bare string rejected", True)
try:
    _efa([".exe"]); check("U3b unknown ext rejected", False, "no raise")
except KeyError:
    check("U3b unknown ext rejected", True)
check("U3b canonical list → set", _efa([".png", ".jpg"]) == {".png", ".jpg"})

# ═══ U3d: stored physical shapes — parse_stored_accept 单点 ═══
from governance.formats import parse_stored_accept as _psa
check("U3d stored SQL None → None (唯一 any)", _psa(None) is None)
for _bad, _why in [("", "empty string"), ('null', "JSON null"),
                   ("[]", "empty array"), ('{".png": 1}', "JSON object"),
                   ('[".png", "image/png"]', "MIME array"),
                   ('[".exe"]', "unknown ext"), ('[""]', "empty token"),
                   ('".png"', "JSON scalar string"), ("not json", "invalid JSON")]:
    try:
        _psa(_bad); check(f"U3d stored {_why} → invalid", False, "no raise")
    except (KeyError, TypeError, ValueError):
        check(f"U3d stored {_why} → invalid", True)
check("U3d stored canonical ext list → set",
      _psa('[".png", ".jpg"]') == {".png", ".jpg"})

# ═══ U3e: extensions_for_accept 严格 list-only ═══
try:
    _efa((".png",)); check("U3e tuple → TypeError", False, "no raise")
except TypeError:
    check("U3e tuple → TypeError", True)
try:
    _efa({".png"}); check("U3e set → TypeError", False, "no raise")
except TypeError:
    check("U3e set → TypeError", True)
try:
    _efa({".png": 1}); check("U3e dict → TypeError", False, "no raise")
except TypeError:
    check("U3e dict → TypeError", True)

# ═══ U6: compatibility mechanisms absent ═══
_main = open(_os.path.join(REPO, "app", "main.py"), encoding="utf-8").read()
check("U6 main startup 有 validate_catalog_rows", "validate_catalog_rows()" in _main)

# ═══ U3c: dispatch invalid stored accept → tool_unavailable (fail closed,
# pre-metering: no tool_call row, no charge, no network) ═══
_cur.execute("delete from pricing where tool_id='badaccept'")
_cur.execute("delete from platform_tools where tool_key='badaccept'")
_cur.execute(
    "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
    " label_json, accept_json, format_family, visibility, enabled, trusted,"
    " created_at, updated_at) values (%s, NULL, %s, 'local', '{}',"
    " '[\"application/pdf\"]', 'image', 'public', true, false, %s, %s)",
    ("badaccept", "badaccept", ts_au, ts_au))
for _bk, _bj in (("badaccept2", "''"), ("badaccept3", "'null'")):
    _cur.execute("delete from pricing where tool_id=%s", (_bk,))
    _cur.execute("delete from platform_tools where tool_key=%s", (_bk,))
    _cur.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, accept_json, format_family, visibility, enabled, trusted,"
        " created_at, updated_at) values (%s, NULL, %s, 'local', '{}',"
        " %s, 'image', 'public', true, false, %s, %s)",
        (_bk, _bk, _bj, ts_au, ts_au))
_cur.execute(
    "insert into pricing (tool_id, cost, enabled, updated_at) values (%s, 5, true, %s)",
    ("badaccept", ts_au))
_cur.execute("select coalesce(sum(delta),0) from ledger where user_id=%s", (_uid,))
_l0 = _cur.fetchone()[0]
_cur.execute("select count(*) from tool_calls where user_id=%s", (_uid,))
_tc0 = _cur.fetchone()[0]
from identity.apikeys import create_key as _mk_key
_vtok = _mk_key(_uid, "au-test")["key"]  # Bearer path (MCP)
def _mcp_call(mid, tool, args):
    return client_au.post("/mcp", json={"jsonrpc": "2.0", "id": mid,
                                     "method": "tools/call",
                                     "params": {"name": tool, "arguments": args}},
                       headers={"Authorization": f"Bearer {_vtok}"})
def _mcp_err(r):
    try:
        return (r.json().get("result", {}).get("content", [{}])[0].get("text", "") or "")
    except Exception:
        return r.text
from economy.billing import grant as _grant28
_grant28(_uid, 500, "manual_topup", idem_key=f"au-u3c:{_uid}", note="au")
_cur.execute("select coalesce(sum(delta),0) from ledger where user_id=%s", (_uid,))
_l0 = _cur.fetchone()[0]
r = _mcp_call(1, "badaccept",
              {"files": [{"filename": "x.png", "b64": "iVBORw0KGgo="}],
               "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 1},
               "client_ref": "au-ref-1"})
_ok_status = r.status_code == 200
_err_txt = _mcp_err(r)
check("U3c invalid stored accept → tool_unavailable (pre-metering)",
      _ok_status and "tool_unavailable" in _err_txt,
      f"{r.status_code} {_err_txt[:80]}")
for _n, _bk in enumerate(("badaccept2", "badaccept3"), start=2):
    r2 = _mcp_call(_n, _bk,
                   {"files": [{"filename": "x.png", "b64": "iVBORw0KGgo="}],
                    "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 1},
                    "client_ref": f"au-ref-{_bk}"})
    check(f"U3c stored {_bk} (''/'null') → pre-metering fail",
          r2.status_code == 200 and "tool_unavailable" in _mcp_err(r2),
          f"{r2.status_code} {_mcp_err(r2)[:80]}")
_cur.execute("select coalesce(sum(delta),0) from ledger where user_id=%s", (_uid,))
_l1 = _cur.fetchone()[0]
_cur.execute("select count(*) from tool_calls where user_id=%s", (_uid,))
_tc1 = _cur.fetchone()[0]
check("U3c ledger unchanged", _l1 == _l0, (_l0, _l1))
check("U3c tool_calls unchanged", _tc1 == _tc0, (_tc0, _tc1))

# ═══ U9: credential channel boundaries (真实 HTTP 边界) ═══
# Operator authority stays users.status; the CHANNEL is what closes:
# /samelabs accepts browser sessions only, ?key= query auth is gone.
_cur.execute("update users set status='samelabs' where id=%s", (_uid,))
r = client_au.get("/samelabs", cookies={COOKIE_NAME: sess},
               headers={"Accept": "text/html"}, follow_redirects=False)
check("U9 operator session → /samelabs allowed",
      r.status_code == 200, f"{r.status_code} {r.text[:80]}")
# denial shape follows the caller — non-browser clients (no
# text/html in Accept) get a 401 JSON body, browsers keep the redirect.
r = client_au.get("/samelabs", headers={"Authorization": f"Bearer {_vtok}"},
               follow_redirects=False)
check("U9 operator bearer token → /samelabs denied (401 JSON)",
      r.status_code == 401 and r.json().get("error") == "operator_auth_required",
      f"{r.status_code} {r.text[:80]}")
r = client_au.post("/samelabs/tools/x/pricing",
                headers={"Authorization": f"Bearer {_vtok}"},
                follow_redirects=False)
check("U9 operator bearer token → /samelabs POST denied (401 JSON)",
      r.status_code == 401 and r.json().get("error") == "operator_auth_required",
      f"{r.status_code} {r.text[:80]}")
r = client_au.get("/samelabs?key=" + _vtok, follow_redirects=False)
check("U9 operator token via ?key= → /samelabs denied (401, query auth absent)",
      r.status_code == 401 and r.json().get("error") == "operator_auth_required",
      f"{r.status_code} {r.text[:80]}")
_cur.execute("update users set status='active' where id=%s", (_uid,))
r = client_au.get("/samelabs", cookies={COOKIE_NAME: sess},
               headers={"Accept": "text/html"}, follow_redirects=False)
check("U9 normal browser session → /samelabs denied (redirect)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
r = client_au.get("/samelabs", headers={"Accept": "text/html"},
               follow_redirects=False)
check("U9 anonymous browser → /samelabs denied (redirect)",
      r.status_code == 302 and "/auth/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
# query-token channel is structurally absent from the auth resolver
_main_src = open(_os.path.join(REPO, "app", "main.py"), encoding="utf-8").read()
check("U9 main.py 无 query key 认证分支",
      'query_params.get("key"' not in _main_src)
check("U9 agents.html 无 ?key= 直连示例",
      "?key=" not in open(_os.path.join(REPO, "app", "templates", "agents.html"),
                          encoding="utf-8").read())
# Bearer works on the machine surface (MCP), never on browser routes.
_ver9 = open(_os.path.join(REPO, "VERSION"), encoding="utf-8").read().strip()
r = client_au.post("/mcp", json={"jsonrpc": "2.0", "id": 91,
                              "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}},
                headers={"Authorization": f"Bearer {_vtok}"})
check("U9 Bearer 在 machine surface (/mcp initialize) 有效",
      r.status_code == 200 and "serverInfo" in r.text, f"{r.status_code} {r.text[:80]}")
check("U9 initialize serverInfo.version == root VERSION",
      r.status_code == 200
      and r.json()["result"]["serverInfo"]["version"] == _ver9,
      r.text[:120])
r = client_au.post("/mcp", json={"jsonrpc": "2.0", "id": 92,
                              "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}})
check("U9 anonymous /mcp initialize → 200 discovery (no Bearer needed)",
      r.status_code == 200 and "serverInfo" in r.text, f"{r.status_code}")
r = client_au.post("/mcp", json={"jsonrpc": "2.0", "id": 93,
                              "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}},
                headers={"Authorization": "Bearer fsk_invalid"})
check("U9 invalid Bearer on /mcp → 401", r.status_code == 401, f"{r.status_code}")
r = client_au.get("/api/me", headers={"Authorization": f"Bearer {_vtok}"})
check("U9 Bearer 在 browser surface (/api/me) 被拒",
      r.status_code == 401, f"{r.status_code} {r.text[:80]}")
_cur.execute("update users set status='active' where id=%s", (_uid,))

# ═══ U10: bcrypt-only password verification (other formats fail closed) ═══
from security import hash_password as _hash_pw
_BCRYPT_OK = _hash_pw("pw12345678")
_LEGACY_HASH = "c2FsdA==:aGFzaA=="  # salt_b64:hash_b64 shape — retired format
import security as _security_mod
check("U10 verify_password: valid bcrypt → True",
      _security_mod.verify_password("pw12345678", _BCRYPT_OK))
check("U10 verify_password: malformed → False",
      not _security_mod.verify_password("pw12345678", "not-a-hash"))
check("U10 verify_password: legacy salt:hash format → False (fail closed)",
      not _security_mod.verify_password("pw12345678", _LEGACY_HASH))
check("U10 verify_password: empty stored → False",
      not _security_mod.verify_password("pw12345678", ""))
check("U10 verify_password: wrong password on bcrypt → False",
      not _security_mod.verify_password("wrong-pass", _BCRYPT_OK))
# C26: governance.platform is orchestration only — no direct SQL/persistence
_pf_src = open(_os.path.join(REPO, "core", "governance", "platform.py"),
               encoding="utf-8").read()
check("U26 platform.py 无 con.execute 直接 SQL",
      "con.execute" not in _pf_src and "cursor" not in _pf_src)
check("U26 platform.py 无 raw SQL 语句",
      "SELECT " not in _pf_src and "INSERT INTO" not in _pf_src
      and "UPDATE " not in _pf_src and "DELETE FROM" not in _pf_src)

# C27: ingress receipt persistence/domain authority seam closed
_ig_src = open(_os.path.join(REPO, "core", "sharing", "ingress_receipt.py"),
               encoding="utf-8").read()
check("U27 ingress_receipt.py 无 con.execute 直接 SQL",
      "con.execute" not in _ig_src and "cursor" not in _ig_src)
check("U27 ingress_receipt.py 无 ingress_requests raw SQL 语句",
      "FROM ingress_requests" not in _ig_src
      and "INSERT INTO ingress_requests" not in _ig_src
      and "UPDATE ingress_requests" not in _ig_src)
_ig_repo_src = open(_os.path.join(REPO, "core", "repo",
                                  "ingress_requests.py"),
                    encoding="utf-8").read()
check("U27 ingress_requests 表 SQL 单一 repo authority",
      "FROM ingress_requests" in _ig_repo_src
      and "INSERT INTO ingress_requests" in _ig_repo_src)
_api_src = open(_os.path.join(REPO, "app", "routes", "api.py"),
                encoding="utf-8").read()
check("U27 routes/api.py 不直接 import repo.ingress_requests",
      "repo.ingress_requests" not in _api_src
      and "repo import ingress_requests" not in _api_src)

import os as _os2

# C29: orders table SQL lives only in repo/orders.py
import glob as _glob29
_orders_sql_files = []
for _f29 in _glob29.glob(_os2.path.join(REPO, "app", "**", "*.py"), recursive=True) + \
             _glob29.glob(_os2.path.join(REPO, "core", "**", "*.py"), recursive=True):
    if "__pycache__" in _f29 or _f29.endswith(_os2.path.join("repo", "orders.py")) \
            or _f29.endswith(_os2.path.join("repo", "operator_views.py")):
        # operator_views = S4 designated READ projection repo (select/join/
        # pagination only); orders WRITE authority stays repo/orders.py
        continue
    _src29 = open(_f29, encoding="utf-8", errors="ignore").read()
    if any(_pat29 in _src29 for _pat29 in
           ("FROM orders", "INTO orders", "UPDATE orders")):
        _orders_sql_files.append(_os2.path.relpath(_f29, REPO))
check("U29 production 非 repo/orders.py 无 orders SQL",
      not _orders_sql_files, str(_orders_sql_files))

# C30: HTTP binding vocabulary closure (legacy output fields retired)
from governance.tool_supply import build_http_binding, DeclarationError  # noqa: E402
_b30 = build_http_binding(
    {"url": "https://vocab.example/api", "timeout_s": 30},
    {"url": "https://old.example", "timeout_s": 90, "auth_header": "X-Key",
     "auth_token": "sealed:x"})
check("C30 build_http_binding 只含四 canonical keys",
      set(_b30) == {"url", "timeout_s", "auth_header", "auth_token"}, _b30)
check("C30 PATCH 保留未修改的合法 canonical fields",
      _b30["auth_header"] == "X-Key" and _b30["timeout_s"] == 30, _b30)
_closed_f = _closed_e = False
try:
    build_http_binding({"url": "https://x.example", "custom_key": "v"})
except DeclarationError:
    _closed_f = True
try:
    build_http_binding({"timeout_s": 60},
                       {"url": "https://o.example", "output_filename": "l.png"})
except DeclarationError:
    _closed_e = True
check("C30 arbitrary unknown binding key → DeclarationError (fail closed)",
      _closed_f and _closed_e)


# C32: single product version authority (root VERSION)
import re as _re31
_ver32 = open(_os2.path.join(REPO, "VERSION"), encoding="utf-8").read()
check("C32 VERSION 为 major.minor 唯一 release authority",
      _ver32.endswith("\n") and _ver32.count("\n") == 1
      and _re31.fullmatch(r"[0-9]+\.[0-9]+", _ver32.strip()))
from config import PRODUCT_VERSION as _PV32  # noqa: E402
check("C32 runtime PRODUCT_VERSION == root VERSION",
      _PV32 == _ver32.strip())

import re as _re33


# C35: backoffice tool audit projection reads via repo primitives
# (S4 batch A: projection migrated from the retired route monolith into
#  core/backoffice/tools.py)
_sl35 = open(_os2.path.join(REPO, "core", "backoffice", "tools.py"),
             encoding="utf-8").read()
_proj35 = _re33.search(
    r"def _safe_remote_projection.*?(?=\ndef )", _sl35, _re33.DOTALL)
check("C35 _safe_remote_projection 无 route-layer con.execute",
      _proj35 is not None and "con.execute" not in _proj35.group(0))

# C36: products page membership counts read via repo.orders primitives
_sl36 = open(_os2.path.join(REPO, "app", "routes", "samelabs",
                            "commerce.py"),
             encoding="utf-8").read()
_plans36 = _re33.search(
    r"(?:async )?def products.*?(?=\n@router|\n(?:async )?def )", _sl36,
    _re33.DOTALL)
if _plans36 is None:  # last-function fallback
    _plans36 = _re33.search(r"(?:async )?def products.*", _sl36, _re33.DOTALL)
check("C36 samelabs_plans 无 route-layer memberships/pending_downgrades SQL",
      _plans36 is not None
      and "FROM memberships" not in _plans36.group(0)
      and "FROM pending_downgrades" not in _plans36.group(0))

# C37: plan mirror commit-outcome classifier reads via repo primitives
_cls37 = _re33.search(
    r"def _classify_db_commit_outcome.*?(?=\ndef )", open(
        _os2.path.join(REPO, "core", "backoffice", "products.py"),
        encoding="utf-8").read(), _re33.DOTALL)
check("C37 classifier 函数体无 con.execute",
      _cls37 is not None and "con.execute" not in _cls37.group(0))

# C38: catalog startup invariant reads via repo primitive
_gcat38 = open(_os2.path.join(REPO, "core", "governance", "catalog.py"),
               encoding="utf-8").read()
_val38 = _re33.search(
    r"def validate_catalog_rows.*?(?=\ndef )", _gcat38, _re33.DOTALL)
check("C38 validate_catalog_rows 函数体无 con.execute",
      _val38 is not None and "con.execute" not in _val38.group(0))

# C39: subscription reversal ledger read via repo primitive
_rev39_src = open(_os2.path.join(REPO, "core", "economy", "reverse.py"),
                  encoding="utf-8").read()
_rev39 = _re33.search(
    r"def _reverse_subscription.*?(?=\ndef )", _rev39_src, _re33.DOTALL)
check("C39 _reverse_subscription 函数体无 con.execute",
      _rev39 is not None and "con.execute" not in _rev39.group(0))

# C40: subscription replay probe reads via the same repo primitive
_sub40_src = open(_os2.path.join(REPO, "core", "economy", "subscription.py"),
                  encoding="utf-8").read()
_paid40 = _re33.search(
    r"def apply_paid_period.*?(?=\ndef )", _sub40_src, _re33.DOTALL)
check("C40 apply_paid_period 函数体无 con.execute",
      _paid40 is not None and "con.execute" not in _paid40.group(0))

# C41: execution tool_calls reads via repo primitives
_gate41_src = open(_os2.path.join(REPO, "core", "execution", "gate.py"),
                   encoding="utf-8").read()
_rec41 = _re33.search(
    r"def recover_abandoned_calls.*?(?=\ndef )", _gate41_src, _re33.DOTALL)
check("C41 recover_abandoned_calls 函数体无 con.execute",
      _rec41 is not None and "con.execute" not in _rec41.group(0))
_out41 = _re33.search(
    r"def resolve_success_outcome.*?(?=\\nasync def )", _gate41_src, _re33.DOTALL)
if _out41 is None:
    _out41 = _re33.search(r"def resolve_success_outcome.*",
                          _gate41_src, _re33.DOTALL)
check("C41 resolve_success_outcome 函数体无 con.execute",
      _out41 is not None and "con.execute" not in _out41.group(0))

# C42: init_db runtime cleanup consumes repo primitives
_db42 = open(_os2.path.join(REPO, "core", "database.py"),
             encoding="utf-8").read()
_init42 = _re33.search(r"def init_db.*?(?=\ndef )", _db42, _re33.DOTALL)
if _init42 is None:
    _init42 = _re33.search(r"def init_db.*", _db42, _re33.DOTALL)
check("C42 init_db 函数体无三个 direct DELETE",
      _init42 is not None
      and "DELETE FROM oauth_states" not in _init42.group(0)
      and "DELETE FROM sessions" not in _init42.group(0)
      and "DELETE FROM upload_usage" not in _init42.group(0))


# C44: privacy handler consumes canonical params only (no silent fallback)
_tr44 = open(_os2.path.join(REPO, "core", "tools_registry.py"),
             encoding="utf-8").read()
_priv44 = _re33.search(
    r"def privacy_handler.*?(?=\ndef |\nclass )", _tr44, _re33.DOTALL)
check("C44 privacy_handler 无 silent preset fallback",
      _priv44 is not None
      and 'params.get("preset"' not in _priv44.group(0)
      and "PRESETS.get(" not in _priv44.group(0))

# C45: remaining first-party registered enum-param handlers consume
# canonical params only (no silent default/fallback)
def _body45(fname, fnpat):
    t = open(_os2.path.join(REPO, "core", fname), encoding="utf-8").read()
    m = _re33.search(fnpat, t, _re33.DOTALL)
    return m.group(0) if m else ""
_cp45 = _body45("tools_compress.py", r"def compress_handler.*?(?=\ndef |\nregister\()")
_by45 = _body45("tools_beautify.py", r"def beautify_handler.*?(?=\ndef |\nregister\()")
_ic45 = _body45("tools_ico.py", r"def ico_handler.*?(?=\ndef |\nregister\()")
for _nm45, _b45, _d45 in (
        ("compress", _cp45, "TARGETS.get("),
        ("beautify", _by45, "PRESETS.get("),
        ("ico", _ic45, "ICO_PRESETS.get(")):
    check(f"C45 {_nm45} handler 无 silent preset fallback",
          _b45 != "" and 'params.get("preset"' not in _b45
          and _d45 not in _b45)

# C46: generic Gokapi concrete operations are only used by the storage
# adapter (and its gokapi implementation); production callers go through
# storage.write/read/delete/info
_gen46 = ("upload_to_gokapi", "get_gokapi_file", "delete_gokapi_file")
_PROD_TREES = ("app", "core", "scripts", "tools")
for _root46, _dirs46, _fs46 in (w for d in _PROD_TREES for w in _os2.walk(_os2.path.join(REPO, d))):
    for _f46 in _fs46:
        if not _f46.endswith(".py") or "__pycache__" in _root46:
            continue
        _p46 = _os2.path.join(_root46, _f46)
        _rel46 = _os2.path.relpath(_p46, REPO)
        if _rel46.startswith(("core/gokapi.py", "core/storage.py", "tests/")):
            continue  # adapter+impl own the concrete ops; tests patch them
        _t46 = open(_p46, encoding="utf-8").read()
        for _sym46 in _gen46:
            check(f"C46 {_rel46} 不直接使用 concrete {_sym46}",
                  _sym46 not in _t46)

# C47: Gokapi /api/files/list transport is owned by the gokapi adapter;
# sharing owns only presentation cache policy
_up47 = open(_os2.path.join(REPO, "core", "sharing", "uploads.py"),
             encoding="utf-8").read()
check("C47 sharing/uploads 不引用 private Gokapi transport internals",
      "_gokapi_connection" not in _up47
      and "_gokapi_path" not in _up47
      and "GOKAPI_KEY" not in _up47)
for _root47, _dirs47, _fs47 in (w for d in _PROD_TREES for w in _os2.walk(_os2.path.join(REPO, d))):
    for _f47 in _fs47:
        if not _f47.endswith(".py") or "__pycache__" in _root47:
            continue
        _rel47 = _os2.path.relpath(_os2.path.join(_root47, _f47), REPO)
        if _rel47 != "core/gokapi.py" and not _rel47.startswith("tests/"):
            _t47 = open(_os2.path.join(_root47, _f47), encoding="utf-8").read()
            check(f"C47 {_rel47} 无 /api/files/list transport",
                  '"/api/files/list"' not in _t47)


# ═══ U4: backup covers PostgreSQL + Gokapi's own sqlite (third-party) ═══
_bk = open(_os.path.join(REPO, "scripts", "backup.sh"), encoding="utf-8").read()
check("U4 backup.sh 仅引用 Gokapi sqlite (第三方豁免)",
      "gokapi.sqlite" in _bk and "flashshare.sqlite" not in _bk)


# ═══ M: backup filename consistency (static) ═══
check("M backup.sh 无 gokapi-$ST typo (weekly 与 daily 文件名一致)",
      "gokapi-$ST." not in _bk and _bk.count("gokapi-$STAMP.tar.gz") >= 4)
check("M backup.sh set -euo pipefail", "set -euo pipefail" in _bk)

# ═══ U8: deploy artifact contracts ═══
# (b) backup rotation tolerates empty dirs under set -euo pipefail
import subprocess as _sp
_rot_sh = (
    "set -euo pipefail\n"
    "KEEP=4\n"
    "for kind in daily weekly; do\n"
    "  D=$(mktemp -d)\n"
    '  { ls -1t "$D"/pg-*.dump 2>/dev/null || true; } |\n'
    "  tail -n +$((KEEP + 1)) | while read -r f; do rm -f \"$f\"; done\n"
    '  echo "$kind-empty-ok"\n'
    '  rm -rf "$D"\n'
    "done\n")
_rot = _sp.run(["bash", "-c", _rot_sh], capture_output=True, text=True)
check("U8 backup rotation: empty daily+weekly under pipefail → exit 0",
      _rot.returncode == 0 and _rot.stdout.count("-empty-ok") == 2,
      _rot.stderr[:120])

# ═══ U11: required parameters carry no None / standalone fallback ═══
import inspect as _inspect
import importlib as _importlib

_economy_sub = _importlib.import_module("economy.subscription")
_gov_commission = _importlib.import_module("governance.commission")


def _param_required(fn, name):
    p = _inspect.signature(fn).parameters[name]
    return p.default is _inspect.Parameter.empty

check("U11 bind_checkout.order_id 为必填参数 (无 None fallback)",
      _param_required(_economy_sub.bind_checkout, "order_id"))
check("U11 commission_rate.con 为必填参数 (无 standalone read)",
      _param_required(_gov_commission.commission_rate, "con"))


_cur.execute("delete from pricing where tool_id like %s or tool_id like 'badaccept%%'", (f"{_u_au}_%",))
_cur.execute("delete from platform_tools where tool_key like %s or tool_key like 'badaccept%%'", (f"{_u_au}_%",))
_cur.execute("delete from platform_tools where tool_key like %s", (f"{_u_au}_%",))
_cur.execute("delete from api_keys where user_id=%s", (_uid,))
_cur.execute("delete from sessions where user_id=%s", (_uid,))
purge_users(_cur, [_uid])
_con.close()


finish()
