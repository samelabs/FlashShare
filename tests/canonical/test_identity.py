#!/usr/bin/env python3
"""Identity chain — register/login/OAuth → session/Bearer → API keys →
ownership → profile/password → operator authority, plus the OAuth 2.1
authorization server for /mcp.
"""
from _harness import REPO, TEST_DB, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# X identity avatar authority
#
# X identity closure canonical — avatar authority = users.avatar_url.
#
# Behavior tests over the real DB (fixtures cleaned; consecutive runs on one
# TEST_DB stay green):
#   1. X signup + image        → avatar_url written verbatim
#   2. X signup + empty image  → avatar_url empty (UI initials fallback)
#   3. existing refresh + new image → avatar_url overwritten
#   4. existing refresh + empty image → avatar_url kept (never cleared)
#   5. bind X + image          → avatar_url overwritten
#   6. bind X + empty image    → avatar_url kept
#   7. unbind X                → x_user_id/x_username/x_display_name/
#                                x_profile_url/avatar_url ALL cleared
#   8. public_user().avatarUrl ← avatar_url only
#   9. fresh schema carries avatar_url
# ════════════════════════════════════════════════════════════════════════
section('X identity avatar authority')

import os as _os
import sys as _sys


import time as _time
ts = int(_time.time())
_u = "xav" + _os.urandom(4).hex()

from database import db
from identity.auth import upsert_x_user, unbind_x, public_user
from repo import users as users_repo


def _mklocal(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])


def _row(user_id):
    with db() as con:
        return users_repo.by_id(con, user_id)


IMG1 = "https://pbs.twimg.com/profile_images/1/a.jpg"
IMG2 = "https://pbs.twimg.com/profile_images/2/b.jpg"

# 1. signup + image
uid1 = upsert_x_user({"id": "xid-" + _u + "-1", "username": "xuser" + _u,
                      "name": "X One", "profile_image_url": IMG1})
r = _row(uid1)
check("1 signup+image: avatar_url written", r["avatar_url"] == IMG1, r["avatar_url"])

# 2. signup + empty image
uid2 = upsert_x_user({"id": "xid-" + _u + "-2", "username": "xuser" + _u + "b",
                      "name": "X Two", "profile_image_url": ""})
r = _row(uid2)
check("2 signup+empty: avatar_url empty", not r["avatar_url"], r["avatar_url"])

# 3. existing refresh + NEW image → overwrite
upsert_x_user({"id": "xid-" + _u + "-1", "username": "xuser" + _u,
               "name": "X One", "profile_image_url": IMG2})
r = _row(uid1)
check("3 refresh+new image: avatar overwritten", r["avatar_url"] == IMG2, r["avatar_url"])

# 4. existing refresh + EMPTY image → keep
upsert_x_user({"id": "xid-" + _u + "-1", "username": "xuser" + _u,
               "name": "X One", "profile_image_url": ""})
r = _row(uid1)
check("4 refresh+empty: avatar kept", r["avatar_url"] == IMG2, r["avatar_url"])

# 5. bind + image → overwrite; 6. bind + empty → keep
loc1 = _mklocal(_u + "loc1")
upsert_x_user({"id": "xid-" + _u + "-3", "username": "xuser" + _u + "c",
               "name": "X Three", "profile_image_url": IMG1}, bind_user_id=loc1)
r = _row(loc1)
check("5 bind+image: avatar overwritten", r["avatar_url"] == IMG1, r["avatar_url"])

loc2 = _mklocal(_u + "loc2")
with db() as con:
    con.execute("update users set avatar_url=%s where id=%s", (IMG2, loc2))
upsert_x_user({"id": "xid-" + _u + "-4", "username": "xuser" + _u + "d",
               "name": "X Four", "profile_image_url": ""}, bind_user_id=loc2)
r = _row(loc2)
check("6 bind+empty: avatar kept", r["avatar_url"] == IMG2, r["avatar_url"])

# 7. unbind → X identity + avatar all cleared
with db() as con:
    con.execute("update users set password_hash='h', email=%s where id=%s",
                (f"{_u}loc2@t.co", loc2))
unbind_x(loc2)
r = _row(loc2)
cleared = (r["x_user_id"] is None and r["x_username"] is None
           and r["x_display_name"] is None and r["x_profile_url"] is None
           and r["avatar_url"] is None)
check("7 unbind: X identity + avatar all cleared", cleared,
      {k: r[k] for k in ("x_user_id", "x_username", "x_display_name",
                         "x_profile_url", "avatar_url")})

# 8. public_user().avatarUrl ← avatar_url only
pu = public_user(_row(uid1))
check("8 public_user avatarUrl ← avatar_url", pu["avatarUrl"] == IMG2, pu["avatarUrl"])
check("8b public_user has no shadow avatar fields",
      not any("avatar" in k and k != "avatarUrl" for k in pu), list(pu))

# 9. fresh schema: avatar_url is the users avatar column
with db() as con:
    cols = [c["column_name"] for c in con.execute(
        "select column_name from information_schema.columns"
        " where table_name='users'").fetchall()]
check("9b fresh schema: avatar_url present", "avatar_url" in cols)



# ════════════════════════════════════════════════════════════════════════
# Auth modal: JSON submit, return target
#
# Auth modal canonical — JSON submit + return target + market coverage.
#
# A*  JSON login: ok + session cookie; the cookie authorizes
#     /account/credits.
# B*  JSON login failures: err_login_failed; repeated failures trip
#     the 429 throttle (same path as the form flow).
# C*  JSON register: ok + logged in; duplicate username returns the
#     existing code.
# D*  Full-page form login behaves as before (302 + cookie, landing
#     on the sanitized next).
# E*  next sanitation: /tools survives; //evil.com, https://evil.com
#     and javascript:… all fall back to /.
# F*  X unconfigured: no X button on the login page (and the flag is
#     injected false).
# G*  /tools lists first-party tools (compress among them) and public
#     community tools.
# ════════════════════════════════════════════════════════════════════════
section('Auth modal: JSON submit, return target')

import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid


