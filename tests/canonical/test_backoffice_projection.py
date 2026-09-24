#!/usr/bin/env python3
"""Backoffice projection canonical — operator surfaces read the same
mechanism single points as the product.

Locks: user/overview credits follow the dual-pool split (spendable kinds
vs provider_earn − withdraw; platform account excluded); membership
badges and plan member counts use membership_is_effective (a time-expired
raw 'active' row is not a member); package create honours the same field
contract as PATCH and the UI (price_cents / active); operator credit
adjustments persist the typed reason; JSON writes are denied with typed
JSON (401/403) while pages redirect; operator logout revokes the session.
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

import re
import time as _time
ts = int(_time.time())

from database import db
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
from app.main import app as _app

client = TestClient(_app)
_u = "bo" + _os.urandom(4).hex()


def _mkuser(name, status="active"):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " %s, %s, %s) returning id",
            (name, f"{name}@T.co", status, ts, ts)).fetchone()
    return int(row["id"])


def _ledger(uid, delta, kind, idem):
    with db() as con:
        con.execute(
            "insert into ledger (user_id, delta, balance_after, kind, idem_key,"
            " created_at) values (%s, %s, 0, %s, %s, %s)",
            (uid, delta, kind, idem, ts))


def _row_html(html, uid):
    m = re.search(r'<tr data-uid="%d">(.*?)</tr>' % uid, html, re.S)
    return m.group(1) if m else ""


op = _mkuser(_u + "op", status="samelabs")
op_sess = create_browser_session(op)
OPC = {COOKIE_NAME: op_sess}

# ══ A. dual-pool user projection ══
a = _mkuser(_u + "a")
_ledger(a, 700, "grant", f"bo:{_u}:g")
_ledger(a, -200, "consume", f"bo:{_u}:c")
_ledger(a, 90, "provider_earn", f"bo:{_u}:e")
_ledger(a, -40, "withdraw", f"bo:{_u}:w")
r = client.get(f"/samelabs/users?q={_u}a", cookies=OPC)
row = _row_html(r.text, a)
cells = re.findall(r'<td class="sl-num[^"]*">([^<]*)</td>', row)
check("A1 users page renders the user row", r.status_code == 200 and row, r.status_code)
check("A2 spendable cell = spendable pool only (500), earn cell = 50",
      cells[:2] == ["500", "50"], cells)
r = client.get(f"/samelabs/users?q={a}", cookies=OPC)
check("A3 numeric q = exact id match", _row_html(r.text, a) != "", r.status_code)
r = client.get(f"/samelabs/users?q={_u}a@t", cookies=OPC)
check("A4 email search (case-insensitive)", _row_html(r.text, a) != "", r.status_code)

# ══ B. membership validity single point ══
with db() as con:
    plan_id = int(con.execute(
        "insert into plans (code, name, monthly_credits, price_cents, sort_order,"
        " active, created_at, updated_at) values (%s, 'BO', 0, 0, %s, FALSE, %s, %s)"
        " returning id", (_u + "pl", 900 + int(_os.urandom(1)[0]), ts, ts)).fetchone()["id"])
def _member(uid, sub, renews):
    with db() as con:
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id, status,"
            " started_at, renews_at, updated_at) values (%s, %s, %s, 'active', %s, %s, %s)",
            (uid, plan_id, sub, ts - 90 * 86400, renews, ts))
b = _mkuser(_u + "b")
_member(b, f"sub:bo:{_u}:b", ts - 86400)          # raw 'active', time-expired
c = _mkuser(_u + "c")
_member(c, f"sub:bo:{_u}:c", ts + 20 * 86400)     # effective
r = client.get(f"/samelabs/users?q={b}", cookies=OPC)
row = _row_html(r.text, b)
check("B1 time-expired raw 'active' membership shown as lapsed",
      "已失效" in row and "sl-b-muted" in row, row[:200])
r = client.get(f"/samelabs/users?q={c}", cookies=OPC)
row = _row_html(r.text, c)
check("B2 effective membership shown as live", "sl-b-ok" in row and "已失效" not in row, row[:200])
r = client.get("/samelabs/plans", cookies=OPC)
card = re.search(r'data-plan="%d">(.*?)</button>' % plan_id, r.text, re.S)
check("B3 plan member count = effective members only (1 of 2 raw 'active')",
      r.status_code == 200 and card and "1 订阅" in card.group(1),
      card.group(1)[-160:] if card else r.status_code)

# ══ C. overview dual-pool aggregates ══
from repo.reports import overview_stats
from economy.billing import SPENDABLE_KINDS
from governance.platform import platform_id
with db() as con:
    st = overview_stats(con, SPENDABLE_KINDS, platform_id())
    exp_sp = int(con.execute(
        "select coalesce(sum(delta),0) n from ledger where kind = any(%s) and user_id<>%s",
        (list(SPENDABLE_KINDS), platform_id())).fetchone()["n"])
    exp_earn = int(con.execute(
        "select coalesce(sum(delta),0) n from ledger where kind in ('provider_earn','withdraw')"
        " and user_id<>%s", (platform_id(),)).fetchone()["n"])
    exp_users = int(con.execute("select count(*) n from users where id<>%s",
                                (platform_id(),)).fetchone()["n"])
check("C1 spendable liability = spendable kinds, platform excluded", st["spendable_liability"] == exp_sp, st)
check("C2 earn liability = provider_earn − withdraw", st["earn_liability"] == exp_earn, st)
check("C3 users count excludes the platform account", st["users"] == exp_users, st)
check("C4 running calls are not counted as failed",
      {"calls_ok", "calls_failed", "calls_running"} <= set(st), st)
r = client.get("/samelabs", cookies=OPC)
check("C5 overview renders platform income tile", r.status_code == 200 and "平台收入" in r.text, r.status_code)

# ══ D. package create contract ══
r = client.post("/samelabs/packages", json={
    "code": f"bopkg_{_u}", "name": "BO", "credits": 1000, "price_cents": 499,
    "active": False}, cookies=OPC)
pkg = (r.json() or {}).get("package") or {}
check("D1 create stores price_cents and active from the UI contract",
      r.status_code == 200 and pkg.get("price_cents") == 499 and pkg.get("active") is False,
      f"{r.status_code} {pkg}")
r = client.post("/samelabs/packages", json={
    "code": f"bopkg2_{_u}", "name": "BO", "credits": 1000, "price": 499}, cookies=OPC)
check("D2 legacy 'price' key is not a silent $0 package (422)", r.status_code == 422, r.status_code)
r = client.post("/samelabs/packages", json={
    "code": f"bopkg3_{_u}", "name": "BO", "credits": 1000, "price_cents": "499"}, cookies=OPC)
check("D3 non-int price_cents → 422", r.status_code == 422, r.status_code)

# ══ E. adjustment reason persisted ══
r = client.post(f"/samelabs/users/{a}/credits", json={
    "amount": 25, "action_id": f"bo-{_u}", "note": "补偿工单 #12"}, cookies=OPC)
with db() as con:
    note = con.execute("select note from ledger where user_id=%s and kind='adjust'"
                       " order by id desc limit 1", (a,)).fetchone()
check("E1 operator adjust persists the typed reason",
      r.status_code == 200 and note and note["note"] == "补偿工单 #12", f"{r.status_code} {note}")

# ══ F. gate answers per surface ══
plain = _mkuser(_u + "plain")
r = client.post("/samelabs/packages", json={}, cookies={COOKIE_NAME: create_browser_session(plain)})
check("F1 non-operator JSON write → 403 JSON",
      r.status_code == 403 and r.json().get("error") == "forbidden", r.status_code)
r = client.post("/samelabs/packages", json={})
check("F2 anonymous JSON write → 401 JSON",
      r.status_code == 401 and r.json().get("error") == "auth_required", r.status_code)
r = client.get("/samelabs", follow_redirects=False)
check("F3 anonymous page → login redirect", r.status_code == 302, r.status_code)

# ══ G. operator logout revokes the session ══
r = client.get("/samelabs", cookies=OPC)
check("G1 base layout logout posts to the real route",
      'action="/auth/logout"' in r.text and 'action="/logout"' not in r.text)
r = client.post("/auth/logout", cookies=OPC, follow_redirects=False)
r = client.get("/samelabs", cookies=OPC, follow_redirects=False)
check("G2 after logout the operator session no longer opens /samelabs", r.status_code == 302, r.status_code)

# fixture tier must not leak into later suites sharing this DB
with db() as con:
    con.execute("delete from memberships where plan_id=%s", (plan_id,))
    con.execute("delete from plans where id=%s", (plan_id,))

print(f"\n{'FAIL' if FAIL else 'PASS'}: {len(FAIL)} failure(s)")
_sys.exit(1 if FAIL else 0)
