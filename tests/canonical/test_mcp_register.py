#!/usr/bin/env python3
"""MCP headless registration canonical — POST /auth/mcp/register.

  R*  The unauthenticated machine registration: a bare POST mints an
      account plus its Bearer key in one response (no cookie, no
      session); the returned key authenticates a real MCP initialize;
      the 401 guidance points agents at the headless endpoint; a
      provided username follows the web validators (taken → 409 typed,
      short → 400 typed); a non-JSON body is err_invalid_request.
"""
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
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
    st, _h, d, _sc = _anon("tools/call", {"name": "share", "arguments": {
        "files": [{"filename": "a.txt", "b64": "aGk="}], "client_ref": "r4c"}})
    _r = (d or {}).get("result") or {}
    _body = _json.loads(((_r.get("content") or [{}])[0]).get("text") or "{}")
    check("R4c anonymous tools/call → isError err_auth_required naming register (no HTTP 401)",
          st == 200 and _r.get("isError") is True
          and _body.get("error") == "err_auth_required"
          and "register" in _body.get("message", ""), f"{st} {_body}")
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
          st == 401 and _h.get("www-authenticate") == 'Bearer realm="518"'
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

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