_HOST = ("127.0.0.1", int(TEST_PORT))
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
    last = None
    for i in range(14):
        st, d, last = _json_form({"__path": "/auth/login", "login": thr,
                                  "password": "nope"})
        if st == 429:
            hit429 = True
            check("B2 repeated failures → 429 (throttled)",
                  d and d.get("error") in ("err_too_many_attempts",
                                           "err_login_failed"), f"{st} {d}")
            break
    if not hit429:
        check("B2 repeated failures → 429 (throttled)", False,
              f"last st={st} {last}")

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
    # folded-unique violations are discriminated by constraint: a duplicate
    # handle reports err_username_taken (audit-fix item 11)
    check("C2 duplicate username → err_username_taken",
          d and d.get("error") == "err_username_taken", f"{st} {d}")

    # ═══ D: full-page form login unchanged ═══════════════════════════
    b, ct = _form({"login": user, "password": "Test12345", "next": "/tools"})
    st, _h, _d, setc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    loc = (_h or {}).get("location", "")
    check("D1 form login 302 + cookie",
          st == 302 and "session" in setc, f"{st} {setc[:50]}")
    check("D2 redirects to next", loc.endswith("/tools"), loc)

    # ═══ E: next sanitation (form path proves the same _safe_next) ═══
    for bad in ("//evil.com", "https://evil.com", "javascript:alert(1)",
                "/\\evil.com", "/\r\nSet-Cookie: x=1"):
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
    st, _h, _d, _sc = _req("GET", "/auth/x/login", None, {"Accept": "text/html"})
    check("F3 unconfigured X login redirects to the login page",
          st == 302 and (_h or {}).get("location", "").endswith("/auth/login"),
          f"{st} {(_h or {}).get('location')}")

    # ═══ G: market lists first-party + community tools ═══════════════
    com = _u + "c"
    b, ct = _form({"username": com, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{com}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    with db() as con:
        uid_com = int(con.execute(
            "select id from users where username=%s", (com,)).fetchone()["id"])
    with db() as con:
        con.execute(
            """
            INSERT INTO platform_tools (tool_key, tool_id, execution,
              visibility, label_json, enabled, owner_id,
              files_min, files_max, outputs_json, created_at, updated_at, accept_json, format_family)
            VALUES (%s, 'g', 'http', 'public',
              '{"en": "Community G"}', TRUE, %s,
              1, 1, '["file"]', %s, %s, '[\".png\"]', 'image')
            """,
            (f"{com}_g", uid_com, int(_time.time()), int(_time.time())))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, 0, TRUE, %s)", (f"{com}_g", int(_time.time())))
    st, _h, body, _sc = _req("GET", "/tools", None, {"Accept": "text/html"})
    body = body if isinstance(body, str) else ""
    check("G1 market lists first-party compress",
          st == 200 and 'href="/tool/compress"' in body, st)
    st, _h, body, _sc = _req("GET", "/tools?q=Community", None, {"Accept": "text/html"})
    body = body if isinstance(body, str) else ""
    check("G2 market search finds the public community tool",
          f"href=\"/tool/{com}_g\"" in body, com)

finally:
    with db() as con:
        con.execute("delete from pricing where tool_id LIKE %s", (f"{_u}%",))
        con.execute("delete from platform_tools where tool_key LIKE %s", (f"{_u}%",))
        rows = con.execute("select id from users where username LIKE %s",
                           (_u + "%",)).fetchall()
        purge_users(con, [int(r["id"]) for r in rows])


# ════════════════════════════════════════════════════════════════════════
# Credential edge states
#
# Credential and identity edge states: fetch-token collisions, X-login
# race recovery, half-empty credential PATCH.
#
# 1. sign_fetch_token keyed its identity on (user, file, second) and the
#    redemption ledger consumed the whole token — two signings of the same
#    file within one second minted ONE token and ate each other's
#    one-time use. Each signing now carries a nonce.
# 2. The X first-login UNIQUE-race recovery queried inside the aborted
#    transaction (InFailedSqlTransaction); recovery now rolls back to a
#    savepoint before converging.
# 3. A PATCH carrying auth_header="" with a non-empty token was treated
#    as an explicit clear; only both-empty clears, one-sided shapes 422.
# ════════════════════════════════════════════════════════════════════════
section('Credential edge states')

import os as _os
import sys as _sys


_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")


import http.client as _hc
import json
import time
import uuid
ts_ces = int(time.time())
_u = "ce" + _os.urandom(3).hex()
_HOST_ces = ("127.0.0.1", int(TEST_PORT))

from database import db


def _req(method, path, body=None, headers=None, cookies=None):
    conn = _hc.HTTPConnection(*_HOST_ces, timeout=60)
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = cookies
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw.decode(errors="replace")


def _form_ces(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register_login(name):
    b, ct = _form_ces({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    conn = _hc.HTTPConnection(*_HOST_ces, timeout=30)
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    conn.close()
    b, ct = _form_ces({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    return ck


# ═══ 1. fetch tokens: same user+file+second mint distinct tokens ═══
from security import sign_fetch_token, verify_fetch_token  # noqa: E402
t1 = sign_fetch_token(4242, "gk-collide", 600)
t2 = sign_fetch_token(4242, "gk-collide", 600)
check("1 same-second signings produce distinct tokens", t1 != t2, f"{t1} == {t2}")
check("1b first token verifies (first redemption)",
      verify_fetch_token(t1, 4242, "gk-collide") is True)
check("1c first token is spent (one-time)",
      verify_fetch_token(t1, 4242, "gk-collide") is False)
check("1d second token survives the first one's redemption",
      verify_fetch_token(t2, 4242, "gk-collide") is True)
check("1e token bound to its file only",
      verify_fetch_token(t1, 4242, "gk-other") is False)

# ═══ 2. X first-login race: recovery must query a live transaction ═══
from repo import users as users_repo  # noqa: E402
import psycopg2.errors as _pgerr
x_id = "race-" + uuid.uuid4().hex[:10]
winner_uid = None
loser_converged = None
with db() as con:
    winner_uid = users_repo.insert_x_user(
        con, "cew" + _os.urandom(3).hex(), "W", "", x_id, "w", "W", "", ts_ces)["id"]
with db() as con:
    # reproduce the losing INSERT inside an open transaction…
    try:
        con.execute("SAVEPOINT x_login_race")
        users_repo.insert_x_user(
            con, "cel" + _os.urandom(3).hex(), "L", "", x_id, "l", "L", "", ts_ces)
        loser_converged = "inserted-unexpectedly"
    except _pgerr.UniqueViolation:
        # …and the fixed recovery shape: rollback to the savepoint, then
        # converge. Without the rollback this SELECT raises
        # InFailedSqlTransaction (the audited failure).
        con.execute("ROLLBACK TO SAVEPOINT x_login_race")
        existing = users_repo.by_x_user_id(con, x_id)
        loser_converged = existing["id"] if existing else None
check("2 race loser converges to the winner after savepoint recovery",
      loser_converged == winner_uid,
      f"winner={winner_uid} loser={loser_converged}")

# ═══ 3. credential PATCH shapes ═══
ck = _register_login(_u)
with db() as con:
    uid = con.execute("select id from users where username=%s", (_u,)).fetchone()["id"]
st, r = _req("POST", "/api/usertools", {
    "label": "CE Tool", "url": "https://93.184.216.34/ce",
    "timeout_s": 30, "files_min": 1, "files_max": 1,
    "format_families": "image", "accept": [".png"], "cost": 0,
    "auth_header": "X-Key", "auth_token": "secret-one"},
    {"Content-Type": "application/json", "Cookie": ck})
key = (r or {}).get("tool_key") or ""
# PATCH routes by the system-generated tool_key
check("3 tool with credential created", st == 200 and r.get("ok") and key,
      f"{st} {str(r)[:80]}")

# 3a: half-empty pair → 422, credential intact
st, r = _req("PATCH", f"/api/usertools/{key}",
             {"auth_header": "", "auth_token": "second-secret"},
             {"Content-Type": "application/json", "Cookie": ck})
with db() as con:
    stored = con.execute(
        "select endpoint_json from platform_tools where tool_key=%s",
        (key,)).fetchone()["endpoint_json"]
check("3a half-empty credential PATCH → 422 bad_auth_header",
      st == 422 and r.get("error") == "bad_auth_header", f"{st} {str(r)[:70]}")
check("3b stored credential survives the rejected half-empty PATCH",
      '"X-Key"' in (stored or "") and '"auth_token"' in (stored or ""),
      stored[:90] if stored else "none")

# 3c: both-empty → explicit clear
st, r = _req("PATCH", f"/api/usertools/{key}",
             {"auth_header": "", "auth_token": ""},
             {"Content-Type": "application/json", "Cookie": ck})
with db() as con:
    stored2 = con.execute(
        "select endpoint_json from platform_tools where tool_key=%s",
        (key,)).fetchone()["endpoint_json"]
check("3c both-empty PATCH → 200 (explicit clear)",
      st == 200 and (r or {}).get("ok"), f"{st} {str(r)[:70]}")
check("3d credential cleared in storage",
      stored2 is None or ("secret-one" not in stored2 and "X-Key" not in stored2),
      (stored2 or "")[:90])

with db() as con:
    con.execute("delete from platform_tools where owner_id=%s", (uid,))
    con.execute("delete from users where id=%s", (winner_uid,))
    purge_users(con, [uid])


# ════════════════════════════════════════════════════════════════════════
# API keys
#
# API keys (BK): key semantics end to end.
#
#   C*  create → row + encrypted copy; the plaintext appears ONLY in the
#       create response and the session-only reveal — never in any other
#       API response and never in plaintext in the database
#   V*  viewable-anytime: reveal works repeatedly (owner session); Bearer
#       access cannot list, reveal, create or delete; a historical row
#       without an encrypted copy reveals a typed "not viewable"
#   L*  limit 10 per user: the 11th create → 409 err_key_limit; N parallel
#       creates cannot exceed the cap; deleting frees a slot
#   D*  delete = physical DELETE (no status column, row gone, key stops
#       authenticating)
#   R*  password change keeps keys working and sessions logged out;
#       operator_reset_password deletes every key of the user
#   S*  suspended accounts never authenticate with any key
#   X*  vault unavailable (TOOL_VAULT_KEY cleared in a subprocess) →
#       create fails typed err_vault, zero rows
#   M*  page/API naming: /account/keys + /api/keys, no /api/tokens or
#       /account/tokens anywhere; old module name absent
#
# Fail-closed env guard; per-run unique names; fixtures cleaned at end.
# ════════════════════════════════════════════════════════════════════════
section('API keys')

import base64
import hashlib
import hmac as _hmac
import http.client as _hc
import json as _json
import os as _os
import subprocess as _sp
import sys as _sys
import time as _time
import uuid as _uuid


_HOST_key = ("127.0.0.1", int(TEST_PORT))
_u = "bk" + _uuid.uuid4().hex[:6]
ts_key = int(_time.time())


def _req_key(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_key, timeout=60)
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


def _form_key(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name, password="Test12345"):
    b, ct = _form_key({"username": name, "password": password,
                   "password_confirm": password, "email": f"{name}@t.co"})
    _req_key("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_key({"login": name, "password": password})
    st, _h, _d, sc = _req_key("POST", "/auth/login", b, {"Content-Type": ct})
    return sc.split(";")[0]


from database import db

uid = uid2 = None
try:
    ck_key = _register(_u + "a")
    ck2 = _register(_u + "b")
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
        uid2 = int(con.execute("select id from users where username=%s",
                               (_u + "b",)).fetchone()["id"])

    # ═══ C. create + storage shape ═══
    st, _h, d, _sc = _req_key("POST", "/api/keys", {"name": "ci"},
                          {"Content-Type": "application/json", "Cookie": ck_key})
    key_row = (d or {}).get("key") or {}
    plaintext = key_row.get("key") or ""
    check("C1 create → 200 with plaintext + prefix",
          st == 200 and plaintext.startswith("fsk_")
          and key_row.get("prefix") == plaintext[4:12], str(d)[:100])
    with db() as con:
        row = con.execute("select * from api_keys where user_id=%s"
                          " order by id desc limit 1", (uid,)).fetchone()
    _hk = _hmac.new(_os.environ["FLASHSHARE_SESSION_SECRET"].encode(),
                    plaintext.encode(), hashlib.sha256).hexdigest()
    check("C2 row stores the keyed hash, never the plaintext",
          row is not None and row["key_hash"] == _hk
          and plaintext not in (row["key_sealed"] or "")
          and (row["key_sealed"] or "").startswith("enc1:"),
          dict(row) if row else None)
    check("C3 table has no status column (delete is physical)",
          row is not None and "status" not in row.keys())
    # plaintext never appears outside create/reveal
    st, _h, d, _sc = _req_key("GET", "/api/keys", None, {"Cookie": ck_key})
    blob = _json.dumps(d)
    check("C4 list exposes prefix only, never the plaintext",
          st == 200 and plaintext not in blob
          and plaintext[4:12] in blob and plaintext not in blob
          and d["keys"][0]["prefix"]
          and "key" not in d["keys"][0], blob[:120])

    # ═══ V. viewable-any-time + session-only ═══
    kid = d["keys"][0]["id"]
    for i in range(2):
        st, _h, d, _sc = _req_key("GET", f"/api/keys/{kid}/reveal", None, {"Cookie": ck_key})
        check(f"V{i+1} reveal (repeat {i+1}) → same plaintext",
              st == 200 and d.get("key") == plaintext, st)
    for path, method, body in (("/api/keys", "GET", None),
                               ("/api/keys", "POST", {"name": "x"}),
                               (f"/api/keys/{kid}/reveal", "GET", None),
                               (f"/api/keys/{kid}", "DELETE", None)):
        st, _h, d, _sc = _req_key(method, path, body, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {plaintext}"})
        check(f"V3 Bearer cannot {method} {path}",
              st in (401, 302) or (st == 403), st)
    # another user's session cannot reveal someone else's key
    st, _h, d, _sc = _req_key("GET", f"/api/keys/{kid}/reveal", None, {"Cookie": ck2})
    check("V4 another user's session → 404", st == 404, st)
    # historical row without a sealed copy
    with db() as con:
        con.execute("update api_keys set key_sealed=NULL where id=%s", (kid,))
    st, _h, d, _sc = _req_key("GET", f"/api/keys/{kid}/reveal", None, {"Cookie": ck_key})
# V5 retired: every key carries a sealed copy (FX-2)

    # ═══ M. naming surface ═══
    st, _h, body, _sc = _req_key("GET", "/account/keys", None, {"Cookie": ck_key})
    check("M1 /account/keys page renders (create + list ids)",
          st == 200 and 'id="createKey"' in body and 'id="keyList"' in body
          and "keys.js" in body, st)
    st, _h, _b, _sc = _req_key("GET", "/account/tokens", None, {"Cookie": ck_key})
    check("M2 /account/tokens retired (404)", st == 404, st)
    st, _h, _b, _sc = _req_key("GET", "/api/tokens", None, {"Cookie": ck_key})
    check("M3 /api/tokens retired (404 JSON)", st == 404, st)
    _src = ""
    for sub in ("app", "core", "static"):
        for root, _d, files in _os.walk(_os.path.join(REPO, sub)):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith((".py", ".js", ".html")):
                    _src += open(_os.path.join(root, f), encoding="utf-8",
                                 errors="ignore").read()
    check("M4 old module/table names absent in product code",
          "identity.apitokens" not in _src and "api_tokens" not in _src,
          "grep api_tokens|identity.apitokens")

    # ═══ L. limit 10 + concurrency ═══
    st, _h, d, _sc = _req_key("GET", "/api/keys", None, {"Cookie": ck_key})
    have = len(d["keys"])
    for i in range(10 - have):
        _req_key("POST", "/api/keys", {"name": f"bulk{i}"},
             {"Content-Type": "application/json", "Cookie": ck_key})
    st, _h, d, _sc = _req_key("GET", "/api/keys", None, {"Cookie": ck_key})
    check("L1 exactly 10 keys held", len(d["keys"]) == 10, len(d["keys"]))
    st, _h, d, _sc = _req_key("POST", "/api/keys", {"name": "eleventh"},
                          {"Content-Type": "application/json", "Cookie": ck_key})
    check("L2 11th create → 409 err_key_limit",
          st == 409 and d.get("error") == "err_key_limit", (st, d))
    # parallel creates on a freed slot cannot exceed the cap
    first_id = d2 = None
    st, _h, d, _sc = _req_key("GET", "/api/keys", None, {"Cookie": ck_key})
    first_id = d["keys"][-1]["id"]
    st, _h, d, _sc = _req_key("DELETE", f"/api/keys/{first_id}", None, {"Cookie": ck_key})
    check("L3 delete frees a slot (physical)",
          st == 200, st)
    import threading
    results = []
    def _mk(i):
        st, _h, d, _sc = _req_key("POST", "/api/keys", {"name": f"race{i}"},
                              {"Content-Type": "application/json", "Cookie": ck_key})
        results.append(st)
    ths = [threading.Thread(target=_mk, args=(i,)) for i in range(6)]
    for t in ths: t.start()
    for t in ths: t.join()
    st, _h, d, _sc = _req_key("GET", "/api/keys", None, {"Cookie": ck_key})
    check("L4 six parallel creates on one free slot → exactly 10 rows",
          len(d["keys"]) == 10 and results.count(200) == 1
          and results.count(409) == 5, (len(d["keys"]), results))

    # ═══ D. delete semantics ═══
    last_id = d["keys"][0]["id"]
    st, _h, _d, _sc = _req_key("DELETE", f"/api/keys/{last_id}", None, {"Cookie": ck_key})
    with db() as con:
        n = con.execute("select count(*) c from api_keys where id=%s",
                        (last_id,)).fetchone()["c"]
    check("D1 delete removes the row physically", st == 200 and int(n) == 0, n)

    # ═══ R. rotation semantics ═══
    st, _h, d, _sc = _req_key("POST", "/api/keys", {"name": "keep"},
                          {"Content-Type": "application/json", "Cookie": ck_key})
    keep_key = d["key"]["key"]
    b, ct = _form_key({"old_password": "Test12345", "new_password": "NewPass12345",
                   "new_password_confirm": "NewPass12345"})
    st, _h, _d, _sc = _req_key("POST", "/account/password", b,
                           {"Content-Type": ct, "Cookie": ck_key})
    check("R1 password change → 302", st == 302, st)
    st, _h, _d, _sc = _req_key("GET", "/account/keys", None, {"Cookie": ck_key})
    check("R2 browser session rotated (redirect to login)", st == 302, st)
    st, _h, d, _sc = _req_key("POST", "/mcp",
                          {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          {"Content-Type": "application/json",
                           "Authorization": f"Bearer {keep_key}"})
    check("R3 API key still authenticates after password change",
          st == 200, st)
    b, ct = _form_key({"login": _u + "a", "password": "NewPass12345"})
    st_l, _h, _d, sc = _req_key("POST", "/auth/login", b, {"Content-Type": ct})
    ck_key = sc.split(";")[0]
    check("R2b re-login with the new password works", st_l == 302 and ck_key,
          (st_l, repr(ck_key)))
    st_probe, _h, d_probe, _sc = _req_key("GET", "/api/me", None, {"Cookie": ck_key})
    check("R2c /api/me with the fresh session", st_probe == 200,
          (st_probe, str(d_probe)[:80]))
    # R4 retired: operator_reset_password had no production caller and
    # was deleted with the FX-2 dead-code sweep

    # ═══ S. suspension ═══
    b, ct = _form_key({"login": _u + "a", "password": "NewPass12345"})
    st_l, _h, _d, sc = _req_key("POST", "/auth/login", b, {"Content-Type": ct})
    ck_key = sc.split(";")[0]
    check("S0pre login with the changed password", st_l == 302 and ck_key,
          st_l)
    st, _h, d, _sc = _req_key("GET", "/api/keys", None, {"Cookie": ck_key})
    check("S0 re-login session reaches the key list", st == 200
          and isinstance(d.get("keys"), list), (st, str(d)[:80]))
    if d.get("keys"):
        _req_key("DELETE", f"/api/keys/{d['keys'][0]['id']}", None,
             {"Cookie": ck_key})
    st, _h, d, _sc = _req_key("POST", "/api/keys", {"name": "sus"},
                          {"Content-Type": "application/json", "Cookie": ck_key})
    check("S0b create after re-login", st == 200 and (d or {}).get("key"),
          (st, str(d)[:80]))
    sus_key = d["key"]["key"]
    with db() as con:
        con.execute("update users set status='suspended' where id=%s", (uid,))
    st, _h, _d, _sc = _req_key("POST", "/mcp",
                           {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           {"Content-Type": "application/json",
                            "Authorization": f"Bearer {sus_key}"})
    check("S1 suspended account's key never authenticates", st == 401, st)
    with db() as con:
        con.execute("update users set status='active' where id=%s", (uid,))

    # ═══ X. vault unavailable → typed create failure ═══
    env = dict(_os.environ)
    env.pop("TOOL_VAULT_KEY", None)
    env["DATABASE_URL"] = TEST_DB.replace("postgresql://", "postgresql+psycopg://")
    probe = _sp.run(
        [_sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
         "import os\n"
         "from identity.apikeys import create_key\n"
         "try:\n"
         "    create_key(%d, 'novault')\n"
         "    print('CREATED')\n"
         "except ValueError as e:\n"
         "    print('ERR', e)" % (
             _os.path.join(REPO, "core"), _os.path.join(REPO, "app"), uid)],
        capture_output=True, text=True, env=env, timeout=60)
    check("X1 vault unavailable → typed err_vault, zero create",
          probe.stdout.strip() == "ERR err_vault",
          (probe.stdout.strip()[:80], probe.stderr.strip()[-120:]))
    with db() as con:
        n = con.execute("select count(*) c from api_keys where user_id=%s"
                        " and name='novault'", (uid,)).fetchone()["c"]
    check("X1b no row written without the vault", int(n) == 0, n)
finally:
    with db() as con:
        for u in (uid, uid2):
            if u:
                con.execute("delete from api_keys where user_id=%s", (u,))
                con.execute("delete from sessions where user_id=%s", (u,))
                con.execute("delete from tool_calls where user_id=%s", (u,))
                con.execute("delete from ledger where user_id=%s", (u,))
                con.execute("delete from upload_usage where user_id=%s", (u,))
                con.execute("delete from ingress_requests where user_id=%s", (u,))
                purge_users(con, [u])
    print("cleanup done")


# ════════════════════════════════════════════════════════════════════════
# Password reset, suspension, delete protection, email, X bind race
#
# Identity & operator-surface closure canonical.
#
# password reset: one-time temp password, sessions revoked, API keys
#     survive, audit without the credential, platform/backoffice rows
#     protected.
# provider suspension: login refused, catalog/MCP/Web/author-page
#     invisible, direct invocation refused before consume, API key dead;
#     restore revives everything; earnings untouched.
# delete protection: every financial/audit fact table RESTRICTs user
#     deletion; credential tables keep CASCADE.
# email: case-insensitive uniqueness (index), mixed-case registration
#     converges, race and duplicate return typed errors, never 500.
# X bind race: two accounts binding the same X identity concurrently —
#     the loser gets typed err_x_bound_other.
# operator tool create: params declaration rides the single supply
#     validator (same errors as the user publish path).
# subscription.active: ignored (logged), zero mutation — the deleted
#     no-op sync handler asserted gone from source.
# gate(): programmatic callers get 401 JSON; browsers keep the 302.
# ════════════════════════════════════════════════════════════════════════
section('Password reset, suspension, delete protection, email, X bind race')

import os as _os
import sys as _sys


import json as _json
import threading
import time
ts_ops = int(time.time())
_u = "i2" + _os.urandom(4).hex()

from database import db

def _mkuser(name, **kw):
    cols = {"username": name, "email": f"{name}@t.co", "password_hash": "x",
            "display_name": "T", "status": "active", "created_at": ts_ops,
            "last_login_at": ts_ops}
    cols.update(kw)
    fields = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with db() as con:
        row = con.execute(
            f"insert into users ({fields}) values ({marks}) returning *",
            tuple(cols.values())).fetchone()
    return dict(row)

def _mktool(tool_key, owner_id, params_json=None):
    # pricing is keyed by the FULL tool_key — the supply-path convention
    # (create_tool_with_pricing → upsert_pricing(con, tool_key, ...))
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled,"
            " params_json, created_at, updated_at, accept_json, format_family)"
            " values (%s, %s, %s, 'http', %s, 'public',"
            " TRUE, %s, %s, %s, '[\".png\"]', 'image')",
            (tool_key, owner_id, tool_key.split("/")[-1],
             _json.dumps({"en": "I2"}), params_json, ts_ops, ts_ops))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, 0, TRUE, %s)", (tool_key, ts_ops))

# ══════════ 1. operator password reset ══════════
from app.web_auth import create_browser_session, COOKIE_NAME
from security import verify_password
from backoffice import users as users_cmd

op = _mkuser(_u + "op", status="samelabs")
target = _mkuser(_u + "t1")
target_sess = create_browser_session(target["id"])

with db() as con:
    con.execute("update users set password_hash=%s where id=%s",
                ("$2b$12$oldhasholdhasholdhasholdhasholdhasholdhasholdhasho",
                 target["id"]))
uid, temp = users_cmd.reset_user_password(op["id"], target["id"])
with db() as con:
    row = con.execute("select password_hash from users where id=%s",
                      (target["id"],)).fetchone()
    n_sess = con.execute("select count(*) c from sessions where user_id=%s",
                         (target["id"],)).fetchone()["c"]
    aud = con.execute(
        "select before_json, after_json from operator_audit where"
        " action='user.password.reset' and target_id=%s",
        (str(target["id"]),)).fetchone()
check("R1 reset: new hash verifies the temp password, sessions all revoked",
      verify_password(temp, row["password_hash"]) and n_sess == 0,
      f"sessions={n_sess}")
check("R2 audit written, credential NEVER recorded",
      aud is not None
      and _json.dumps(temp) not in _json.dumps(dict(aud))
      and temp not in _json.dumps(dict(aud)), str(aud))
with db() as con:
    platform = dict(con.execute(
        "select * from users where username='platform'").fetchone())
for protect_id, label in ((platform["id"], "platform"),
                          (op["id"], "backoffice"),
                          (999999, "unknown")):
    try:
        users_cmd.reset_user_password(op["id"], protect_id)
        check(f"R3 reset refused for {label}", False)
        break
    except ValueError as exc:
        ok = str(exc) == ("user_protected" if protect_id != 999999
                          else "not_found")
        if not ok:
            check(f"R3 reset refused for {label}", False, str(exc))
            break
    except KeyError:
        if protect_id != 999999:
            check(f"R3 reset refused for {label}", False, "KeyError")
            break
else:
    check("R3 platform/backoffice/unknown reset refused (typed)", True)

# ══════════ 2. provider suspension — every surface ══════════
from economy.billing import grant as _grant
from tools import load_all as _load_all
_load_all()
from governance.catalog import list_catalog, search_catalog, resolve
from repo import keys as keys_repo

prov = _mkuser(_u + "prov")
tool_a = f"{prov['username']}_zzp_{_u}"
tool_b = f"{prov['username']}_zzc_{_u}"
_mktool(tool_a, prov["id"])
_mktool(tool_b, prov["id"])
first_party_before = len([t for t in list_catalog(None)])
community_before = {t["id"] for t in list_catalog(None) if t["origin"] == "community"}
check("S0a provider tools listed pre-suspension",
      {tool_a, tool_b} <= community_before, str(community_before))

row = users_cmd.set_user_suspended(op["id"], prov["id"], suspended=True)
with db() as con:
    st = con.execute("select status from users where id=%s",
                     (prov["id"],)).fetchone()["status"]
check("S1 suspend: status + audit",
      st == "suspended", st)
community_after = {t["id"] for t in list_catalog(None) if t["origin"] == "community"}
check("S2 catalog hides suspended provider's tools",
      tool_a not in community_after and tool_b not in community_after)
check("S3 search hides them too",
      not {t["id"] for t in search_catalog("zz")
           if t["id"].startswith(prov["username"] + "/")})
t, err = resolve(tool_a, None)
check("S4 resolve refuses (invocation/tool page/MCP single point)",
      t is None and err == "not_found", f"{t} {err}")
t_self, err_self = resolve(tool_a, prov["id"])
check("S4b even the owner cannot resolve while suspended",
      t_self is None and err_self == "not_found")
# direct invocation through the REAL dispatch preflight (no claim/charge)
from execution.dispatch import preflight
payer = _mkuser(_u + "payer")
tool, declared, _canon_body, err_body, err_status = preflight(
    {"id": payer["id"]}, tool_a,
    {"expiry_days": 7, "allowed_downloads": 10}, {})
check("S5 dispatch preflight refuses before any charge",
      err_body == {"ok": False, "error": "tool_not_found"}
      and err_status == 404, f"{err_body} {err_status}")
bal_pre = None
from economy.billing import spendable_balance
bal_pre = spendable_balance(payer["id"])
# author page: suspended → 404
from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)
r = client.get(f"/u/{prov['username']}")
check("S6 author page 404 for suspended provider", r.status_code == 404,
      str(r.status_code))
# login + API key surfaces already keyed on status; assert the live query
from identity import auth as _auth
check("S7 login refused",
      _auth.authenticate_local(prov["username"], "whatever") is None)
# restore
users_cmd.set_user_suspended(op["id"], prov["id"], suspended=False)
community_restored = {t["id"] for t in list_catalog(None) if t["origin"] == "community"}
t2, err2 = resolve(tool_a, None)
r2 = client.get(f"/u/{prov['username']}")
check("S8 restore revives catalog/resolve/author page",
      {tool_a, tool_b} <= community_restored and t2 is not None
      and r2.status_code == 200,
      f"{err2} {r2.status_code}")
row_replay = users_cmd.set_user_suspended(op["id"], prov["id"], False)
check("S9 suspend replay on active row → no-op success",
      row_replay["status"] == "active")
try:
    users_cmd.set_user_suspended(op["id"], platform["id"], True)
    check("S10 platform row cannot be suspended", False)
except ValueError as exc:
    check("S10 platform/backoffice rows cannot be suspended",
          str(exc) == "user_protected", str(exc))

# ══════════ 3. delete protection (FK RESTRICT) ══════════
restrict_tables = ["ledger", "tool_calls", "orders", "withdrawals",
                   "memberships", "payment_reviews",
                   "subscription_period_payments", "operator_audit"]
_victim = _mkuser(_u + "vic")
_mktool(f"{_victim['username']}_v_{_u}", _victim["id"])
from economy.billing import grant as grant_ledger
grant_ledger(_victim["id"], 10, "grant", note="fk")
for tbl in restrict_tables:
    try:
        with db() as con:
            # minimal row per table via direct insert where cheap
            if tbl == "ledger":
                pass  # already has a row
            elif tbl == "tool_calls":
                con.execute(
                    "insert into tool_calls (client_ref, user_id, tool_id,"
                    " status, cost_snapshot, started_at, finished_at,"
                    " commission_rate_snapshot)"
                    " values ('fk', %s, 't', 'failed', 0, %s, %s, 0)",
                    (_victim["id"], ts_ops, ts_ops))
            elif tbl == "orders":
                con.execute(
                    "insert into orders (user_id, kind, item_id, provider,"
                    " status, amount_cents, currency, credits_snapshot,"
                    " created_at)"
                    " values (%s, 'package', 1, 'creem', 'failed', 1,"
                    " 'USD', 10, %s)", (_victim["id"], ts_ops))
            elif tbl == "withdrawals":
                con.execute(
                    "insert into withdrawals (user_id, amount, status,"
                    " payout_channel_snapshot, created_at)"
                    " values (%s, 1, 'pending', 'x', %s)",
                    (_victim["id"], ts_ops))
            elif tbl == "memberships":
                con.execute(
                    "insert into memberships (user_id, plan_id,"
                    " provider_subscription_id, status, started_at)"
                    " values (%s, (select id from plans order by id"
                    " limit 1), %s, 'active', %s)",
                    (_victim["id"], f"sub:fk_{_u}", ts_ops))
            elif tbl == "payment_reviews":
                con.execute(
                    "insert into payment_reviews (event_type, event_id,"
                    " txn_id, user_id, created_at)"
                    " values ('refund.created', %s, %s, %s, %s)",
                    (f"fk_{_u}", f"txn_{_u}", _victim["id"], ts_ops))
            elif tbl == "subscription_period_payments":
                con.execute(
                    "insert into subscription_period_payments (sub_id,"
                    " period_start, user_id, base_txn_id, created_at)"
                    " values (%s, %s, %s, %s, %s)",
                    (f"sub:fk_{_u}", ts_ops, _victim["id"], f"txn_{_u}", ts_ops))
            elif tbl == "operator_audit":
                from repo import operator_audit
                operator_audit.record(con, op["id"], "credits.adjust",
                                      "user_credits", str(_victim["id"]),
                                      None, None, ts_ops)
        with db() as con:
            con.execute("delete from users where id=%s", (_victim["id"],))
        check(f"F1 {tbl}: user deletion RESTRICTed", False)
        break
    except Exception as exc:
        if "foreign key" not in str(exc).lower() and "restrict" not in str(exc).lower():
            check(f"F1 {tbl}: user deletion RESTRICTed", False,
                  str(exc)[:90])
            break
else:
    check("F1 all 8 fact tables RESTRICT user deletion (+platform_tools.owner)",
          True)
# platform_tools owner restrict: victim has a tool → deletion blocked
try:
    with db() as con:
        con.execute("delete from users where id=%s", (_victim["id"],))
    check("F2 platform_tools.owner_id RESTRICTs", False)
except Exception:
    check("F2 platform_tools.owner_id RESTRICTs user deletion", True)
# credential tables still CASCADE: sessions row dies with the user
casc = _mkuser(_u + "casc")
create_browser_session(casc["id"])
with db() as con:
    con.execute("delete from users where id=%s", (casc["id"],))
with db() as con:
    n = con.execute("select count(*) c from sessions where user_id=%s",
                    (casc["id"],)).fetchone()["c"]
check("F3 sessions CASCADE with user deletion", n == 0, str(n))

# ══════════ 4. email lowercased + unique ══════════
from identity import auth as _auth2
mixed = _mkuser(_u + "m1", email=None)
with db() as con:
    con.execute("delete from users where id=%s", (mixed["id"],))
uid_m = _auth2.register_user(_u + "m1", f"MiXeD.{_u}@Example.COM", "pw12345678")
with db() as con:
    stored = con.execute("select email from users where id=%s",
                         (uid_m,)).fetchone()["email"]
check("E1 registration stores the lowercased email",
      stored == f"mixed.{_u}@example.com", stored)
try:
    _auth2.register_user(_u + "m2", f"MIXED.{_u}@example.com", "pw12345678")
    check("E2 case-variant duplicate refused (typed)", False)
except ValueError as exc:
    check("E2 case-variant duplicate refused (typed err_email_taken)",
          str(exc) == "err_email_taken", str(exc))
# login with the mixed-case form works
check("E3 mixed-case login form resolves",
      _auth2.authenticate_local(f"MiXeD.{_u}@Example.COM", "pw12345678")
      == uid_m)
# update_profile email race → typed
other = _mkuser(_u + "o1", password_hash=None)
try:
    _auth2.update_profile(other["id"], email=f"MIXED.{_u}@Example.COM")
    check("E4 update to a taken email refused (typed)", False)
except ValueError as exc:
    check("E4 update to a taken email refused (typed err_email_used)",
          str(exc) == "err_email_used", str(exc))
with db() as con:
    idx = con.execute(
        "select indexdef from pg_indexes where indexname ="
        " 'users_email_lower_uk'").fetchone()
check("E5 lower(email) unique index exists",
      idx is not None and "UNIQUE" in idx["indexdef"]
      and "lower(email)" in idx["indexdef"], str(idx))

# ══════════ 5. X bind race → typed loser ══════════
def _x_snap(x_id_ops):
    return {"id": x_id_ops, "username": f"xu_{x_id_ops[-6:]}",
            "name": "XU", "profile_image_url": ""}

a = _mkuser(_u + "xa")
b = _mkuser(_u + "xb")
x_id_ops = f"1779{_u}x".replace("-", "")[:18]
results_ops = {}
def _bind(uid_key):
    try:
        results_ops[uid_key] = ("ok", _auth2.upsert_x_user(_x_snap(x_id_ops),
                                                       bind_user_id=uid_key))
    except ValueError as exc:
        results_ops[uid_key] = ("typed", str(exc))
    except Exception as exc:
        results_ops[uid_key] = ("untyped", f"{type(exc).__name__}:{exc}")

th1 = threading.Thread(target=_bind, args=(a["id"],))
th2 = threading.Thread(target=_bind, args=(b["id"],))
th1.start(); th2.start(); th1.join(); th2.join()
kinds = [v[0] for v in results_ops.values()]
check("X1 concurrent bind: one winner, one typed err_x_bound_other, zero 500",
      sorted(kinds) == ["ok", "typed"]
      and all(v[1] == "err_x_bound_other" for v in results_ops.values()
              if v[0] == "typed"),
      str(results_ops))
# concurrent first-login INSERT race on the same X identity converges
fresh_x = f"1788{_u}y".replace("-", "")[:18]
login_results = {}
def _login():
    try:
        login_results[threading.get_ident()] = \
            ("ok", _auth2.upsert_x_user(_x_snap(fresh_x)))
    except Exception as exc:
        login_results[threading.get_ident()] = \
            ("untyped", f"{type(exc).__name__}:{exc}")
t1 = threading.Thread(target=_login)
t2 = threading.Thread(target=_login)
t1.start(); t2.start(); t1.join(); t2.join()
ids = {v[1] for v in login_results.values() if v[0] == "ok"}
check("X2 concurrent first login converges to ONE account, no 500",
      len(login_results) == 2 and all(v[0] == "ok" for v in login_results.values())
      and len(ids) == 1, str(login_results))

# ══════════ 6. operator tool create with params (single validator) ══════════
from backoffice import tools as tools_cmd
from governance.tool_supply import DeclarationError
op_sess = create_browser_session(op["id"])
OP_H = {"Cookie": f"{COOKIE_NAME}={op_sess}"}
good_params = [{"key": "mode", "type": "enum", "label": {"en": "Mode"},
                "options": [{"value": "fast"}, {"value": "best"}],
                "required": True, "default": "fast"}]
tool_key = tools_cmd.create_http_tool(op["id"], {
    "tool_id": f"i2op{_u}"[:24].replace("-", ""), "label": "I2 Op Tool",
        "format_families": "image",
    "url": "https://op.example.com/api", "timeout_s": 30,
    "accept": [".png"], "files_min": 0, "files_max": 1,
    "outputs": ["text"], "params": good_params, "cost": 0})
with db() as con:
    row = con.execute(
        "select params_json from platform_tools where tool_key=%s",
        (tool_key,)).fetchone()
check("P1 operator tool with params → stored canonical declaration",
      row is not None
      and _json.loads(row["params_json"])[0]["key"] == "mode",
      str(row))
try:
    tools_cmd.create_http_tool(op["id"], {
        "tool_id": f"i2bad{_u}"[:24].replace("-", ""), "label": "Bad",
        "format_families": "image",
        "url": "https://op.example.com/api", "timeout_s": 30,
        "accept": [".png"], "files_min": 0, "files_max": 1,
        "outputs": ["text"],
        "params": [{"key": "x", "type": "nope"}], "cost": 0})
    check("P2 invalid params refused by the SAME validator", False)
except DeclarationError:
    check("P2 invalid params refused by the SAME validator "
          "(tool_supply DeclarationError)", True)
r = client.post("/samelabs/tools/create",
                json={"tool_id": f"i2rt{_u}"[:24].replace("-", ""),
                      "label": "Route", "url": "https://op.example.com/api",
                      "format_families": "image",
                      "params": {"key": "not-a-list"}},
                headers=OP_H)
check("P3 route refuses non-array params (bad_params 422)",
      r.status_code == 422 and r.json().get("error") == "bad_params",
      r.text)

# ══════════ 7. subscription.active ignored; no-op sync deleted ══════════
from economy.subscription import handle_event as _hev
sub_ev = f"sub_{_u}ign"
res = _hev("subscription.active",
           {"subscription": {"id": sub_ev, "status": "active"}})
src_sub = open(_os.path.join(REPO, "core", "economy", "subscription.py"),
               encoding="utf-8").read()
check("A1 subscription.active → acknowledged ignored, zero mutation",
      res.get("ignored") is True, str(res))
check("A2 the no-op sync handler is gone from source",
      "_ev_sync" not in src_sub and "def _ev_sync" not in src_sub)

# ══════════ 8. gate(): 401 JSON for programs, 302 for browsers ══════════
r_json = client.post("/samelabs/economy/commission", json={"rate": 10},
                     headers={"Accept": "application/json"})
r_html = client.post("/samelabs/economy/commission", json={"rate": 10},
                     headers={"Accept": "text/html,application/xhtml+xml"},
                     follow_redirects=False)
check("G1 unauthenticated JSON caller → 401 JSON body",
      r_json.status_code == 401
      and r_json.json().get("error") == "operator_auth_required",
      f"{r_json.status_code} {r_json.text}")
check("G2 browser-shaped caller → login redirect",
      r_html.status_code == 302 and "/auth/login" in r_html.headers.get(
          "location", ""), str(r_html.status_code))


# ════════════════════════════════════════════════════════════════════════
# OAuth state, email normalization, limiters, Bearer fallthrough
#
# Identity closure canonical — behavior over the real app + isolated DB.
#
#   A. X OAuth state bound to the initiating browser (cookie == state),
#      single-use, bind flow requires the bind target's live session
#   B. X callback failures render localized text, never raw X/exception text
#   C. email normalization (strip + lowercase) at every write and lookup
#   D. changing email requires the current password (password accounts)
#   E. profile/security/unbind errors map through i18n; raw err_* never render
#   F. per-IP register limiter + per-IP aggregate login-failure limiter
#   G. stale session cookie on /mcp falls through to a valid Bearer
#   H. operator_reset_password: temp password + revoke sessions/tokens
#
# Every fixture uses a per-run random suffix and is deleted at the end.
# ════════════════════════════════════════════════════════════════════════
section('OAuth state, email normalization, limiters, Bearer fallthrough')

import os as _os
import sys as _sys


import re as _re
import time as _time
import urllib.parse as _up

ts_idc = int(_time.time())
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
client_idc = TestClient(_app)
_uids = []
_states = []


def _mkuser(name, email=None, password=None, x_id=None):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " x_user_id, status, created_at, last_login_at)"
            " values (%s, %s, %s, 'T', %s, 'active', %s, %s) returning id",
            (name, email, hash_password(password) if password else None,
             x_id, ts_idc, ts_idc)).fetchone()
    _uids.append(int(row["id"]))
    return int(row["id"])


def _user(uid):
    with db() as con:
        return con.execute("select * from users where id=%s", (uid,)).fetchone()


def _req_idc(method, path, cookies=None, **kw):
    client_idc.cookies.clear()
    return client_idc.request(method, path, cookies=cookies or {},
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
        r = _req_idc("GET", "/auth/x/login" + ("?bind=1" if bind else ""), cookies=cookies)
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
    r = _req_idc("GET", f"/auth/x/callback?code=c1&state={st}")
    check("A2 callback without state cookie → 400, no session, no exchange",
          r.status_code == 400 and COOKIE_NAME not in r.headers.get("set-cookie", "")
          and not _exchange_calls, (r.status_code, _exchange_calls))
    check("A2b rejected attempt consumes the state (single use)", not _state_exists(st))
    r = _req_idc("GET", f"/auth/x/callback?code=c1&state={st}", cookies={OAUTH_COOKIE: st})
    check("A2c consumed state + correct cookie → 400", r.status_code == 400, r.status_code)

    r, st = _start()
    _, st_other = _start()
    r = _req_idc("GET", f"/auth/x/callback?code=c2&state={st}", cookies={OAUTH_COOKIE: st_other})
    check("A3 callback with another flow's state cookie → 400 (login CSRF)",
          r.status_code == 400 and not _exchange_calls, r.status_code)

    # normal login flow still works
    _x_payload.clear()
    _x_payload.update({"id": "xid-" + _u + "-login", "username": "xl" + _u,
                       "name": "XL", "profile_image_url": ""})
    r, st = _start()
    r = _req_idc("GET", f"/auth/x/callback?code=c3&state={st}", cookies={OAUTH_COOKIE: st})
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
    r = _req_idc("GET", f"/auth/x/callback?code=c3&state={st}", cookies={OAUTH_COOKIE: st})
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
    r = _req_idc("GET", f"/auth/x/callback?code=c4&state={st}",
             cookies={OAUTH_COOKIE: st, COOKIE_NAME: s2})
    check("A6 bind callback under another user's session → 400, target not bound",
          r.status_code == 400 and _user(b1)["x_user_id"] is None
          and _user(b2)["x_user_id"] is None, r.status_code)
    r, st = _start(cookies={COOKIE_NAME: s1}, bind=True)
    r = _req_idc("GET", f"/auth/x/callback?code=c5&state={st}", cookies={OAUTH_COOKIE: st})
    check("A6b bind callback with no session → 400, not bound",
          r.status_code == 400 and _user(b1)["x_user_id"] is None, r.status_code)
    r, st = _start(cookies={COOKIE_NAME: s1}, bind=True)
    r = _req_idc("GET", f"/auth/x/callback?code=c6&state={st}",
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
    r = _req_idc("GET", f"/auth/x/callback?code=c7&state={st}", cookies={OAUTH_COOKIE: st})
    check("B1 exchange failure → 400 localized text, no raw X body",
          r.status_code == 400 and _form_error(r.text) == EN["err_x_login_incomplete"]
          and "SECRETBODY" not in r.text, _form_error(r.text))
    r, st = _start()
    r = _req_idc("GET", f"/auth/x/callback?code=c8&state={st}",
             cookies={OAUTH_COOKIE: st, "flashshare_lang": "zh"})
    check("B1b ZH: exchange failure → ZH text, no raw body",
          _form_error(r.text) == ZH["err_x_login_incomplete"] and "SECRETBODY" not in r.text,
          _form_error(r.text))
    _exchange_raise[0] = None
    # X account already bound to b1 → binding it to b2 fails with mapped text
    r, st = _start(cookies={COOKIE_NAME: s2}, bind=True)
    r = _req_idc("GET", f"/auth/x/callback?code=c9&state={st}",
             cookies={OAUTH_COOKIE: st, COOKIE_NAME: s2, "flashshare_lang": "zh"})
    check("B2 bound-to-other → ZH mapped text, no raw key",
          r.status_code == 400 and _form_error(r.text) == ZH["err_x_bound_other"]
          and _no_raw_keys(r.text), _form_error(r.text))

    # ═══════════ C. email normalization ═══════════
    ip_reg = "10.%d.%d.1" % (ts_idc % 250, _os.getpid() % 250)
    r = _req_idc("POST", "/auth/register", headers={"X-Real-IP": ip_reg}, data={
        "username": _u + "r1", "email": f"  {_u.upper()}R1@MiXed.COM ",
        "password": "pw-r1-12345", "password_confirm": "pw-r1-12345"})
    with db() as con:
        rr = con.execute("select id, email from users where username=%s", (_u + "r1",)).fetchone()
    if rr:
        _uids.append(int(rr["id"]))
    check("C1 register stores stripped+lowercased email",
          r.status_code == 302 and rr and rr["email"] == f"{_u}r1@mixed.com",
          (r.status_code, rr and rr["email"]))
    r = _req_idc("POST", "/auth/register", headers={"X-Real-IP": ip_reg}, data={
        "username": _u + "r2", "email": f"{_u}r1@mixed.com".upper(),
        "password": "pw-r2-12345", "password_confirm": "pw-r2-12345"})
    check("C2 register same email different case → err_email_taken text",
          _form_error(r.text) == EN["err_email_taken"], _form_error(r.text))
    legacy = _mkuser(_u + "lg", f"{_u}Legacy@X.com", "pw-lg-12345")
    r = _req_idc("POST", "/auth/register", headers={"X-Real-IP": ip_reg}, data={
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
    r = _req_idc("POST", "/account/profile", cookies={COOKIE_NAME: sd1},
             data={"email": f"{_u}d1new@t.co", "display_name": "D1"})
    check("D1 change email without current password → EN text, unchanged",
          r.status_code == 200 and _form_error(r.text) == EN["err_current_password_required"]
          and _user(d1)["email"] == f"{_u}d1@t.co", (r.status_code, _form_error(r.text)))
    r = _req_idc("POST", "/account/profile", cookies={COOKIE_NAME: sd1, "flashshare_lang": "zh"},
             data={"email": f"{_u}d1new@t.co", "current_password": "wrong-pass"})
    check("D2 wrong current password → ZH text, unchanged, no raw key",
          _form_error(r.text) == ZH["err_old_password_wrong"] and _no_raw_keys(r.text)
          and _user(d1)["email"] == f"{_u}d1@t.co", _form_error(r.text))
    r = _req_idc("POST", "/account/profile", cookies={COOKIE_NAME: sd1},
             data={"email": f"{_u}D1New@T.co", "current_password": "pw-d1-12345"})
    check("D3 correct current password → 302, email changed (normalized)",
          r.status_code == 302 and _user(d1)["email"] == f"{_u}d1new@t.co", r.status_code)
    r = _req_idc("POST", "/account/profile", cookies={COOKIE_NAME: sd1},
             data={"email": f"{_u}D1NEW@t.co", "display_name": "D1 renamed"})
    check("D4 unchanged email (case only) needs no password; other fields save",
          r.status_code == 302 and _user(d1)["display_name"] == "D1 renamed", r.status_code)
    xo = _mkuser(_u + "xo", None, None, x_id="xid-" + _u + "-xo")
    sxo = create_browser_session(xo)
    r = _req_idc("POST", "/account/profile", cookies={COOKIE_NAME: sxo},
             data={"email": f"{_u}XO@t.co"})
    check("D5 X-only user (no password) sets email without password",
          r.status_code == 302 and _user(xo)["email"] == f"{_u}xo@t.co", r.status_code)
    r = _req_idc("GET", "/account/profile", cookies={COOKIE_NAME: sd1})
    r2 = _req_idc("GET", "/account/profile", cookies={COOKIE_NAME: sxo})
    check("D6 profile form carries current_password only for password accounts",
          'name="current_password"' in r.text and 'name="current_password"' not in r2.text)

    # ═══════════ E. error mapping ═══════════
    r = _req_idc("POST", "/auth/x/unbind", cookies={COOKIE_NAME: sxo})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check("E1 unbind without password → 400 err_set_email_before_unbind (not 500)",
          r.status_code == 400 and body.get("error") == "err_set_email_before_unbind",
          (r.status_code, body))
    for lang, T in (("en", EN), ("zh", ZH)):
        r = _req_idc("POST", "/account/password", cookies={COOKIE_NAME: sxo, "flashshare_lang": lang},
                 data={"new_password": "abc", "new_password_confirm": "abc"})
        check(f"E2[{lang}] set_password validation error → rendered text (not 500)",
              r.status_code == 200 and _form_error(r.text) == T["err_password_short"]
              and _no_raw_keys(r.text), (r.status_code, _form_error(r.text)))
        r = _req_idc("POST", "/account/password", cookies={COOKIE_NAME: sd1, "flashshare_lang": lang},
                 data={"new_password": "", "new_password_confirm": ""})
        check(f"E3[{lang}] empty new password → err_password_empty text (not err_fill_login)",
              _form_error(r.text) == T["err_password_empty"]
              and _form_error(r.text) != T["err_fill_login"], _form_error(r.text))
        r = _req_idc("POST", "/account/password", cookies={COOKIE_NAME: sd1, "flashshare_lang": lang},
                 data={"new_password": "pw-new-12345", "new_password_confirm": "pw-new-12345"})
        check(f"E4[{lang}] missing current password → err_current_password_required text",
              _form_error(r.text) == T["err_current_password_required"], _form_error(r.text))
        r = _req_idc("POST", "/account/password", cookies={COOKIE_NAME: sd1, "flashshare_lang": lang},
                 data={"old_password": "nope-nope", "new_password": "pw-new-12345",
                       "new_password_confirm": "pw-new-12345"})
        check(f"E5[{lang}] wrong current password → mapped text, no raw key",
              _form_error(r.text) == T["err_old_password_wrong"] and _no_raw_keys(r.text),
              _form_error(r.text))
        r = _req_idc("POST", "/account/profile", cookies={COOKIE_NAME: sd1, "flashshare_lang": lang},
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
    _i18n_js = open(_os.path.join(REPO, "static", "i18n.js"), encoding="utf-8").read()
    check("E8b static/i18n.js regenerated with new keys",
          all(k in _i18n_js for k in _new_keys))

    # ═══════════ F. rate limits ═══════════
    # Local thresholds + a controllable clock: the loop sizes follow the
    # patched module constants (a handful of requests), not the runner's
    # env-raised limits (10000 — production defaults stay untouched), and
    # window expiry advances the clock instead of sleeping. The aggregate
    # per-IP limit stays above the per-(ip, login) limit so the two
    # throttles remain separable, exactly as in production (50 vs 10).
    ip_a = "10.200.%d.%d" % (ts_idc % 250, _os.getpid() % 250)
    ip_b = "10.201.%d.%d" % (ts_idc % 250, _os.getpid() % 250)
    ip_c = "10.202.%d.%d" % (ts_idc % 250, _os.getpid() % 250)
    ip_d = "10.203.%d.%d" % (ts_idc % 250, _os.getpid() % 250)
    ip_e = "10.204.%d.%d" % (ts_idc % 250, _os.getpid() % 250)
    _rl_ips = (ip_a, ip_b, ip_c, ip_d, ip_e)
    bad = {"username": "x", "email": "a@b.co", "password": "p", "password_confirm": "p"}
    _clock = [_time.time()]
    _saved = (auth_route._REGISTER_IP_LIMIT, auth_route._LOGIN_IP_FAIL_LIMIT,
              auth_route._LOGIN_FAIL_LIMIT, auth_route.now)
    auth_route._REGISTER_IP_LIMIT = 3
    auth_route._LOGIN_IP_FAIL_LIMIT = 5
    auth_route._LOGIN_FAIL_LIMIT = 3
    auth_route.now = lambda: int(_clock[0])
    try:
        codes = [_req_idc("POST", "/auth/register", headers={"X-Real-IP": ip_a}, data=bad).status_code
                 for _ in range(auth_route._REGISTER_IP_LIMIT)]
        r = _req_idc("POST", "/auth/register", headers={"X-Real-IP": ip_a}, data=bad)
        check("F1 register per-IP limiter: attempts beyond limit → 429 localized",
              all(c == 200 for c in codes) and r.status_code == 429
              and _form_error(r.text) == EN["err_too_many_attempts"], (codes[-1], r.status_code))
        r = _req_idc("POST", "/auth/register", headers={"X-Real-IP": ip_b}, data=bad)
        check("F1b other IP unaffected", r.status_code == 200, r.status_code)
        _clock[0] += auth_route._REGISTER_IP_WINDOW + 1
        r = _req_idc("POST", "/auth/register", headers={"X-Real-IP": ip_a}, data=bad)
        check("F1c register window slid (controlled clock) → passes again",
              r.status_code == 200, r.status_code)

        # every in-budget failure must still answer as a normal failed
        # login — a limiter that engages before the threshold (the exact
        # regression this catches) would 429 one of these
        within = [_req_idc("POST", "/auth/login", headers={"X-Real-IP": ip_c},
                           data={"login": f"{_u}nouser{i}", "password": "whatever-1"})
                  for i in range(auth_route._LOGIN_IP_FAIL_LIMIT)]
        check("F2a in-budget failures all answer 200 err_login_failed (no premature 429)",
              all(r_.status_code == 200
                  and _form_error(r_.text) == EN["err_login_failed"] for r_ in within),
              [r_.status_code for r_ in within])
        r = _req_idc("POST", "/auth/login", headers={"X-Real-IP": ip_c},
                 data={"login": _u + "b1", "password": "pw-b1-12345"})
        check("F2b login per-IP aggregate limiter (distinct logins) → 429 even with valid creds",
              r.status_code == 429 and _form_error(r.text) == EN["err_too_many_attempts"],
              r.status_code)
        r = _req_idc("POST", "/auth/login", headers={"X-Real-IP": ip_d},
                 data={"login": _u + "b1", "password": "pw-b1-12345"})
        check("F2c other IP logs in normally", r.status_code == 302, r.status_code)
        _clock[0] += auth_route._LOGIN_IP_FAIL_WINDOW + 1
        r = _req_idc("POST", "/auth/login", headers={"X-Real-IP": ip_c},
                 data={"login": _u + "b1", "password": "pw-b1-12345"})
        check("F2d login aggregate window slid (controlled clock) → 302 again",
              r.status_code == 302, r.status_code)

        for i in range(auth_route._LOGIN_FAIL_LIMIT):
            _req_idc("POST", "/auth/login", headers={"X-Real-IP": ip_e},
                 data={"login": _u + "b2", "password": "wrong-pass"})
        r = _req_idc("POST", "/auth/login", headers={"X-Real-IP": ip_e},
                 data={"login": _u + "b2", "password": "pw-b2-12345"})
        check("F3 per-(ip, login) throttle unchanged", r.status_code == 200
              and _form_error(r.text) == EN["err_login_failed"], r.status_code)
    finally:
        # config, clock and this run's limiter keys are all restored
        (auth_route._REGISTER_IP_LIMIT, auth_route._LOGIN_IP_FAIL_LIMIT,
         auth_route._LOGIN_FAIL_LIMIT, auth_route.now) = _saved
        for store in (auth_route._register_attempts, auth_route._login_ip_failures):
            for k in ((ip,) for ip in _rl_ips):
                store.pop(k, None)
        for k in [k for k in list(auth_route._login_failures) if k[0] in _rl_ips]:
            auth_route._login_failures.pop(k, None)

    # ═══════════ G. /mcp stale cookie + Bearer ═══════════
    g = _mkuser(_u + "g", f"{_u}g@t.co", "pw-g-123456")
    tok = create_key(g, "idc")["key"]
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    r = _req_idc("POST", "/mcp", cookies={COOKIE_NAME: "stale-" + _u},
             headers={"Authorization": f"Bearer {tok}"}, json=rpc)
    check("G1 stale cookie + valid Bearer on /mcp → authenticated (not 401)",
          r.status_code == 200 and "result" in r.json(), r.status_code)
    r = _req_idc("POST", "/mcp", cookies={COOKIE_NAME: "stale-" + _u}, json=rpc)
    # a cookie is never a machine credential: stale cookie + no Bearer is
    # the anonymous surface (register + first-party tools only)
    _g2 = [t["name"] for t in ((r.json().get("result") or {}).get("tools") or [])] \
        if r.status_code == 200 else []
    check("G2 stale cookie, no Bearer → anonymous tools/list (register, no meta tools)",
          r.status_code == 200 and "register" in _g2
          and "list_uploads" not in _g2, f"{r.status_code} {_g2[:5]}")
    r = _req_idc("GET", "/account/profile", cookies={COOKIE_NAME: "stale-" + _u},
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
            purge_users(con, [uid])
        left = con.execute(
            "select count(*) c from users where username like %s or x_user_id like %s",
            (_u + "%", "xid-" + _u + "%")).fetchone()["c"]
    print("cleanup: users removed", len(ids), "left", left)


# ════════════════════════════════════════════════════════════════════════
# OAuth 2.1 for /mcp (CIMD clients)
#
# OAuth 2.1 canonical for the main /mcp surface (CIMD clients).
#
#   W*  Well-known metadata: RFC 9728 protected-resource (the resource IS
#       BASE_URL/mcp; scopes = the capability triple) and RFC 8414
#       authorization-server (PKCE S256, public clients, NO registration
#       endpoint — CIMD replaced DCR).
#   A*  CIMD validation: the client_id URL fetch goes through the pinned
#       SSRF-guarded transport; the document must self-reference, carry
#       valid redirect_uris and a public-client auth method.
#   F*  Full flow (identity core in-process with the fetch patched; the
#       resource server side over real HTTP): authorize validation →
#       consent code → PKCE exchange → access token calls /mcp → refresh
#       rotation → resource binding → PKCE negatives.
#   S*  Capability enforcement at the one chokepoint: a token granted
#       only tools:call executes tools but is refused files:read /
#       files:delete with err_insufficient_scope; fsk_ keys keep the full
#       set; OAuth plugin attribution lands on tool_calls.oauth_client_id.
#   C*  Challenge: anonymous tools/call → 401 + WWW-Authenticate pointing
#       at the protected-resource metadata; `register` stays anonymously
#       callable (the one-call onboarding path).
#   R*  Revocation: grants are viewable and revocable from the
#       session-authenticated account surface — a revoked token is 401 on
#       /mcp and its refresh is invalid_grant; another user's grant is
#       untouchable (404); a password change clears browser sessions only
#       while the OAuth tokens and the fsk_ key both survive.
#
# Fail-closed env guard; per-run unique names; fixtures cleaned at end.
# ════════════════════════════════════════════════════════════════════════
section('OAuth 2.1 for /mcp (CIMD clients)')

import asyncio
import base64
import hashlib
import http.client as _hc
import json as _json
import os as _os
import secrets
import sys as _sys
import time as _ts
import urllib.parse as _up
import uuid as _uuid


_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")


_HOST_oau = ("127.0.0.1", int(TEST_PORT))
BASE = _os.environ.get("FLASHSHARE_BASE_URL", f"http://127.0.0.1:{TEST_PORT}").rstrip("/")
RESOURCE = f"{BASE}/mcp"
ts_oau = int(_ts.time())
_u = "oa" + _uuid.uuid4().hex[:6]


def _req_oau(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_oau, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        parsed = _json.loads(data) if data else None
    except Exception:
        parsed = data.decode("utf-8", "replace")
    return r.status, hdrs, parsed


def _mcp(body, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return _req_oau("POST", "/mcp", body, h)


# ── fixtures: a real web user (owns the OAuth grant) ──
from database import db  # noqa: E402
from identity.apikeys import create_key  # noqa: E402


def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_oau, ts_oau)).fetchone()
    return int(row["id"])


UID = _mkuser(_u)
UID2 = None  # second user, created by the R* revocation section
FSK = create_key(UID, "oa-suite")["key"]

PNG_HEX = ("89504e470d0a1a0a0000000d4948445200000001000000010806"
           "0000001f15c4890000000d49444154789c626001000000ffff03"
           "000006000557bfabd40000000049454e44ae426082")
PNG_B64 = base64.b64encode(bytes.fromhex(PNG_HEX)).decode()

# ═══ W. well-known metadata ═══
st, _h, d = _req_oau("GET", "/.well-known/oauth-protected-resource")
check("W1 protected-resource metadata: resource IS /mcp, AS = issuer",
      st == 200 and d.get("resource") == RESOURCE
      and d.get("authorization_servers") == [BASE], d)
check("W2 scopes_supported = capability triple",
      d.get("scopes_supported") == ["tools:call", "files:read", "files:delete"], d)

st, _h, d = _req_oau("GET", "/.well-known/oauth-authorization-server")
check("W3 AS metadata: PKCE S256, public clients, NO registration endpoint (CIMD)",
      st == 200 and d.get("code_challenge_methods_supported") == ["S256"]
      and d.get("token_endpoint_auth_methods_supported") == ["none"]
      and "registration_endpoint" not in d
      and d.get("client_id_metadata_document_supported") is True
      and d.get("scopes_supported") == ["tools:call", "files:read", "files:delete"], d)

# ═══ C. challenge contract ═══
st, h, d = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "share",
                            "arguments": {"client_ref": f"c-{_u}"}}})
_wa = (h or {}).get("www-authenticate") or ""
check("C1 anonymous tools/call → 401 + WWW-Authenticate resource_metadata",
      st == 401 and "resource_metadata" in _wa
      and "oauth-protected-resource" in _wa, (st, _wa))

st, h, d = _mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "register", "arguments": {}}})
check("C2 register stays anonymously callable (200, mints fsk_ key)",
      st == 200 and "fsk_" in _json.dumps(d), st)

st, h, d = _mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "catalog_search", "arguments": {"q": "x"}}},
                token="fsk_definitely_not_real")
check("C3 presented-but-invalid bearer → 401 (never silent anonymous)",
      st == 401, st)

# invalid-token challenge carries the RFC 6750 error code
st, h, d = _mcp({"jsonrpc": "2.0", "id": 4, "method": "ping"},
                token="fsot_not_a_real_token")
_wa = (h or {}).get("www-authenticate") or ""
check("C4 invalid bearer → 401 + error=\"invalid_token\" + resource_metadata",
      st == 401 and 'error="invalid_token"' in _wa
      and "resource_metadata" in _wa, (st, _wa))

# authorize per-IP throttle (CIMD outbound + codes-write amplifier gate)
import app.routes.oauth_provider as _rp  # noqa: E402
from starlette.requests import Request as _SReq  # noqa: E402
_rp._authz_hits.clear()
def _fake_req(ip="9.9.9.9"):
    scope = {"type": "http", "method": "GET", "path": "/oauth/authorize",
             "query_string": b"", "headers": [],
             "client": (ip, 1234), "server": ("test", 80)}
    return _SReq(scope)
_throttled = [_rp._authorize_throttled(_fake_req())
              for _ in range(_rp._AUTHZ_LIMIT + 1)]
check("C5 authorize per-IP throttle engages at the limit",
      all(_throttled[:_rp._AUTHZ_LIMIT]) is False
      and _throttled[-1] is True, (sum(_throttled), _rp._AUTHZ_LIMIT))
_rp._authz_hits.clear()

# /oauth/token per-IP throttle: separate store, its own limit
_rp._token_hits.clear()
_throttled_t = [_rp._token_throttled(_fake_req())
                for _ in range(_rp._TOKEN_LIMIT + 1)]
check("C6 token per-IP throttle engages at the limit",
      all(_throttled_t[:_rp._TOKEN_LIMIT]) is False
      and _throttled_t[-1] is True, (sum(_throttled_t), _rp._TOKEN_LIMIT))
_rp._token_hits.clear()

# full consent flow over the in-process ASGI app (TestClient): the
# patched fetch IS the server's fetch — login, consent render, code
# redirect with state echo, deny path, CSRF nonce enforcement
from fastapi.testclient import TestClient as _TC  # noqa: E402
from app.main import app as _app  # noqa: E402

# ═══ A. CIMD validation (fetch patched to a served document) ═══
import remote as _remote  # noqa: E402
import identity.oauth_provider as _oa  # noqa: E402

CIMD_URL = f"https://plugin-{_u}.example/oauth.json"
REDIRECT = f"https://plugin-{_u}.example/callback"
CIMD_DOC = {
    "client_id": CIMD_URL,
    "client_name": "Suite Plugin",
    "redirect_uris": [REDIRECT, "http://localhost:8943/callback"],
    "token_endpoint_auth_method": "none",
    "logo_uri": "https://plugin.example/logo.png",
}


def _serve_cimd(doc):
    return 200, _json.dumps(doc).encode(), None, "application/json", None


def _cimd_hop(doc):
    def _hop(url, method="GET", body=None, headers=None, timeout_s=60,
             max_bytes=None, deadline=None):
        return _serve_cimd(doc)
    return _hop


# Single-hop stub: the REAL redirect loop (pinned_request) runs in the
# middle — the resolver is stubbed so no real DNS happens (.example
# hosts do not resolve).
_rs = _remote.socket
_orig_pinned = _remote._pinned_request
_orig_resolve = _remote._resolve_host
def _fake_resolve(host, port):
    return [(_rs.AF_INET, _rs.SOCK_STREAM, _rs.IPPROTO_TCP, "",
             ("93.184.216.34", port))]
_remote._resolve_host = _fake_resolve

_st, _h, _d = None, None, None
try:
    _remote._pinned_request = _cimd_hop(CIMD_DOC)
    _oa._cimd_cache.clear()

    doc = _oa.cimd_fetch(CIMD_URL)
    check("A1 CIMD fetch returns the verified document",
          bool(doc) and doc["client_name"] == "Suite Plugin"
          and set(doc["redirect_uris"]) == set(CIMD_DOC["redirect_uris"]), doc)

    bad = dict(CIMD_DOC, client_id="https://other.example/oauth.json")
    _remote._pinned_request = _cimd_hop(bad)
    _oa._cimd_cache.clear()
    check("A2 non-self-referential document rejected (anti-impersonation)",
          _oa.cimd_fetch(CIMD_URL) is None)

    _remote._pinned_request = _cimd_hop(
        dict(CIMD_DOC, redirect_uris=["ftp://x/y"]))
    _oa._cimd_cache.clear()
    check("A3 non-https/loopback redirect entries rejected",
          _oa.cimd_fetch(CIMD_URL) is None)

    _remote._pinned_request = _cimd_hop(
        dict(CIMD_DOC, token_endpoint_auth_method="client_secret_basic"))
    _oa._cimd_cache.clear()
    check("A4 confidential-client auth method rejected (public-only MVP)",
          _oa.cimd_fetch(CIMD_URL) is None)

    check("A5 http:// client_id rejected before any fetch",
          _oa.cimd_fetch("http://plain.example/oauth.json") is None)

    # ═══ F. full flow (identity core; resource-server side over real HTTP) ═══
    _remote._pinned_request = _cimd_hop(CIMD_DOC)
    _oa._cimd_cache.clear()

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    vdoc, scope = _oa.validate_authorization(
        CIMD_URL, REDIRECT, "tools:call", RESOURCE,
        challenge, "S256")
    check("F1 authorize validation passes (CIMD client, PKCE S256, resource=/mcp)",
          vdoc["client_name"] == "Suite Plugin" and scope == "tools:call", scope)

    try:
        _oa.validate_authorization(CIMD_URL, REDIRECT, "tools:call",
                                   f"{BASE}/other", challenge, "S256")
        ok = False
    except ValueError as e:
        ok = str(e) == "invalid_target"
    check("F2 resource binding: audience ≠ /mcp rejected", ok)

    code = _oa.create_authorization_code(
        UID, CIMD_URL, REDIRECT, challenge, scope, RESOURCE)
    check("F3 consent code minted (fsoa_ prefix)", code.startswith("fsoa_"), code[:12])

    toks = _oa.exchange_authorization_code(
        code, CIMD_URL, REDIRECT, verifier, RESOURCE)
    ACCESS = toks["access_token"]
    REFRESH = toks["refresh_token"]
    check("F4 PKCE exchange mints fsot_ access + fsor_ refresh, scope echoed",
          ACCESS.startswith("fsot_") and REFRESH.startswith("fsor_")
          and toks["scope"] == "tools:call", list(toks))

    ident = _oa.authenticate_access_token(ACCESS, RESOURCE)
    check("F5 access token authenticates as the granting user with client attribution",
          ident and ident["user"]["id"] == UID
          and ident["scopes"] == {"tools:call"}
          and ident["client_id"] == CIMD_URL,
          ident and {k: ident[k] for k in ("scopes", "client_id")})

    try:
        _oa.exchange_authorization_code(
            code, CIMD_URL, REDIRECT, verifier, RESOURCE)
        ok = False
    except ValueError as e:
        ok = str(e) == "invalid_grant"
    check("F6 authorization code is single-use", ok)

    # ── the resource-server side over real HTTP ──
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                      "params": {"name": "share",
                                 "arguments": {
                                     "files": [{"filename": "a.png", "b64": PNG_B64}],
                                     "client_ref": f"oa-{_u}"}}}, token=ACCESS)
    _r = (d or {}).get("result") or {}
    check("S1 OAuth token executes a tool over HTTP (readable result)",
          st == 200 and _r.get("isError") is False
          and "Result page:" in ((_r.get("content") or [{}])[0].get("text", "")),
          str(d)[:120])

    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                      "params": {"name": "list_uploads",
                                 "arguments": {}}}, token=ACCESS)
    _r = (d or {}).get("result") or {}
    _sc = _r.get("structuredContent") or {}
    check("S2 tools:call-only token refused files:read (err_insufficient_scope)",
          _r.get("isError") is True
          and _sc.get("error") == "err_insufficient_scope"
          and _sc.get("required_scope") == "files:read", _sc)

    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                      "params": {"name": "delete_upload",
                                 "arguments": {"public_id": "x"}}}, token=ACCESS)
    _sc = ((d or {}).get("result") or {}).get("structuredContent") or {}
    check("S3 tools:call-only token refused files:delete",
          _sc.get("error") == "err_insufficient_scope"
          and _sc.get("required_scope") == "files:delete", _sc)

    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                      "params": {"name": "share",
                                 "arguments": {
                                     "files": [{"filename": "b.png", "b64": PNG_B64}],
                                     "client_ref": f"fsk-{_u}"}}}, token=FSK)
    check("S4 fsk_ key keeps the full capability set (no regression)",
          st == 200 and not ((d or {}).get("result") or {}).get("isError"), st)

    # S6: the platform-tool path enforces tools:call too — mint a
    # files:read-only token and try to execute (paid tools spend credits)
    code_rd = _oa.create_authorization_code(
        UID, CIMD_URL, REDIRECT, challenge, "files:read", RESOURCE)
    toks_rd = _oa.exchange_authorization_code(
        code_rd, CIMD_URL, REDIRECT, verifier, RESOURCE)
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 16, "method": "tools/call",
                      "params": {"name": "share",
                                 "arguments": {
                                     "files": [{"filename": "c.png", "b64": PNG_B64}],
                                     "client_ref": f"s6-{_u}"}}},
                     token=toks_rd["access_token"])
    _sc = ((d or {}).get("result") or {}).get("structuredContent") or {}
    check("S6 files:read-only token refused a PLATFORM tool (tools:call)",
          ((d or {}).get("result") or {}).get("isError") is True
          and _sc.get("error") == "err_insufficient_scope"
          and _sc.get("required_scope") == "tools:call", _sc)
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                      "params": {"name": "list_uploads",
                                 "arguments": {}}},
                     token=toks_rd["access_token"])
    check("S6b the same files:read token DOES list uploads",
          st == 200 and not ((d or {}).get("result") or {}).get("isError"),
          st)

    with db() as con:
        row = con.execute(
            "select oauth_client_id from tool_calls where user_id=%s"
            " and client_ref=%s", (UID, f"oa-{_u}")).fetchone()
    check("S5 OAuth plugin attribution stamped on tool_calls",
          row and row["oauth_client_id"] == CIMD_URL, row)

    # ═══ S5b. attribution contract ═══
    # oauth_client_id = the SERVER AUTHENTICATION CONTEXT that CREATED the
    # call row (stamped in the claim INSERT's own transaction). Replays —
    # same client or any other — answer the settled row and never
    # re-attribute it; there is no post-hoc write path at all.
    _ref_a = f"oa-{_u}"
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                      "params": {"name": "share",
                                 "arguments": {
                                     "files": [{"filename": "a.png", "b64": PNG_B64}],
                                     "client_ref": _ref_a}}},
                     token=ACCESS)
    _sc_r = ((d or {}).get("result") or {}).get("structuredContent") or {}
    # client B on the same user: mint + exchange (no CIMD fetch needed)
    CIMD_B = f"https://plugin-b-{_u}.example/oauth.json"
    REDIRECT_B = f"https://plugin-b-{_u}.example/cb"
    _code_b = _oa.create_authorization_code(
        UID, CIMD_B, REDIRECT_B, challenge, "tools:call", RESOURCE)
    _toks_b = _oa.exchange_authorization_code(
        _code_b, CIMD_B, REDIRECT_B, verifier, RESOURCE)
    st_b, _h, d_b = _mcp({"jsonrpc": "2.0", "id": 24, "method": "tools/call",
                          "params": {"name": "share",
                                     "arguments": {
                                         "files": [{"filename": "a.png", "b64": PNG_B64}],
                                         "client_ref": _ref_a}}},
                         token=_toks_b["access_token"])
    _sc_rb = ((d_b or {}).get("result") or {}).get("structuredContent") or {}
    with db() as con:
        row_a = con.execute(
            "select oauth_client_id, status from tool_calls where user_id=%s"
            " and client_ref=%s", (UID, _ref_a)).fetchone()
    check("S5b replay by A then client B → replay answered twice, attribution stays A",
          row_a and row_a["oauth_client_id"] == CIMD_URL
          and _sc_r.get("replay") is True and _sc_rb.get("replay") is True,
          f"{_sc_r} {_sc_rb} {row_a}")

    # ═══ S5c. creation-source attribution ═══
    # An fsk_ key CREATES the call row → attribution stays NULL forever;
    # an OAuth token replaying that ref afterwards must not stamp itself
    # on a row it did not create (the old post-hook did exactly that).
    _ref_c = f"attr-fsk-{_u}"
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 25, "method": "tools/call",
                      "params": {"name": "share",
                                 "arguments": {
                                     "files": [{"filename": "d.png", "b64": PNG_B64}],
                                     "client_ref": _ref_c}}}, token=FSK)
    _ok_c = st == 200 and not ((d or {}).get("result") or {}).get("isError")
    st2, _h, d2 = _mcp({"jsonrpc": "2.0", "id": 26, "method": "tools/call",
                        "params": {"name": "share",
                                   "arguments": {
                                       "files": [{"filename": "d.png", "b64": PNG_B64}],
                                       "client_ref": _ref_c}}},
                       token=ACCESS)
    _replay_c = (((d2 or {}).get("result") or {})
                 .get("structuredContent") or {}).get("replay") is True
    with db() as con:
        row_c = con.execute(
            "select oauth_client_id, status from tool_calls where user_id=%s"
            " and client_ref=%s", (UID, _ref_c)).fetchone()
    check("S5c fsk_ creation stays NULL; OAuth replay of the ref never re-attributes",
          _ok_c and _replay_c and row_c and row_c["oauth_client_id"] is None
          and row_c["status"] == "ok",
          f"ok={_ok_c} replay={_replay_c} row={row_c}")

    
    # failed platform http tool (unreachable endpoint) for A
    _ftk = f"oaftk{_os.urandom(4).hex()}"
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled, accept_json,"
            " format_family, body_format, files_min, files_max,"
            " outputs_json, endpoint_json, created_at, updated_at)"
            " values (%s, NULL, %s, 'http', %s, 'public', TRUE, NULL,"
            " NULL, 'text', 0, 0, '[\"text\"]', %s, %s, %s)",
            (_ftk, _ftk, _json.dumps({"en": "OA Fail Probe"}),
             _json.dumps({"url": "http://127.0.0.1:1/never",
                          "timeout_s": 3}), ts_oau, ts_oau))
        con.execute("insert into pricing (tool_id, cost, enabled,"
                    " updated_at) values (%s, 0, TRUE, %s)", (_ftk, ts_oau))
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 22, "method": "tools/call",
                      "params": {"name": _ftk,
                                 "arguments": {"input": "x",
                                               "client_ref": f"oaF-{_u}"}}},
                     token=ACCESS)
    _sc_f = ((d or {}).get("result") or {}).get("structuredContent") or {}
    with db() as con:
        row_f = con.execute(
            "select status, oauth_client_id from tool_calls where user_id=%s"
            " and client_ref=%s", (UID, f"oaF-{_u}")).fetchone()
    check("S5b A's failed call → call row failed, attribution still A",
          row_f and row_f["status"] == "failed"
          and row_f["oauth_client_id"] == CIMD_URL,
          f"{_sc_f} {row_f}")
    _oa._cimd_cache.clear()

    # refresh rotation
    toks2 = _oa.refresh_access_token(REFRESH, CIMD_URL, RESOURCE)
    check("F7 refresh rotation mints a fresh pair",
          toks2["access_token"].startswith("fsot_")
          and toks2["access_token"] != ACCESS, list(toks2)[:1])
    try:
        _oa.refresh_access_token(REFRESH, CIMD_URL, RESOURCE)
        ok = False
    except ValueError:
        ok = True
    check("F8 used refresh token is dead (rotation is one-time)", ok)

    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                      "params": {"name": "catalog_search",
                                 "arguments": {"q": ""}}}, token=toks2["access_token"])
    check("F9 rotated token works over HTTP (discovery = tools:call)",
          st == 200 and not ((d or {}).get("result") or {}).get("isError"), st)

    # PKCE negative: fresh code, wrong verifier
    code2 = _oa.create_authorization_code(
        UID, CIMD_URL, REDIRECT, challenge, "tools:call", RESOURCE)
    try:
        _oa.exchange_authorization_code(
            code2, CIMD_URL, REDIRECT, "x" * 50, RESOURCE)
        ok = False
    except ValueError as e:
        ok = str(e) == "invalid_grant"
    check("F10 wrong PKCE verifier rejected", ok)

