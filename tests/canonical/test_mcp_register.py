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

    # R4: the 401 guidance points agents at the headless endpoint
    st4, _h4, d4, _sc4 = _initialize(None)
    msg = str(((d4 or {}) if isinstance(d4, dict) else {}).get("message") or "")
    check("R4 anonymous initialize → 401 + headless guidance",
          st4 == 401 and "/auth/mcp/register" in msg, f"{st4} {msg[:80]}")

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
