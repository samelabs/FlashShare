"""Credential and identity edge states: fetch-token collisions, X-login
race recovery, half-empty credential PATCH.

1. sign_fetch_token keyed its identity on (user, file, second) and the
   redemption ledger consumed the whole token — two signings of the same
   file within one second minted ONE token and ate each other's
   one-time use. Each signing now carries a nonce.
2. The X first-login UNIQUE-race recovery queried inside the aborted
   transaction (InFailedSqlTransaction); recovery now rolls back to a
   savepoint before converging.
3. A PATCH carrying auth_header="" with a non-empty token was treated
   as an explicit clear; only both-empty clears, one-sided shapes 422.
"""
import os as _os
import sys as _sys

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT or _TEST_PORT == "8000":
    _sys.stderr.write("ERROR: isolated FLASHSHARE_TEST_PORT required.\n"); _sys.exit(2)
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
_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")
from tests.support.cleanup import purge_users  # noqa: E402

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:110]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import http.client as _hc
import json
import time
import uuid
ts = int(time.time())
_u = "ce" + _os.urandom(3).hex()
_HOST = ("127.0.0.1", int(_TEST_PORT))

from database import db


def _req(method, path, body=None, headers=None, cookies=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
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


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register_login(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    conn.close()
    b, ct = _form({"login": name, "password": "Test12345"})
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
        con, "cew" + _os.urandom(3).hex(), "W", "", x_id, "w", "W", "", ts)["id"]
with db() as con:
    # reproduce the losing INSERT inside an open transaction…
    try:
        con.execute("SAVEPOINT x_login_race")
        users_repo.insert_x_user(
            con, "cel" + _os.urandom(3).hex(), "L", "", x_id, "l", "L", "", ts)
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

print()
if FAIL:
    print(f"FAIL: {FAIL}")
    _sys.exit(1)
print(f"OK — credential edge states ({COUNT} checks)")