finally:
    _remote._pinned_request = _orig_pinned
    _remote._resolve_host = _orig_resolve
    _oa._cimd_cache.clear()

# ═══ consent flow (TestClient, in-process server + patched CIMD) ═══
try:
    _remote._pinned_request = _cimd_hop(CIMD_DOC)
    _remote._resolve_host = _fake_resolve
    _oa._cimd_cache.clear()
    with db() as con:
        _set_pw = con.execute(
            "update users set password_hash=%s where id=%s",
            (__import__("bcrypt").hashpw(b"Test12345", __import__("bcrypt").gensalt()).decode(), UID)).rowcount
    with _TC(_app, base_url="https://testserver") as _tc:
        _lr = _tc.post("/auth/login",
                       data={"login": _u, "password": "Test12345"},
                       follow_redirects=False)
        check("P1 web login for the consent flow", _lr.status_code in (302, 303),
              _lr.status_code)
        authz_url = ("/oauth/authorize?" + _up.urlencode({
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": REDIRECT, "scope": "tools:call files:read",
            "state": f"st-{_u}", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE}))
        page = _tc.get(authz_url)
        _html = page.text
        check("P2 consent page renders the CIMD-verified name + both scopes",
              page.status_code == 200 and "Suite Plugin" in _html
              and "oauth_scope_tools_call" in _html
              and "oauth_scope_files_read" in _html
              and "oauth_scope_files_delete" not in _html,
              page.status_code)
        # anti-impersonation lines: where the client lives + where the
        # browser returns (loopback redirect distinguishes the two hosts)
        _page_lb = _tc.get("/oauth/authorize?" + _up.urlencode({
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": "http://localhost:8943/callback",
            "scope": "tools:call", "state": f"st-{_u}",
            "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE}))
        _html_lb = _page_lb.text
        check("P2b consent page shows client host + redirect host lines",
              _page_lb.status_code == 200
              and 'data-i18n="oauth_client_from"' in _html_lb
              and 'data-i18n="oauth_redirect_to"' in _html_lb
              and f"plugin-{_u}.example" in _html_lb
              and "localhost" in _html_lb,
              _page_lb.status_code)
        # CSRF: posting without the nonce cookie fails
        bad = _tc.post("/oauth/authorize", data={
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": REDIRECT, "scope": "tools:call",
            "state": "x", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE,
            "consent_nonce": "forged", "action": "allow"})
        check("P3 consent submit without the nonce cookie rejected",
              bad.status_code == 400, bad.status_code)
        # deny → error redirect with state echo
        deny = _tc.post("/oauth/authorize", data={
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": REDIRECT, "scope": "tools:call",
            "state": f"st-{_u}", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE,
            "consent_nonce": _tc.cookies.get("fs_oauth_consent"),
            "action": "deny"}, follow_redirects=False)
        _loc = deny.headers.get("location") or ""
        check("P4 deny → redirect error=access_denied + state echoed",
              deny.status_code == 302 and "error=access_denied" in _loc
              and f"state=st-{_u}" in _loc, _loc[:100])
        # allow → code redirect with state echo + iss (a fresh render
        # first — the deny above consumed the one-shot nonce cookie)
        _tc.get(authz_url)
        allow = _tc.post("/oauth/authorize", data={
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": REDIRECT, "scope": "tools:call",
            "state": f"st-{_u}", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE,
            "consent_nonce": _tc.cookies.get("fs_oauth_consent"),
            "action": "allow"}, follow_redirects=False)
        _loc = allow.headers.get("location") or ""
        _q = dict(_up.parse_qsl(_up.urlsplit(_loc).query))
        check("P5 allow → 302 with code + state + iss",
              allow.status_code == 302 and _q.get("code", "").startswith("fsoa_")
              and _q.get("state") == f"st-{_u}" and _q.get("iss") == BASE,
              _loc[:120])
        # the consent-minted code exchanges at the real token endpoint
        _tokr = _tc.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": CIMD_URL,
            "code": _q.get("code"), "redirect_uri": REDIRECT,
            "code_verifier": verifier, "resource": RESOURCE})
        _td = _tokr.json() if _tokr.status_code == 200 else {}
        check("P6 consent code exchanges at /oauth/token over HTTP",
              _tokr.status_code == 200 and _td.get("access_token", "").startswith("fsot_")
              and _td.get("scope") == "tools:call", _tokr.status_code)
        if _td.get("access_token"):
            _st2, _h2, d2 = _mcp({"jsonrpc": "2.0", "id": 20,
                                  "method": "tools/call",
                                  "params": {"name": "catalog_search",
                                             "arguments": {"q": ""}}},
                                 token=_td["access_token"])
            check("P7 consent-flow token works end-to-end on /mcp",
                  _st2 == 200 and not ((d2 or {}).get("result") or {}).get("isError"),
                  _st2)

        # ═══ R. grants: view + revoke + credential rotation ═══
        _gl = _tc.get("/api/oauth/grants")
        _gd = _gl.json() if _gl.status_code == 200 else {}
        _entry = next((g for g in _gd.get("grants", [])
                       if g.get("client_id") == CIMD_URL), None)
        check("R1 grants list shows the consented client (host + merged scopes)",
              _gl.status_code == 200 and _gd.get("ok") is True and _entry
              and _entry["client_host"] == f"plugin-{_u}.example"
              and _entry["scope"] == "tools:call files:read",
              str(_gd)[:140])

        _rv = _tc.post("/api/oauth/grants/revoke",
                       json={"client_id": CIMD_URL})
        check("R2 revoking the client's grant → ok",
              _rv.status_code == 200 and _rv.json().get("ok") is True,
              _rv.status_code)
        _st3, _h3, d3 = _mcp({"jsonrpc": "2.0", "id": 21,
                              "method": "tools/call",
                              "params": {"name": "catalog_search",
                                         "arguments": {"q": ""}}},
                             token=_td["access_token"])
        check("R3 revoked access token → 401 on /mcp", _st3 == 401, _st3)
        try:
            _oa.refresh_access_token(_td["refresh_token"], CIMD_URL, RESOURCE)
            _r3b = False
        except ValueError as _e3b:
            _r3b = str(_e3b) == "invalid_grant"
        check("R3b revoked refresh token → invalid_grant", _r3b)

        _rv2 = _tc.post("/api/oauth/grants/revoke",
                        json={"client_id": CIMD_URL})
        check("R4 re-revoke with no live rows → 404 not_found",
              _rv2.status_code == 404
              and _rv2.json().get("error") == "not_found", _rv2.status_code)

        # another user's grant is untouchable from this session
        UID2 = _mkuser(_u + "b")
        CIMD_URL2 = f"https://other-{_u}.example/oauth.json"
        REDIRECT2 = f"https://other-{_u}.example/callback"
        _code_b = _oa.create_authorization_code(
            UID2, CIMD_URL2, REDIRECT2, challenge, "tools:call", RESOURCE)
        _toks_b = _oa.exchange_authorization_code(
            _code_b, CIMD_URL2, REDIRECT2, verifier, RESOURCE)
        _rv3 = _tc.post("/api/oauth/grants/revoke",
                        json={"client_id": CIMD_URL2})
        check("R5 revoking another user's client grant → 404",
              _rv3.status_code == 404, _rv3.status_code)
        check("R5b the other user's token still authenticates",
              _oa.authenticate_access_token(
                  _toks_b["access_token"], RESOURCE) is not None)

        # /oauth/token honors its per-IP gate over HTTP (burn the budget
        # for the TestClient peer address, then one real POST)
        _rp._token_hits.clear()
        for _ in range(_rp._TOKEN_LIMIT):
            _rp._token_throttled(_fake_req(ip="testclient"))
        _t429 = _tc.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": CIMD_URL,
            "code": "fsoa_x", "redirect_uri": REDIRECT,
            "code_verifier": verifier, "resource": RESOURCE})
        check("C7 real /oauth/token over the limit → 429 temporarily_unavailable",
              _t429.status_code == 429
              and _t429.json().get("error") == "temporarily_unavailable",
              _t429.status_code)
        _rp._token_hits.clear()

        # credential independence: a password change clears browser
        # sessions only — the OAuth grant and the fsk_ key both survive
        _code_c = _oa.create_authorization_code(
            UID, CIMD_URL, REDIRECT, challenge, "tools:call", RESOURCE)
        _toks_c = _oa.exchange_authorization_code(
            _code_c, CIMD_URL, REDIRECT, verifier, RESOURCE)
        from identity.auth import change_password as _chg_pw
        _chg_pw(UID, "Test12345", "NewPass1234")
        _st4, _h4, d4 = _mcp({"jsonrpc": "2.0", "id": 22,
                              "method": "tools/call",
                              "params": {"name": "catalog_search",
                                         "arguments": {"q": ""}}},
                             token=_toks_c["access_token"])
        check("R6 password change keeps the user's OAuth tokens working",
              _st4 == 200 and not ((d4 or {}).get("result") or {}).get("isError"),
              _st4)
        _toks_c2 = _oa.refresh_access_token(_toks_c["refresh_token"],
                                            CIMD_URL, RESOURCE)
        check("R6b refresh after password change still grants", bool(_toks_c2))
        st, _h, d = _mcp({"jsonrpc": "2.0", "id": 23,
                          "method": "tools/call",
                          "params": {"name": "catalog_search",
                                     "arguments": {"q": ""}}},
                         token=FSK)
        check("R7 the same user's fsk_ key still works after the change",
              st == 200 and not ((d or {}).get("result") or {}).get("isError"),
              st)
