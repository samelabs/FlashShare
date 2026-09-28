#!/usr/bin/env python3
"""Auth modal canonical — JSON submit + return target + market coverage.

  A*  JSON login: ok + session cookie; the cookie authorizes
      /account/credits.
  B*  JSON login failures: err_login_failed; repeated failures trip
      the 429 throttle (same path as the form flow).
  C*  JSON register: ok + logged in; duplicate username returns the
      existing code.
  D*  Full-page form login behaves as before (302 + cookie, landing
      on the sanitized next).
  E*  next sanitation: /tools survives; //evil.com, https://evil.com
      and javascript:… all fall back to /.
  F*  X unconfigured: no X button on the login page (and the flag is
      injected false).
  G*  /tools lists first-party tools (compress among them) and public
      community tools.
"""
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
_u = "am" + _uuid.uuid4().hex[:6]
JSON_H = {"Content-Type": "application/json", "Accept": "application/json"}

from database import db

created = []
try:
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

    def _json_form(fields):
        conn = _hc.HTTPConnection(*_HOST, timeout=30)
        b, ct = _form(fields)
        conn.request("POST", fields.pop("__path"), b,
                     {"Content-Type": ct, "Accept": "application/json"})
        r = conn.getresponse()
        data = r.read()
        setc = r.getheader("Set-Cookie") or ""
        conn.close()
        try:
            return r.status, _json.loads(data) if data else None, setc
        except Exception:
            return r.status, None, setc

    # ── seed a user via form register (unchanged path) ──
    user = _u + "u"
    b, ct = _form({"username": user, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{user}@t.co"})
    st, _h, _d, _sc = _req("POST", "/auth/register", b, {"Content-Type": ct})
    check("S0 form register works (302)", st == 302, st)

    # ═══ A: JSON login success ═══════════════════════════════════════
    st, d, setc = _json_form({"__path": "/auth/login", "login": user,
                              "password": "Test12345"})
    cookie = setc.split(";")[0]
    check("A1 JSON login ok + user facts",
          st == 200 and d and d.get("ok") and d["user"].get("username") == user,
          f"{st} {d}")
    check("A2 session cookie set", bool(cookie) and "session" in cookie, setc[:60])
    st, _h, _d, _sc = _req("GET", "/account/credits", None,
                           {"Cookie": cookie, "Accept": "text/html"})
    check("A3 cookie authorizes /account/credits", st == 200, st)

    # ═══ B: JSON failures + throttle ═════════════════════════════════
    st, d, _sc = _json_form({"__path": "/auth/login", "login": user,
                             "password": "wrong-pass"})
    check("B1 wrong password → err_login_failed",
          st == 200 and d and d.get("error") == "err_login_failed", f"{st} {d}")
    thr = _u + "t"
    hit429 = False
    for i in range(12):
        st, d, _sc = _json_form({"__path": "/auth/login", "login": thr,
                                 "password": "nope"})
        if st == 429:
            hit429 = True
            check("B2 repeated failures → 429 with code",
                  d and d.get("error") == "err_too_many_attempts", f"{st} {d}")
            break
    if not hit429:
        check("B2 repeated failures → 429 with code", False, f"last st={st}")

    # ═══ C: JSON register ════════════════════════════════════════════
    reg = _u + "r"
    st, d, setc = _json_form({"__path": "/auth/register", "username": reg,
                              "email": f"{reg}@t.co", "password": "Test12345",
                              "password_confirm": "Test12345"})
    check("C1 JSON register ok + session",
          st == 200 and d and d.get("ok") and "session" in setc, f"{st} {d}")
    st, d, _sc = _json_form({"__path": "/auth/register", "username": reg,
                             "email": f"{reg}2@t.co", "password": "Test12345",
                             "password_confirm": "Test12345"})
    check("C2 duplicate username → existing code",
          d and d.get("error") == "err_username_taken", f"{st} {d}")

    # ═══ D: full-page form login unchanged ═══════════════════════════
    b, ct = _form({"login": user, "password": "Test12345", "next": "/tools"})
    st, _h, _d, setc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    loc = (_h or {}).get("location", "")
    check("D1 form login 302 + cookie",
          st == 302 and "session" in setc, f"{st} {setc[:50]}")
    check("D2 redirects to next", loc.endswith("/tools"), loc)

    # ═══ E: next sanitation (form path proves the same _safe_next) ═══
    for bad in ("//evil.com", "https://evil.com", "javascript:alert(1)"):
        b, ct = _form({"login": user, "password": "Test12345", "next": bad})
        st, _h, _d, _sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
        loc = (_h or {}).get("location", "")
        check(f"E {bad!r} → fallback /",
              st == 302 and (loc == "/" or loc.startswith("http") and loc.rstrip("/").endswith("/")), loc)

    # ═══ F: X unconfigured → no X button ═════════════════════════════
    st, _h, body, _sc = _req("GET", "/auth/login", None, {"Accept": "text/html"})
    body = body if isinstance(body, str) else ""
    check("F1 login page renders without X button",
          st == 200 and "x-login" not in body and "/auth/x/login" not in body, st)
    check("F2 flag injected false", "window.__X_LOGIN__ = false" in body, "")

    # ═══ G: market lists first-party + community tools ═══════════════
    com = _u + "c"
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
            " tool_type, label_json, icon, inputs_json, outputs_json, visibility,"
            " enabled, created_at, updated_at) values (%s, %s, %s, 'http',"
            " 'produce', '{\"en\": \"Community G\"}', 'wand', '{}', '[\"file\"]',"
            " 'public', TRUE, %s, %s)",
            (f"{com}/g", None, "g", int(time.time()), int(time.time())))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, 0, TRUE, %s)", (f"{com}/g", int(time.time())))
    st, _h, body, _sc = _req("GET", "/tools", None, {"Accept": "text/html"})
    body = body if isinstance(body, str) else ""
    check("G1 market lists first-party compress",
          st == 200 and 'href="/tool/compress"' in body, st)
    check("G2 market lists the public community tool",
          f"href=\"/tool/{com}/g\"" in body, com)

finally:
    with db() as con:
        con.execute("delete from pricing where tool_id LIKE %s", (f"{_u}%",))
        con.execute("delete from platform_tools where tool_key LIKE %s", (f"{_u}%",))
        rows = con.execute("select id from users where username LIKE %s",
                           (_u + "%",)).fetchall()
        purge_users(con, [int(r["id"]) for r in rows])

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
