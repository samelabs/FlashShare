#!/usr/bin/env python3
"""Backoffice operations canonical — operator console surfaces.

Locks: every console page is operator-session only; orders/calls/shares
filters return exactly the matching facts; account status moves only
active ↔ suspended (self, other operators, the platform account and any
authority grant are refused) and suspension cuts the account's sessions
on the next request; share takedown has owner-delete semantics (public
link gone) and is audited; on-demand membership reconcile runs the login
compensation channel and is audited; the audit page escapes snapshots.
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
def check(name, ok, detail=""):
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:140]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import base64
import re
import time as _time
import uuid

from database import db
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
from app.main import app as _app
from governance.platform import platform_id

client = TestClient(_app)
ts = int(_time.time())
_u = "bx" + _os.urandom(4).hex()


def _mkuser(name, status="active"):
    with db() as con:
        return int(con.execute(
            "insert into users (username, email, password_hash, display_name, status,"
            " created_at, last_login_at) values (%s, %s, 'x', 'T', %s, %s, %s) returning id",
            (name, f"{name}@t.co", status, ts, ts)).fetchone()["id"])


def _audits(action, target_id):
    with db() as con:
        return con.execute("select * from operator_audit where action=%s and target_id=%s",
                           (action, str(target_id))).fetchall()


def _status(uid):
    with db() as con:
        return con.execute("select status from users where id=%s", (uid,)).fetchone()["status"]


op = _mkuser(_u + "op", "samelabs")
OPC = {COOKIE_NAME: create_browser_session(op)}
op2 = _mkuser(_u + "op2", "samelabs")
cust = _mkuser(_u + "cu")
cust_sess = create_browser_session(cust)

# ══ A. pages: operator session only ══
for path in ("/samelabs/orders", "/samelabs/members", "/samelabs/calls",
             "/samelabs/uploads", "/samelabs/audit", f"/samelabs/users/{cust}"):
    r = client.get(path, cookies=OPC)
    check(f"A1 {path} renders for operator", r.status_code == 200, r.status_code)
    r = client.get(path, cookies={COOKIE_NAME: cust_sess}, follow_redirects=False)
    check(f"A2 {path} redirects a non-operator", r.status_code == 302, r.status_code)

# ══ B. filters return exactly the matching facts ══
with db() as con:
    pkg = con.execute("insert into packages (code, name, credits, price_cents, active, created_at,"
                      " updated_at) values (%s, 'BX', 10, 100, TRUE, %s, %s) returning id",
                      (_u + "pk", ts, ts)).fetchone()["id"]
    o_paid = con.execute("insert into orders (user_id, kind, item_id, provider, status, amount_cents,"
                         " currency, created_at, provider_order_id) values (%s,'package',%s,'creem',"
                         "'paid',100,'USD',%s,%s) returning id",
                         (cust, pkg, ts, f"ord_{_u}_p")).fetchone()["id"]
    o_ref = con.execute("insert into orders (user_id, kind, item_id, provider, status, amount_cents,"
                        " currency, created_at, provider_order_id) values (%s,'package',%s,'creem',"
                        "'refunded',100,'USD',%s,%s) returning id",
                        (cust, pkg, ts, f"ord_{_u}_r")).fetchone()["id"]
    con.execute("insert into tool_calls (client_ref, user_id, tool_id, status, error, started_at,"
                " finished_at) values (%s, %s, 'bx_tool', 'failed', %s, %s, %s)",
                (uuid.uuid4().hex, cust, f"err_{_u}", ts, ts + 1))
r = client.get(f"/samelabs/orders?q={cust}&status=refunded", cookies=OPC)
check("B1 order filter: status + user", f"ord_{_u}_r" in r.text and f"ord_{_u}_p" not in r.text)
r = client.get(f"/samelabs/orders?q=ord_{_u}_p", cookies=OPC)
check("B2 order search by provider id", f"ord_{_u}_p" in r.text and f"ord_{_u}_r" not in r.text)
r = client.get(f"/samelabs/calls?status=failed&user={cust}", cookies=OPC)
check("B3 calls filter shows the failure code", f"err_{_u}" in r.text)
r = client.get(f"/samelabs/calls?status=ok&user={cust}", cookies=OPC)
check("B4 calls filter excludes other statuses", f"err_{_u}" not in r.text)
r = client.get("/samelabs/orders?status=bogus", cookies=OPC)
check("B5 unknown filter value ignored (no error)", r.status_code == 200)

# ══ C. account status: active ↔ suspended only ══
def _set(uid, status, cookies=OPC):
    return client.post(f"/samelabs/users/{uid}/status", json={"status": status}, cookies=cookies)

r = client.get("/api/me", cookies={COOKIE_NAME: cust_sess})
check("C0 customer session authenticates before suspension", r.status_code == 200, r.status_code)
r = _set(cust, "suspended")
check("C1 suspend → 200, status suspended, one audit",
      r.status_code == 200 and _status(cust) == "suspended"
      and len(_audits("user.suspend", cust)) == 1, r.text[:120])
r = client.get("/api/me", cookies={COOKIE_NAME: cust_sess})
check("C2 suspended account's session no longer authenticates", r.status_code == 401, r.status_code)
r = _set(cust, "active")
check("C3 reinstate → active, audited",
      r.status_code == 200 and _status(cust) == "active"
      and len(_audits("user.reinstate", cust)) == 1, r.text[:120])
r = _set(cust, "active")
check("C4 no-op transition writes no audit",
      r.status_code == 200 and r.json().get("changed") is False
      and len(_audits("user.reinstate", cust)) == 1, r.text[:120])
r = _set(op2, "suspended")
check("C5 another operator cannot be suspended (authority untouched)",
      r.status_code == 422 and _status(op2) == "samelabs", f"{r.status_code} {_status(op2)}")
r = _set(op, "suspended")
check("C6 operator cannot change own status", r.status_code == 422 and _status(op) == "samelabs")
r = _set(platform_id(), "active")
check("C7 platform account status not editable", r.status_code == 422 and _status(platform_id()) == "suspended")
r = _set(cust, "samelabs")
check("C8 authority grant is not a transition", r.status_code == 422 and _status(cust) == "active")
from repo import users as users_repo
with db() as con:
    none_row = users_repo.set_account_status(con, op2, "suspended", ts)
check("C9 SQL writer refuses rows outside active/suspended", none_row is None and _status(op2) == "samelabs")
try:
    with db() as con:
        users_repo.set_account_status(con, cust, "samelabs", ts)
    check("C10 SQL writer refuses authority statuses", False, "accepted")
except ValueError:
    check("C10 SQL writer refuses authority statuses", _status(cust) == "active")
r = client.post(f"/samelabs/users/{cust}/status", json={"status": "suspended"},
                cookies={COOKIE_NAME: create_browser_session(_mkuser(_u + "plain"))})
check("C11 non-operator write → 403", r.status_code == 403 and _status(cust) == "active")

# ══ D. share takedown ══
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
body = (b"".join(f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in {"expiryDays": "1", "allowedDownloads": "50",
                              "client_ref": uuid.uuid4().hex}.items())
        + b'--B\r\nContent-Disposition: form-data; name="file"; filename="t.png"\r\n'
          b'Content-Type: image/png\r\n\r\n' + PNG + b"\r\n--B--\r\n")
r = client.post("/api/uploads", content=body, cookies={COOKIE_NAME: cust_sess},
                headers={"Content-Type": "multipart/form-data; boundary=B"})
pid = ((r.json() or {}).get("file") or {}).get("publicId")
check("D0 share landed", r.status_code == 200 and pid, r.text[:120])
r = client.get(f"/samelabs/uploads?q={pid}", cookies=OPC)
check("D1 share search by public id", pid and pid in r.text)
r = client.post(f"/samelabs/uploads/{pid}/takedown", json={},
                cookies={COOKIE_NAME: cust_sess})
check("D2 non-operator takedown → 403", r.status_code == 403)
r = client.post(f"/samelabs/uploads/{pid}/takedown", json={}, cookies=OPC)
with db() as con:
    gone = con.execute("select 1 from uploads where public_id=%s", (pid,)).fetchone() is None
aud = _audits("upload.takedown", pid)
check("D3 takedown removes the public mapping, one audit with the before snapshot",
      r.status_code == 200 and gone and len(aud) == 1
      and aud[0]["before_json"]["file_name"] == "t.png", r.text[:120])
r = client.get(f"/f/{pid}", follow_redirects=False)
check("D4 public link is gone", r.status_code == 404, r.status_code)
r = client.post(f"/samelabs/uploads/{pid}/takedown", json={}, cookies=OPC)
check("D5 repeat takedown → 404, no second audit",
      r.status_code == 404 and len(_audits("upload.takedown", pid)) == 1, r.status_code)
r = client.post("/samelabs/uploads/bad!id/takedown", json={}, cookies=OPC)
check("D6 malformed id → 400", r.status_code == 400, r.status_code)

# ══ E. membership reconcile on demand ══
r = client.post(f"/samelabs/users/{cust}/reconcile", json={}, cookies=OPC)
aud = _audits("membership.reconcile", cust)
check("E1 reconcile without membership: nothing checked, audited",
      r.status_code == 200 and r.json().get("checked") == 0 and len(aud) == 1
      and aud[0]["after_json"]["credits"] == 0, r.text[:120])

# ══ F. audit page escapes snapshots ══
client.post("/samelabs/packages", json={"code": _u + "xss", "name": "<script>x</script>",
                                        "credits": 1, "price_cents": 100}, cookies=OPC)
r = client.get("/samelabs/audit?action=package.create", cookies=OPC)
check("F1 audit snapshot rendered escaped",
      r.status_code == 200 and "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text)

# ══ G. user detail ══
r = client.get(f"/samelabs/users/{cust}", cookies=OPC)
check("G1 detail shows identity and the user's orders",
      r.status_code == 200 and f"#{cust}" in r.text
      and f'<td class="sl-mono sl-dim">{o_ref}</td>' in r.text
      and f'<td class="sl-mono sl-dim">{o_paid}</td>' in r.text)
check("G2 detail offers suspend for an active customer",
      'data-op="status"' in r.text and 'data-status="suspended"' in r.text)
r = client.get(f"/samelabs/users/{op2}", cookies=OPC)
check("G3 detail offers no status action on an operator", 'data-op="status"' not in r.text)
r = client.get("/samelabs/users/999999999", cookies=OPC, follow_redirects=False)
check("G4 unknown user → back to list", r.status_code == 302)

print(f"\n{'FAIL' if FAIL else 'PASS'}: {len(FAIL)} failure(s)")
_sys.exit(1 if FAIL else 0)