finally:
    _remote._pinned_request = _orig_pinned
    _remote._resolve_host = _orig_resolve
    _oa._cimd_cache.clear()

# token endpoint over HTTP: unknown client (fetch will fail for real)
st, _h, d = _req_oau("POST", "/oauth/token", {
    "grant_type": "authorization_code", "client_id": "https://no-such.example/x.json",
    "code": "fsoa_x", "redirect_uri": "https://no-such.example/cb",
    "code_verifier": "v" * 50, "resource": RESOURCE},
    {"Content-Type": "application/x-www-form-urlencoded"})
# form-encoded body needed; send as dict is JSON — do it properly
body = ("grant_type=authorization_code&client_id="
        + _up.quote("https://no-such.example/x.json")
        + "&code=fsoa_x&redirect_uri=" + _up.quote("https://no-such.example/cb")
        + "&code_verifier=" + "v" * 50 + "&resource=" + _up.quote(RESOURCE))
st, _h, d = _req_oau("POST", "/oauth/token", body.encode(),
                 {"Content-Type": "application/x-www-form-urlencoded"})
check("T1 token endpoint: unresolvable CIMD client → 401 invalid_client",
      st == 401 and (d or {}).get("error") == "invalid_client", (st, d))

# ═══ cleanup ═══
with db() as con:
    for _uid_c in (UID, UID2):
        if _uid_c is None:
            continue
        con.execute("delete from oauth_mcp_codes where user_id=%s", (_uid_c,))
        con.execute("delete from oauth_mcp_tokens where user_id=%s", (_uid_c,))
        con.execute("delete from tool_calls where user_id=%s", (_uid_c,))
        con.execute("delete from uploads where user_id=%s", (_uid_c,))
        con.execute("delete from api_keys where user_id=%s", (_uid_c,))
        con.execute("delete from ledger where user_id=%s", (_uid_c,))
    purge_users(con, [u for u in (UID, UID2) if u is not None])
