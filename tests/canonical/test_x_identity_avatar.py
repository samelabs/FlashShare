#!/usr/bin/env python3
"""X identity closure canonical — avatar authority = users.avatar_url.

Behavior tests over the real DB (fixtures cleaned; consecutive runs on one
TEST_DB stay green):
  1. X signup + image        → avatar_url written verbatim
  2. X signup + empty image  → avatar_url empty (UI initials fallback)
  3. existing refresh + new image → avatar_url overwritten
  4. existing refresh + empty image → avatar_url kept (never cleared)
  5. bind X + image          → avatar_url overwritten
  6. bind X + empty image    → avatar_url kept
  7. unbind X                → x_user_id/x_username/x_display_name/
                               x_profile_url/avatar_url ALL cleared
  8. public_user().avatarUrl ← avatar_url only
  9. fresh schema has no x_avatar_url column
 10. production runtime references to x_avatar_url = 0 (source scan)
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:110]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

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

# 9. fresh schema: users has no x_avatar_url column
with db() as con:
    cols = [c["column_name"] for c in con.execute(
        "select column_name from information_schema.columns"
        " where table_name='users'").fetchall()]
check("9 fresh schema: no x_avatar_url column", "x_avatar_url" not in cols,
      [c for c in cols if "avatar" in c])
check("9b fresh schema: avatar_url present", "avatar_url" in cols)

# 10. production runtime residual scan: zero x_avatar_url references
import glob as _glob
_resid = []
for pat in ("core/**/*.py", "app/**/*.py", "static/**/*.js", "app/templates/**/*.html"):
    for f in _glob.glob(_os.path.join(_REPO, pat), recursive=True):
        try:
            src = open(f, encoding="utf-8").read()
        except OSError:
            continue
        if "x_avatar_url" in src:
            _resid.append(_os.path.relpath(f, _REPO))
check("10 runtime x_avatar_url references = 0", not _resid, _resid)

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — X identity avatar authority ({COUNT} checks)")
