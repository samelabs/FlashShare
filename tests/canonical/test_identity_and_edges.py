#!/usr/bin/env python3
"""Identity & edge contracts canonical.

Locks: X OAuth state is bound to the initiating browser (a callback URL
minted elsewhere is refused and burned); email identity is
case-insensitive (canonical stored form, CI login, CI uniqueness at the
DB); package fulfillment never grants on a reversed order and serializes
with the reversal (reversal-before-fulfillment converges to zero credits);
MCP rejects non-object params/arguments as -32602 and reports ingress
conflicts inside the JSON-RPC server-error range; expired shares are not
tool sources; the anonymous client-error tap is bounded.
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
_os.environ["FLASHSHARE_X_CLIENT_ID"] = "x-client-test"
_os.environ["FLASHSHARE_X_CLIENT_SECRET"] = "x-secret-test"
_os.environ["CREEM_CREDITS_PRODUCT_ID"] = "prod_credits_ie"
_os.environ["CREEM_CREDITS_CURRENCY"] = "USD"
_os.environ.setdefault("CREEM_API_KEY", "test-key")

FAIL = []
def check(name, ok, detail=""):
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:140]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import base64
import time as _time
import urllib.parse
import uuid

import psycopg2
import psycopg2.errors
from database import db
from fastapi.testclient import TestClient
from app.main import app as _app
from app.routes import auth_route as _auth_route

client = TestClient(_app)
ts = int(_time.time())
_u = "ie" + _os.urandom(3).hex()


def _state_of(resp):
    loc = resp.headers.get("location", "")
    return urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("state", [""])[0]


def _state_rows(state):
    with db() as con:
        return con.execute("select count(*) n from oauth_states where state=%s",
                           (state,)).fetchone()["n"]


# ══ A. OAuth state bound to the initiating browser ══
r = client.get("/auth/x/login", follow_redirects=False)
state = _state_of(r)
setc = r.headers.get("set-cookie", "")
check("A1 x login sets an HttpOnly state cookie scoped to /auth/x",
      r.status_code in (302, 307) and state and f"flashshare_oauth={state}" in setc
      and "HttpOnly" in setc and "Path=/auth/x" in setc, setc)

_auth_route.exchange_x_token = lambda code, verifier: {"access_token": "tok"}
_x_id = "x" + _os.urandom(4).hex()
_auth_route.fetch_x_user = lambda tok: {"id": _x_id, "username": _u + "xu", "name": "XU"}

r = client.get(f"/auth/x/callback?code=c&state={state}", follow_redirects=False,
               headers={"Cookie": ""})
check("A2 callback without the browser-bound cookie is refused (400)",
      r.status_code == 400 and "flashshare_session=" not in r.headers.get("set-cookie", ""),
      r.status_code)
check("A3 the refused state is burned", _state_rows(state) == 0)

r = client.get("/auth/x/login", follow_redirects=False)
s2 = _state_of(r)
r = client.get(f"/auth/x/callback?code=c&state={s2}", follow_redirects=False,
               headers={"Cookie": f"flashshare_oauth={state}"})
check("A4 callback with another flow's cookie is refused", r.status_code == 400, r.status_code)

r = client.get("/auth/x/login", follow_redirects=False)
s3 = _state_of(r)
r = client.get(f"/auth/x/callback?code=c&state={s3}", follow_redirects=False,
               headers={"Cookie": f"flashshare_oauth={s3}"})
check("A5 bound callback logs in and clears the state cookie",
      r.status_code == 302 and "flashshare_session=" in r.headers.get("set-cookie", "")
      and 'flashshare_oauth=""' in r.headers.get("set-cookie", ""),
      f"{r.status_code} {r.headers.get('set-cookie', '')[:160]}")

# ══ B. email identity is case-insensitive ══
def _form(fields):
    body = "".join(f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                   for k, v in fields.items()) + "--B--\r\n"
    return body.encode(), {"Content-Type": "multipart/form-data; boundary=B"}

mixed = f"{_u}.MiXed@Case.CO"
b, h = _form({"username": _u + "reg", "email": mixed, "password": "Test12345",
              "password_confirm": "Test12345"})
r = client.post("/auth/register", content=b, headers=h, follow_redirects=False)
with db() as con:
    stored = con.execute("select email from users where username=%s", (_u + "reg",)).fetchone()
check("B1 registration stores the canonical lower-case email",
      r.status_code == 302 and stored and stored["email"] == mixed.lower(), stored)
b, h = _form({"login": mixed.upper(), "password": "Test12345"})
r = client.post("/auth/login", content=b, headers=h, follow_redirects=False)
check("B2 login by email is case-insensitive",
      r.status_code == 302 and "flashshare_session=" in r.headers.get("set-cookie", ""), r.status_code)
b, h = _form({"username": _u + "reg2", "email": mixed.upper(), "password": "Test12345",
              "password_confirm": "Test12345"})
r = client.post("/auth/register", content=b, headers=h, follow_redirects=False)
with db() as con:
    dup = con.execute("select 1 from users where username=%s", (_u + "reg2",)).fetchone()
check("B3 case-variant re-registration refused", r.status_code == 200 and dup is None, r.status_code)
try:
    with db() as con:
        con.execute("insert into users (username, email, display_name, status, created_at,"
                    " last_login_at) values (%s, %s, 'T', 'active', %s, %s)",
                    (_u + "raw", mixed.title(), ts, ts))
    check("B4 DB enforces lower(email) uniqueness", False, "insert accepted")
except psycopg2.errors.UniqueViolation as exc:
    check("B4 DB enforces lower(email) uniqueness",
          "users_email_lower_uk" in str(exc), exc)
from identity.auth import update_profile
with db() as con:
    other = con.execute("insert into users (username, email, display_name, status, created_at,"
                        " last_login_at) values (%s, %s, 'T', 'active', %s, %s) returning id",
                        (_u + "oth", f"{_u}.other@t.co", ts, ts)).fetchone()["id"]
try:
    update_profile(other, email=mixed.upper())
    check("B5 profile email change to a case-variant of a taken email refused", False, "accepted")
except ValueError as exc:
    check("B5 profile email change to a case-variant of a taken email refused",
          str(exc) == "err_email_used", exc)

# ══ C. fulfillment vs reversal ══
from economy import creem as _creem
from economy import reconcile as _reconcile
from economy.reverse import reverse_payment
from repo.orders import insert_order, insert_package, set_provider_order_id

with db() as con:
    buyer = con.execute("insert into users (username, email, display_name, status, created_at,"
                        " last_login_at) values (%s, %s, 'T', 'active', %s, %s) returning id",
                        (_u + "buy", f"{_u}.buy@t.co", ts, ts)).fetchone()["id"]
    pkg = insert_package(con, _u + "pk", "IE", 100, 500, ts)

def _order(prov):
    with db() as con:
        o = insert_order(con, buyer, "package", pkg["id"], "creem", 500, "USD", ts,
                         credits_snapshot=100)
        set_provider_order_id(con, o["id"], prov)
    return o["id"]

def _grants(oid):
    with db() as con:
        return con.execute("select count(*) n from ledger where user_id=%s and idem_key=%s",
                           (buyer, f"order:{oid}")).fetchone()["n"]

def _status(oid):
    with db() as con:
        return con.execute("select status from orders where id=%s", (oid,)).fetchone()["status"]

o1 = _order(f"ord_{_u}_1")
with db() as con:
    con.execute("update orders set status='refunded' where id=%s", (o1,))
res = _creem.fulfill(f"ord_{_u}_1", "", 500, product_id="prod_credits_ie", currency="USD")
check("C1 fulfillment on a refunded order grants nothing and keeps the terminal state",
      res.get("granted") is False and _grants(o1) == 0 and _status(o1) == "refunded", res)

o2 = _order(f"ord_{_u}_2")
_txn = "txn_" + _u
_reconcile.fetch_transaction = lambda tid: {"id": tid, "status": "refunded",
                                            "type": "payment", "order": f"ord_{_u}_2"}
rv = reverse_payment("refund.created", {"id": "evt_" + _u, "object": {
    "object": "refund", "transaction": {"id": _txn}}})
res = _creem.fulfill(f"ord_{_u}_2", "", 500, product_id="prod_credits_ie", currency="USD")
check("C2 reversal before fulfillment converges to zero credits",
      rv.get("reason") == "no_grant" and res.get("granted") is False
      and _grants(o2) == 0 and _status(o2) == "refunded", f"{rv} {res}")

o3 = _order(f"ord_{_u}_3")
res = _creem.fulfill(f"ord_{_u}_3", "", 500, product_id="prod_credits_ie", currency="USD")
res2 = _creem.fulfill(f"ord_{_u}_3", "", 500, product_id="prod_credits_ie", currency="USD")
check("C3 normal fulfillment grants once, replay idempotent, paid",
      res.get("granted") is True and _grants(o3) == 1 and _status(o3) == "paid", f"{res} {res2}")

# ══ D. MCP protocol shape ══
from identity.apitokens import create_token
tok = create_token(buyer, "ie")["token"]
H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}, headers=H)
check("D1 non-object params → -32602", r.status_code == 200
      and (r.json().get("error") or {}).get("code") == -32602, r.text[:120])
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "x", "arguments": ["a"]}}, headers=H)
check("D2 non-object arguments → -32602", r.status_code == 200
      and (r.json().get("error") or {}).get("code") == -32602, r.text[:120])
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
from app.routes import mcp as _mcp_mod
_tool = next((t["name"] for t in _mcp_mod._tool_specs({"id": buyer, "username": _u + "buy"})
              if t["name"] != "catalog_search"), None)
ref = "ie-" + uuid.uuid4().hex
def _call(body):
    return client.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": _tool, "arguments": {"files": [{"filename": "a.png",
            "b64": base64.b64encode(body).decode()}], "client_ref": ref,
            "delivery": {"expiry_days": 1, "allowed_downloads": 1}}}})
_call(_PNG)
r = _call(_PNG + b"\x00")
err = r.json().get("error") or {}
check("D3 ingress conflict code is inside the JSON-RPC server-error range",
      err.get("message") == "idempotency_conflict" and -32099 <= err.get("code", 0) <= -32000,
      r.text[:160])

# ══ E. expired share is not a tool source ══
from execution.source import resolve_upload_sync
b, h = _form({"expiryDays": "1", "allowedDownloads": "50", "client_ref": uuid.uuid4().hex})
parts = (b[:-len(b"--B--\r\n")] + b'--B\r\nContent-Disposition: form-data; name="file"; '
         b'filename="s.png"\r\nContent-Type: image/png\r\n\r\n' + _PNG + b"\r\n--B--\r\n")
with db() as con:
    urow = con.execute("select * from users where id=%s", (buyer,)).fetchone()
from app.web_auth import create_browser_session
r = client.post("/api/uploads", content=parts, headers={
    **h, "Cookie": f"flashshare_session={create_browser_session(buyer)}"})
pid = ((r.json() or {}).get("file") or {}).get("publicId")
up, e = resolve_upload_sync(pid, dict(urow), "")
check("E1 live share resolves as tool source", up is not None and e is None, f"{r.status_code} {e}")
with db() as con:
    con.execute("update uploads set expire_at=%s where public_id=%s", (ts - 10, pid))
up, e = resolve_upload_sync(pid, dict(urow), "")
check("E2 expired share → not_found", up is None and e == "not_found", e)

# ══ F. client-error tap bounded ══
r = client.post("/api/client-error", content=b"x" * 20000, headers={"Content-Type": "application/json"})
check("F1 oversized diagnostics body acknowledged without processing", r.status_code == 200)
r = client.post("/api/client-error", json=["not", "a", "dict"])
check("F2 non-object diagnostics body acknowledged", r.status_code == 200)

print(f"\n{'FAIL' if FAIL else 'PASS'}: {len(FAIL)} failure(s)")
_sys.exit(1 if FAIL else 0)
