#!/usr/bin/env python3
"""Daily login credit canonical (restored mechanism + operator config).

  A*  Off by default: with no daily_login_amount setting a login writes
      no grant_daily ledger row and the credits page renders no claim
      widget; illegal stored values behave as off (fail-open toward
      "no reward", never toward blocking login).
  B*  Configured (settings write): a login grants exactly one
      kind=grant_daily / note=activity:daily_login /
      idem=daily:{uid}:{yyyymmdd} row of the configured amount; repeat
      logins and the explicit claim button never double-grant.
  C*  The grant is spendable through the real consume path.
  D*  Operator surface: audited POST /samelabs/economy/daily-amount
      (before/after recorded), validation 422 leaves settings
      untouched, non-operator refused.
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
from tests.support.cleanup import purge_users  # noqa: E402

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:130]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import http.client as _hc
import time

ts = int(time.time())
_u = "b7d" + _os.urandom(4).hex()

from database import db

HOST = ("127.0.0.1", int(_TEST_PORT))


def _req(method, path, body=None, headers=None):
    import json as _json
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    payload = None
    if body is not None:
        if isinstance(body, dict):
            payload = _json.dumps(body).encode()
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/json")
        else:
            payload = body if isinstance(body, (str, bytes)) else None
            if isinstance(payload, str):
                payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        return r.status, hdrs, data.decode(errors="replace")
    except Exception:
        return r.status, hdrs, repr(data[:80])


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    return _login(name)


def _login(name):
    conn = _hc.HTTPConnection(*HOST, timeout=30)
    b, ct = _form({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck


def _grant_rows(uid):
    with db() as con:
        rows = con.execute(
            "select delta, kind, note, idem_key from ledger"
            " where user_id=%s and kind='grant_daily'", (uid,)).fetchall()
    return [dict(r) for r in rows]


def _set_amount(value):
    """Direct settings write (the operator surface has its own test)."""
    with db() as con:
        con.execute("delete from settings where key='daily_login_amount'")
        if value is not None:
            con.execute(
                "insert into settings (key, value, updated_at)"
                " values ('daily_login_amount', %s, %s)", (value, ts))


users = []

# ═══ A: off by default ═══════════════════════════════════════════════
_set_amount(None)
uid_a, ck_a = _register(_u + "a")
users.append(uid_a)
st, _h, body = _req("GET", "/account/credits", None, {"Cookie": ck_a})
check("A1 no grant_daily row when unconfigured", _grant_rows(uid_a) == [])
check("A2 credits page has no claim widget", "dailyBox" not in body and "acct-daily" not in body)
st, _h, _b = _req("POST", "/account/credits/claim", b"", {"Cookie": ck_a})
check("A3 claim redirects (disabled, no grant)",
      st in (303, 302) and _grant_rows(uid_a) == [], st)

# A4: illegal stored values = off (fail-open toward "no reward")
for bad in ("abc", "1001", "-3", "5.5"):
    _set_amount(bad)
    _login(_u + "a")
    check(f"A4 illegal value {bad!r} grants nothing", _grant_rows(uid_a) == [])

# ═══ B: configured amount ═══════════════════════════════════════════
_set_amount("5")
uid_b, ck_b = _register(_u + "b")
users.append(uid_b)
rows = _grant_rows(uid_b)
day = time.strftime("%Y%m%d", time.gmtime())
check("B1 one grant row with canonical kind/note/idem",
      len(rows) == 1 and rows[0]["delta"] == 5
      and rows[0]["note"] == "activity:daily_login"
      and rows[0]["idem_key"] == f"daily:{uid_b}:{day}", rows)
_login(_u + "b")
st, _h, body = _req("POST", "/account/credits/claim", b"", {"Cookie": ck_b})
_login(_u + "b")
rows = _grant_rows(uid_b)
check("B2 repeat logins + claim never double-grant", len(rows) == 1, rows)
check("B3 claim redirects to credits page", st in (303, 302), st)
st, _h, body = _req("GET", "/account/credits", None, {"Cookie": ck_b})
check("B4 widget renders claimed state", 'id="dailyBox"' in body and "done" in body, body[:0])
st, _h, body = _req("GET", "/account/credits", None, {"Cookie": ck_a})
check("B5 widget renders claimable state for un-granted user",
      'id="dailyBox"' in body and "acct-daily-claim" in body and "acct-daily-info" in body)

# ═══ C: the grant is spendable through the real charge path ════════
from economy.billing import consume, spendable_balance, InsufficientCredits
bal = spendable_balance(uid_b)
check("C1 grant lands in spendable balance", bal == 5, bal)
row = consume(uid_b, 5, "consume", idem_key=f"b7:{uid_b}:c1", note="b7d")
check("C2 real consume path drains the grant", spendable_balance(uid_b) == 0)
try:
    consume(uid_b, 1, "consume", idem_key=f"b7:{uid_b}:c3", note="b7d")
    check("C3 further charge refused", False, "no InsufficientCredits")
except InsufficientCredits:
    check("C3 further charge refused", True)

# ═══ D: operator surface ═════════════════════════════════════════════
import json as _json
_set_amount(None)
op_uid, op_ck = _register(_u + "op")
users.append(op_uid)
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op_uid,))
    con.execute("delete from operator_audit where action='daily_amount.update'"
                " and actor_user_id=%s", (op_uid,))

st, _h, body = _req("POST", "/samelabs/economy/daily-amount", {"amount": 5},
                    {"Cookie": op_ck})
check("D1 operator set 5 ok", st == 200 and _json.loads(body).get("amount") == 5,
      f"{st} {body[:80]}")
with db() as con:
    row = con.execute(
        "select value from settings where key='daily_login_amount'").fetchone()
    aud = con.execute(
        "select action, target_type, target_id, before_json, after_json"
        " from operator_audit where actor_user_id=%s"
        " and action='daily_amount.update' order by id desc limit 1",
        (op_uid,)).fetchone()
check("D2 settings value updated", row is not None and row["value"] == "5", row)
_bj = dict(aud) if aud else {}
check("D3 one audit fact with before/after",
      aud is not None and _bj.get("action") == "daily_amount.update"
      and _bj.get("target_type") == "setting"
      and _bj.get("target_id") == "daily_login_amount"
      and _bj["before_json"] == {"amount": 0}
      and _bj["after_json"] == {"amount": 5}, _bj)

uid_d, _ck = _register(_u + "d")
users.append(uid_d)
rows = _grant_rows(uid_d)
check("D4 operator-set amount grants on login",
      len(rows) == 1 and rows[0]["delta"] == 5, rows)

# D5: invalid amounts → 422, settings untouched
bad_all = True
for bad in (-1, 1001, 5.5, "5", None, True):
    st, _h, body = _req("POST", "/samelabs/economy/daily-amount",
                        {"amount": bad}, {"Cookie": op_ck})
    if st != 422:
        bad_all = False
        check(f"D5 amount={bad!r} → 422", False, f"{st} {body[:60]}")
with db() as con:
    row = con.execute(
        "select value from settings where key='daily_login_amount'").fetchone()
check("D5 invalid amounts all 422, settings untouched",
      bad_all and row is not None and row["value"] == "5", row)

# D6: non-operator refused
st, _h, _b = _req("POST", "/samelabs/economy/daily-amount", {"amount": 7},
                  {"Cookie": ck_b})
check("D6 non-operator POST refused", st in (401, 302, 303), st)

# D7: platform page shows the form with the current value
st, _h, body = _req("GET", "/samelabs/economy/platform", None,
                    {"Cookie": op_ck, "Accept": "text/html"})
check("D7 platform page renders daily-amount form",
      st == 200 and 'id="dailyAmountInput"' in body
      and 'value="5"' in body, st)

# ═══ cleanup ═════════════════════════════════════════════════════════
_set_amount(None)
with db() as con:
    purge_users(con, users)

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
