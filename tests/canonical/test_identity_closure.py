#!/usr/bin/env python3
"""Identity closure canonical — behavior over the real app + isolated DB.

  A. X OAuth state bound to the initiating browser (cookie == state),
     single-use, bind flow requires the bind target's live session
  B. X callback failures render localized text, never raw X/exception text
  C. email normalization (strip + lowercase) at every write and lookup
  D. changing email requires the current password (password accounts)
  E. profile/security/unbind errors map through i18n; raw err_* never render
  F. per-IP register limiter + per-IP aggregate login-failure limiter
  G. stale session cookie on /mcp falls through to a valid Bearer
  H. operator_reset_password: temp password + revoke sessions/tokens

Every fixture uses a per-run random suffix and is deleted at the end.
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

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:140]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import re as _re
import time as _time
import urllib.parse as _up

ts = int(_time.time())
_u = "idc" + _os.urandom(4).hex()

from database import db
from i18n import server_text
from security import hash_password
from identity import auth as idauth
from identity.apikeys import create_key
from app.web_auth import create_browser_session, COOKIE_NAME
from app.routes import auth_route
from fastapi.testclient import TestClient
from app.main import app as _app

import logging as _logging
_logging.getLogger("httpx").setLevel(_logging.WARNING)

EN = server_text("en")
ZH = server_text("zh")
client = TestClient(_app)
_uids = []
_states = []


def _mkuser(name, email=None, password=None, x_id=None):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " x_user_id, status, created_at, last_login_at)"
            " values (%s, %s, %s, 'T', %s, 'active', %s, %s) returning id",
            (name, email, hash_password(password) if password else None,
             x_id, ts, ts)).fetchone()
    _uids.append(int(row["id"]))
    return int(row["id"])


def _user(uid):
    with db() as con:
        return con.execute("select * from users where id=%s", (uid,)).fetchone()


def _req(method, path, cookies=None, **kw):
    client.cookies.clear()
    return client.request(method, path, cookies=cookies or {},
                          follow_redirects=False, **kw)


def _form_error(html):
    m = _re.search(r'<p class="error-note">(.*?)</p>', html, _re.S)
    return m.group(1).strip() if m else None


def _no_raw_keys(html):
    err = _form_error(html) or ""
    return not _re.search(r"\berr_[a-z_]+\b", err)


try:
    # ═══════════ A. X OAuth browser binding ═══════════
    auth_route.X_CLIENT_ID = "canon-client"
    auth_route.X_CLIENT_SECRET = "canon-secret"
    _exchange_calls = []
    _x_payload = {}
    _exchange_raise = [None]

    def _fake_exchange(code, verifier):
        _exchange_calls.append((code, verifier))
        if _exchange_raise[0]:
            raise _exchange_raise[0]
        return {"access_token": "at-" + code}

    def _fake_fetch(access_token):
        return dict(_x_payload)

    auth_route.exchange_x_token = _fake_exchange
    auth_route.fetch_x_user = _fake_fetch
    OAUTH_COOKIE = auth_route._OAUTH_COOKIE

    def _start(cookies=None, bind=False):
        r = _req("GET", "/auth/x/login" + ("?bind=1" if bind else ""), cookies=cookies)
        loc = r.headers.get("location", "")
        state = _up.parse_qs(_up.urlparse(loc).query).get("state", [""])[0]
        _states.append(state)
        return r, state

    def _state_exists(state):
        with db() as con:
            return con.execute("select 1 from oauth_states where state=%s",
                               (state,)).fetchone() is not None

    r, st = _start()
    setc = r.headers.get("set-cookie", "")
    check("A1 x/login → X authorize redirect with state", r.status_code in (302, 307)
          and "x.com/i/oauth2/authorize" in r.headers.get("location", "") and st, r.status_code)
    check("A1b state cookie HttpOnly+Secure+SameSite=Lax, path /auth/x, value = state",
          f"{OAUTH_COOKIE}={st}" in setc and "HttpOnly" in setc and "Secure" in setc
          and "samesite=lax" in setc.lower() and "Path=/auth/x" in setc, setc)

    # forwarded callback: victim browser has no state cookie
    _exchange_calls.clear()
    r = _req("GET", f"/auth/x/callback?code=c1&state={st}")
    check("A2 callback without state cookie → 400, no session, no exchange",
          r.status_code == 400 and COOKIE_NAME not in r.headers.get("set-cookie", "")
          and not _exchange_calls, (r.status_code, _exchange_calls))
    check("A2b rejected attempt consumes the state (single use)", not _state_exists(st))
    r = _req("GET", f"/auth/x/callback?code=c1&state={st}", cookies={OAUTH_COOKIE: st})
    check("A2c consumed state + correct cookie → 400", r.status_code == 400, r.status_code)

    r, st = _start()
    _, st_other = _start()
    r = _req("GET", f"/auth/x/callback?code=c2&state={st}", cookies={OAUTH_COOKIE: st_other})
    check("A3 callback with another flow's state cookie → 400 (login CSRF)",
          r.status_code == 400 and not _exchange_calls, r.status_code)

    # normal login flow still works
    _x_payload.clear()
    _x_payload.update({"id": "xid-" + _u + "-login", "username": "xl" + _u,
                       "name": "XL", "profile_image_url": ""})
    r, st = _start()
    r = _req("GET", f"/auth/x/callback?code=c3&state={st}", cookies={OAUTH_COOKIE: st})
    setc = r.headers.get("set-cookie", "")
    with db() as con:
        xrow = con.execute("select id from users where x_user_id=%s",
                           (_x_payload["id"],)).fetchone()
    if xrow:
        _uids.append(int(xrow["id"]))
    check("A4 normal X login: 302 /, session cookie, user created, exchange with PKCE verifier",
          r.status_code == 302 and r.headers.get("location") == "/"
          and f"{COOKIE_NAME}=" in setc and xrow is not None
          and _exchange_calls and _exchange_calls[-1][0] == "c3", (r.status_code, setc))
    check("A4b state cookie cleared on success", f'{OAUTH_COOKIE}=""' in setc
          or _re.search(OAUTH_COOKIE + r"=;|" + OAUTH_COOKIE + r'="";', setc) is not None
          or "Max-Age=0" in setc, setc)
    r = _req("GET", f"/auth/x/callback?code=c3&state={st}", cookies={OAUTH_COOKIE: st})
    check("A5 replay of a used state → 400", r.status_code == 400, r.status_code)

    # bind flow
    b1 = _mkuser(_u + "b1", f"{_u}b1@t.co", "pw-b1-12345")
    b2 = _mkuser(_u + "b2", f"{_u}b2@t.co", "pw-b2-12345")
    s1 = create_browser_session(b1)
    s2 = create_browser_session(b2)
    _x_payload.clear()
    _x_payload.update({"id": "xid-" + _u + "-bind", "username": "xb" + _u,
                       "name": "XB", "profile_image_url": ""})
    r, st = _start(cookies={COOKIE_NAME: s1}, bind=True)
    r = _req("GET", f"/auth/x/callback?code=c4&state={st}",
             cookies={OAUTH_COOKIE: st, COOKIE_NAME: s2})
    check("A6 bind callback under another user's session → 400, target not bound",
          r.status_code == 400 and _user(b1)["x_user_id"] is None
          and _user(b2)["x_user_id"] is None, r.status_code)
    r, st = _start(cookies={COOKIE_NAME: s1}, bind=True)
    r = _req("GET", f"/auth/x/callback?code=c5&state={st}", cookies={OAUTH_COOKIE: st})
    check("A6b bind callback with no session → 400, not bound",
          r.status_code == 400 and _user(b1)["x_user_id"] is None, r.status_code)
    r, st = _start(cookies={COOKIE_NAME: s1}, bind=True)
    r = _req("GET", f"/auth/x/callback?code=c6&state={st}",
             cookies={OAUTH_COOKIE: st, COOKIE_NAME: s1})
    check("A7 bind callback in the initiating user's session → 302 profile, bound",
          r.status_code == 302 and r.headers.get("location") == "/account/profile"
          and _user(b1)["x_user_id"] == _x_payload["id"], r.status_code)

    v = auth_route.verify_oauth_callback
    check("A8 verify: state row + matching cookie (login) → ok",
          v("s", "s", {"bind_user_id": None}, None) is None)
    check("A8b verify: missing cookie / mismatch / no row → rejected",
          v("s", "", {"bind_user_id": None}, None) == "err_x_state_expired"
          and v("s", "t", {"bind_user_id": None}, None) == "err_x_state_expired"
          and v("s", "s", None, None) == "err_x_state_expired")
    check("A8c verify: bind requires session user == bind_user_id",
          v("s", "s", {"bind_user_id": 7}, 8) == "err_x_bind_failed"
          and v("s", "s", {"bind_user_id": 7}, None) == "err_x_bind_failed"
          and v("s", "s", {"bind_user_id": 7}, 7) is None)

    # ═══════════ B. X error mapping ═══════════
    _exchange_raise[0] = RuntimeError("X token exchange failed: HTTP 400 {\"error\":\"SECRETBODY\"}")
    r, st = _start()
    r = _req("GET", f"/auth/x/callback?code=c7&state={st}", cookies={OAUTH_COOKIE: st})
    check("B1 exchange failure → 400 localized text, no raw X body",
          r.status_code == 400 and _form_error(r.text) == EN["err_x_login_incomplete"]
          and "SECRETBODY" not in r.text, _form_error(r.text))
    r, st = _start()
    r = _req("GET", f"/auth/x/callback?code=c8&state={st}",
             cookies={OAUTH_COOKIE: st, "flashshare_lang": "zh"})
    check("B1b ZH: exchange failure → ZH text, no raw body",
          _form_error(r.text) == ZH["err_x_login_incomplete"] and "SECRETBODY" not in r.text,
          _form_error(r.text))
    _exchange_raise[0] = None
    # X account already bound to b1 → binding it to b2 fails with mapped text
    r, st = _start(cookies={COOKIE_NAME: s2}, bind=True)
    r = _req("GET", f"/auth/x/callback?code=c9&state={st}",
             cookies={OAUTH_COOKIE: st, COOKIE_NAME: s2, "flashshare_lang": "zh"})
    check("B2 bound-to-other → ZH mapped text, no raw key",
          r.status_code == 400 and _form_error(r.text) == ZH["err_x_bound_other"]
          and _no_raw_keys(r.text), _form_error(r.text))

    # ═══════════ C. email normalization ═══════════
    ip_reg = "10.%d.%d.1" % (ts % 250, _os.getpid() % 250)
    r = _req("POST", "/auth/register", headers={"X-Real-IP": ip_reg}, data={
        "username": _u + "r1", "email": f"  {_u.upper()}R1@MiXed.COM ",
        "password": "pw-r1-12345", "password_confirm": "pw-r1-12345"})
    with db() as con:
        rr = con.execute("select id, email from users where username=%s", (_u + "r1",)).fetchone()
    if rr:
        _uids.append(int(rr["id"]))
    check("C1 register stores stripped+lowercased email",
          r.status_code == 302 and rr and rr["email"] == f"{_u}r1@mixed.com",
          (r.status_code, rr and rr["email"]))
    r = _req("POST", "/auth/register", headers={"X-Real-IP": ip_reg}, data={
        "username": _u + "r2", "email": f"{_u}r1@mixed.com".upper(),
        "password": "pw-r2-12345", "password_confirm": "pw-r2-12345"})
    check("C2 register same email different case → err_email_taken text",
          _form_error(r.text) == EN["err_email_taken"], _form_error(r.text))
    legacy = _mkuser(_u + "lg", f"{_u}Legacy@X.com", "pw-lg-12345")
    r = _req("POST", "/auth/register", headers={"X-Real-IP": ip_reg}, data={
        "username": _u + "r3", "email": f"{_u}legacy@x.com",
        "password": "pw-r3-12345", "password_confirm": "pw-r3-12345"})
    check("C3 register vs legacy mixed-case row → rejected",
          _form_error(r.text) == EN["err_email_taken"], _form_error(r.text))
    check("C3b login lookup is case-insensitive (legacy row)",
          idauth.authenticate_local(f"{_u}LEGACY@x.COM", "pw-lg-12345") == legacy)
    check("C3c login by uppercased email of a normalized row",
          rr and idauth.authenticate_local(f" {_u.upper()}R1@MIXED.com ", "pw-r1-12345") == rr["id"])
    try:
        idauth.update_profile(b2, email=f"{_u}LEGACY@x.com", current_password="pw-b2-12345")
        c4 = None
    except ValueError as exc:
        c4 = str(exc)
    check("C4 profile email change onto another user's email (case-diff) → err_email_used",
          c4 == "err_email_used", c4)

    # ═══════════ D. email change requires current password ═══════════
    d1 = _mkuser(_u + "d1", f"{_u}d1@t.co", "pw-d1-12345")
    sd1 = create_browser_session(d1)
    r = _req("POST", "/account/profile", cookies={COOKIE_NAME: sd1},
             data={"email": f"{_u}d1new@t.co", "display_name": "D1"})
    check("D1 change email without current password → EN text, unchanged",
          r.status_code == 200 and _form_error(r.text) == EN["err_current_password_required"]
          and _user(d1)["email"] == f"{_u}d1@t.co", (r.status_code, _form_error(r.text)))
    r = _req("POST", "/account/profile", cookies={COOKIE_NAME: sd1, "flashshare_lang": "zh"},
             data={"email": f"{_u}d1new@t.co", "current_password": "wrong-pass"})
    check("D2 wrong current password → ZH text, unchanged, no raw key",
          _form_error(r.text) == ZH["err_old_password_wrong"] and _no_raw_keys(r.text)
          and _user(d1)["email"] == f"{_u}d1@t.co", _form_error(r.text))
    r = _req("POST", "/account/profile", cookies={COOKIE_NAME: sd1},
             data={"email": f"{_u}D1New@T.co", "current_password": "pw-d1-12345"})
    check("D3 correct current password → 302, email changed (normalized)",
          r.status_code == 302 and _user(d1)["email"] == f"{_u}d1new@t.co", r.status_code)
    r = _req("POST", "/account/profile", cookies={COOKIE_NAME: sd1},
             data={"email": f"{_u}D1NEW@t.co", "display_name": "D1 renamed"})
    check("D4 unchanged email (case only) needs no password; other fields save",
          r.status_code == 302 and _user(d1)["display_name"] == "D1 renamed", r.status_code)
    xo = _mkuser(_u + "xo", None, None, x_id="xid-" + _u + "-xo")
    sxo = create_browser_session(xo)
    r = _req("POST", "/account/profile", cookies={COOKIE_NAME: sxo},
             data={"email": f"{_u}XO@t.co"})
    check("D5 X-only user (no password) sets email without password",
          r.status_code == 302 and _user(xo)["email"] == f"{_u}xo@t.co", r.status_code)
    r = _req("GET", "/account/profile", cookies={COOKIE_NAME: sd1})
    r2 = _req("GET", "/account/profile", cookies={COOKIE_NAME: sxo})
    check("D6 profile form carries current_password only for password accounts",
          'name="current_password"' in r.text and 'name="current_password"' not in r2.text)

    # ═══════════ E. error mapping ═══════════
    r = _req("POST", "/auth/x/unbind", cookies={COOKIE_NAME: sxo})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check("E1 unbind without password → 400 err_set_email_before_unbind (not 500)",
          r.status_code == 400 and body.get("error") == "err_set_email_before_unbind",
          (r.status_code, body))
    for lang, T in (("en", EN), ("zh", ZH)):
        r = _req("POST", "/account/password", cookies={COOKIE_NAME: sxo, "flashshare_lang": lang},
                 data={"new_password": "abc", "new_password_confirm": "abc"})
        check(f"E2[{lang}] set_password validation error → rendered text (not 500)",
              r.status_code == 200 and _form_error(r.text) == T["err_password_short"]
              and _no_raw_keys(r.text), (r.status_code, _form_error(r.text)))
        r = _req("POST", "/account/password", cookies={COOKIE_NAME: sd1, "flashshare_lang": lang},
                 data={"new_password": "", "new_password_confirm": ""})
        check(f"E3[{lang}] empty new password → err_password_empty text (not err_fill_login)",
              _form_error(r.text) == T["err_password_empty"]
              and _form_error(r.text) != T["err_fill_login"], _form_error(r.text))
        r = _req("POST", "/account/password", cookies={COOKIE_NAME: sd1, "flashshare_lang": lang},
                 data={"new_password": "pw-new-12345", "new_password_confirm": "pw-new-12345"})
        check(f"E4[{lang}] missing current password → err_current_password_required text",
              _form_error(r.text) == T["err_current_password_required"], _form_error(r.text))
        r = _req("POST", "/account/password", cookies={COOKIE_NAME: sd1, "flashshare_lang": lang},
                 data={"old_password": "nope-nope", "new_password": "pw-new-12345",
                       "new_password_confirm": "pw-new-12345"})
        check(f"E5[{lang}] wrong current password → mapped text, no raw key",
              _form_error(r.text) == T["err_old_password_wrong"] and _no_raw_keys(r.text),
              _form_error(r.text))
        r = _req("POST", "/account/profile", cookies={COOKIE_NAME: sd1, "flashshare_lang": lang},
                 data={"email": "not-an-email", "current_password": "pw-d1-12345"})
        check(f"E6[{lang}] profile domain error → mapped text, no raw key",
              _form_error(r.text) == T["err_email_invalid"] and _no_raw_keys(r.text),
              _form_error(r.text))

    class _Req:
        class state:
            lang = "zh"
    from app.web_ctx import error_text
    check("E7 unknown code → generic localized text, never raw",
          error_text(_Req, "boom: psycopg2 detail") == ZH["err_unexpected"]
          and error_text(_Req, "err_not_a_key") == ZH["err_unexpected"])
    _all_langs = ("fr", "ko", "ru", "ar", "de", "pt", "ja", "zh", "es", "en")
    _new_keys = ("err_current_password_required", "err_user_suspended",
                 "err_unexpected", "err_too_many_attempts")
    check("E8 new error keys present and non-empty in all 10 locales",
          all(server_text(l).get(k) for l in _all_langs for k in _new_keys))
    _i18n_js = open(_os.path.join(_REPO, "static", "i18n.js"), encoding="utf-8").read()
    check("E8b static/i18n.js regenerated with new keys",
          all(k in _i18n_js for k in _new_keys))

    # ═══════════ F. rate limits ═══════════
    ip_a = "10.200.%d.%d" % (ts % 250, _os.getpid() % 250)
    ip_b = "10.201.%d.%d" % (ts % 250, _os.getpid() % 250)
    bad = {"username": "x", "email": "a@b.co", "password": "p", "password_confirm": "p"}
    codes = [_req("POST", "/auth/register", headers={"X-Real-IP": ip_a}, data=bad).status_code
             for _ in range(auth_route._REGISTER_IP_LIMIT)]
    r = _req("POST", "/auth/register", headers={"X-Real-IP": ip_a}, data=bad)
    check("F1 register per-IP limiter: attempts beyond limit → 429 localized",
          all(c == 200 for c in codes) and r.status_code == 429
          and _form_error(r.text) == EN["err_too_many_attempts"], (codes[-1], r.status_code))
    r = _req("POST", "/auth/register", headers={"X-Real-IP": ip_b}, data=bad)
    check("F1b other IP unaffected", r.status_code == 200, r.status_code)

    ip_c = "10.202.%d.%d" % (ts % 250, _os.getpid() % 250)
    ip_d = "10.203.%d.%d" % (ts % 250, _os.getpid() % 250)
    for i in range(auth_route._LOGIN_IP_FAIL_LIMIT):
        _req("POST", "/auth/login", headers={"X-Real-IP": ip_c},
             data={"login": f"{_u}nouser{i}", "password": "whatever-1"})
    r = _req("POST", "/auth/login", headers={"X-Real-IP": ip_c},
             data={"login": _u + "b1", "password": "pw-b1-12345"})
    check("F2 login per-IP aggregate limiter (distinct logins) → 429 even with valid creds",
          r.status_code == 429 and _form_error(r.text) == EN["err_too_many_attempts"],
          r.status_code)
    r = _req("POST", "/auth/login", headers={"X-Real-IP": ip_d},
             data={"login": _u + "b1", "password": "pw-b1-12345"})
    check("F2b other IP logs in normally", r.status_code == 302, r.status_code)
    ip_e = "10.204.%d.%d" % (ts % 250, _os.getpid() % 250)
    for i in range(auth_route._LOGIN_FAIL_LIMIT):
        _req("POST", "/auth/login", headers={"X-Real-IP": ip_e},
             data={"login": _u + "b2", "password": "wrong-pass"})
    r = _req("POST", "/auth/login", headers={"X-Real-IP": ip_e},
             data={"login": _u + "b2", "password": "pw-b2-12345"})
    check("F3 per-(ip, login) throttle unchanged", r.status_code == 200
          and _form_error(r.text) == EN["err_login_failed"], r.status_code)

    # ═══════════ G. /mcp stale cookie + Bearer ═══════════
    g = _mkuser(_u + "g", f"{_u}g@t.co", "pw-g-123456")
    tok = create_key(g, "idc")["key"]
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    r = _req("POST", "/mcp", cookies={COOKIE_NAME: "stale-" + _u},
             headers={"Authorization": f"Bearer {tok}"}, json=rpc)
    check("G1 stale cookie + valid Bearer on /mcp → authenticated (not 401)",
          r.status_code == 200 and "result" in r.json(), r.status_code)
    r = _req("POST", "/mcp", cookies={COOKIE_NAME: "stale-" + _u}, json=rpc)
    check("G2 stale cookie, no Bearer → 401", r.status_code == 401, r.status_code)
    r = _req("GET", "/account/profile", cookies={COOKIE_NAME: "stale-" + _u},
             headers={"Authorization": f"Bearer {tok}"})
    check("G3 stale cookie + Bearer on /account → login redirect (Bearer never a web user)",
          r.status_code == 302 and r.headers.get("location") == "/auth/login", r.status_code)

    # H-block retired: operator_reset_password had no production caller
    # (FX-2 dead-code sweep)

finally:
    # ═══════════ cleanup (only this run's fixtures) ═══════════
    with db() as con:
        extra = con.execute(
            "select id from users where username like %s or x_user_id like %s",
            (_u + "%", "xid-" + _u + "%")).fetchall()
    ids = sorted(set(_uids) | {int(x["id"]) for x in extra})
    with db() as con:
        for st_ in _states:
            con.execute("delete from oauth_states where state=%s", (st_,))
        for uid in ids:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            con.execute("delete from users where id=%s", (uid,))
        left = con.execute(
            "select count(*) c from users where username like %s or x_user_id like %s",
            (_u + "%", "xid-" + _u + "%")).fetchone()["c"]
    print("cleanup: users removed", len(ids), "left", left)

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — identity closure ({COUNT} checks)")