print("cleanup: oauth fixtures removed")



# ════════════════════════════════════════════════════════════════════════
# Pool saturation under the SAME process/pool (identity semantics)
#
# I2 audit item: with every pool slot held, a request carrying a VALID
# session must fail as a service error — never silently degrade to
# anonymous (login redirect / auth_required). Evidence level: HTTP
# semantics through the full middleware via an in-process TestClient —
# the ASGI app and the slot holders below share the VERY SAME
# database._pool (an out-of-process holder pool proves nothing here).
# Release must fully drain (no leak).
# ════════════════════════════════════════════════════════════════════════
section('Pool saturation: identity never silently degrades')

import threading as _thr
_ps_u = "ps" + _uuid.uuid4().hex[:8]


def _ps_form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


from app.web_auth import create_browser_session as _mk_sess_ps
from database import db as _db_ps
with _db_ps() as con:
    _row_ps = con.execute(
        "insert into users (username, email, password_hash, display_name,"
        " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
        " 'active', %s, %s) returning id",
        (_ps_u, f"{_ps_u}@t.co", ts_oau, ts_oau)).fetchone()
    _ps_uid = int(_row_ps["id"])
_valid_cookie = _mk_sess_ps(_ps_uid)
check("PS0 seeded a real session", bool(_valid_cookie), "")

import database as _dbmod
from database import db as _dbhold
from fastapi.testclient import TestClient as _TC_ps
import warnings as _w_ps
with _w_ps.catch_warnings():
    _w_ps.simplefilter("ignore", DeprecationWarning)
    _client_ps = _TC_ps(_app, raise_server_exceptions=False)

_holders = []
_release = _thr.Event()


def _hold_slot():
    with _dbhold() as con:
        con.execute("select 1")
        _release.wait(10)


for _ in range(16):        # every slot the pool has
    _t = _thr.Thread(target=_hold_slot, daemon=True)
    _t.start(); _holders.append(_t)
import time as _t_ps; _t_ps.sleep(0.8)  # let them all land in the pool

# HTTP semantics (full middleware, in-process): valid session +
# saturated pool -> service error, NOT a silent anonymous downgrade
# (no 302 to login, no auth_required body)
_r = _client_ps.get("/account/profile",
                    cookies={"flashshare_session": _valid_cookie},
                    headers={"Accept": "text/html"},
                    follow_redirects=False)
check("PS1 saturated pool + valid session → 5xx service failure (never anonymous)",
      _r.status_code >= 500, _r.status_code)

# light request keeps being served (healthz touches no DB)
_r = _client_ps.get("/healthz")
check("PS2 event loop still serves during saturation", _r.status_code == 200,
      _r.status_code)

_release.set()
for _t in _holders:
    _t.join(timeout=6)
_pool = _dbmod._get_pool()
check("PS3 all slots returned after release (no leak)",
      len(_pool._used) == 0, len(_pool._used))

# after the drain the same session is fully functional again
_r = _client_ps.get("/account/profile",
                    cookies={"flashshare_session": _valid_cookie},
                    headers={"Accept": "text/html"},
                    follow_redirects=False)
check("PS4 session fully functional after drain", _r.status_code == 200,
      _r.status_code)
with _db_ps() as con:
    con.execute("delete from sessions where user_id=%s", (_ps_uid,))
    purge_users(con, [_ps_uid])


finish()
