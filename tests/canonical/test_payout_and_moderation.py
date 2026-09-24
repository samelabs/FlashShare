#!/usr/bin/env python3
"""Payout settlement & tool moderation canonical.

Tool moderation: an operator hold on a tool key delists it and no owner
write (price edit, delete + re-create) nor pricing toggle can relist it
until released; the owner never flips the commerce switch.

Withdrawal: pending → approved (earn-pool debit) → paid (payout reference
required) | failed (the exact debit returns to the earn pool, once); the
payout target is the one frozen at request time. Every settlement is one
transaction with its audit fact.
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

import time as _time

from database import db
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
from app.main import app as _app

client = TestClient(_app)
ts = int(_time.time())
_u = "pm" + _os.urandom(4).hex()


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


op = _mkuser(_u + "op", "samelabs")
OPC = {COOKIE_NAME: create_browser_session(op)}

# ══ W. withdrawal settlement ══
from repo import ledger as ledger_repo
from economy.earnings import earnings_summary
prov = _mkuser(_u + "pv")
PVC = {COOKIE_NAME: create_browser_session(prov)}
with db() as con:
    con.execute("update users set payout_channel=%s where id=%s", ("paypal:orig@t.co", prov))
    con.execute("insert into ledger (user_id, delta, balance_after, kind, idem_key, created_at)"
                " values (%s, 25000, 25000, 'provider_earn', %s, %s)", (prov, f"pm:{_u}:e", ts))

def _earn():
    with db() as con:
        return ledger_repo.earn_pool_balance(con, prov)

def _wd(amount):
    r = client.post("/api/earnings/withdraw", json={"amount": amount}, cookies=PVC)
    with db() as con:
        return r, con.execute("select * from withdrawals where user_id=%s order by id desc limit 1",
                              (prov,)).fetchone()

r, w1 = _wd(15000)
check("W1 request lands pending with the payout target frozen",
      r.status_code == 200 and w1["status"] == "pending"
      and w1["payout_channel"] == "paypal:orig@t.co", r.text[:120])
with db() as con:
    con.execute("update users set payout_channel=%s where id=%s", ("paypal:changed@t.co", prov))
r = client.get("/samelabs/withdrawals", cookies=OPC)
check("W2 operator queue shows the request-time target, not the later edit",
      "paypal:orig@t.co" in r.text and "paypal:changed@t.co" not in r.text)
r = client.post(f"/samelabs/withdrawals/{w1['id']}/settle", json={"outcome": "paid", "txn_ref": "X"},
                cookies=OPC)
check("W3 pending cannot be settled (must be approved first)",
      r.status_code == 422 and r.json().get("error") == "not_approved", r.text[:120])
client.post(f"/samelabs/withdrawals/{w1['id']}/process", json={"approve": True}, cookies=OPC)
check("W4 approval debits the earn pool", _earn() == 10000, _earn())
r = client.post(f"/samelabs/withdrawals/{w1['id']}/settle", json={"outcome": "paid"}, cookies=OPC)
check("W5 paid without payout reference → 422",
      r.status_code == 422 and r.json().get("error") == "txn_ref_required", r.text[:120])
r = client.post(f"/samelabs/withdrawals/{w1['id']}/settle",
                json={"outcome": "paid", "txn_ref": "PP-123"}, cookies=OPC)
with db() as con:
    w1b = con.execute("select * from withdrawals where id=%s", (w1["id"],)).fetchone()
check("W6 paid: status paid + reference stored + one audit, earn pool unchanged",
      r.status_code == 200 and w1b["status"] == "paid" and w1b["txn_ref"] == "PP-123"
      and len(_audits("withdrawal.paid", w1["id"])) == 1 and _earn() == 10000, r.text[:120])

r, w2 = _wd(10000)
check("W6b second request lands", r.status_code == 200 and w2["id"] != w1["id"], r.text[:120])
client.post(f"/samelabs/withdrawals/{w2['id']}/process", json={"approve": True}, cookies=OPC)
check("W7 second approval debits the rest", _earn() == 0, _earn())
r = client.post(f"/samelabs/withdrawals/{w2['id']}/settle", json={"outcome": "failed"}, cookies=OPC)
with db() as con:
    w2b = con.execute("select * from withdrawals where id=%s", (w2["id"],)).fetchone()
    rev = con.execute("select count(*) n, coalesce(sum(delta),0) s from ledger where user_id=%s"
                      " and idem_key=%s", (prov, f"withdrawal-reversal:{w2['id']}")).fetchone()
s = earnings_summary(prov)
check("W8 failed: status failed, debit returned once, withdrawable again",
      r.status_code == 200 and w2b["status"] == "failed" and rev["n"] == 1 and rev["s"] == 10000
      and _earn() == 10000 and s["available"] == 10000 and s["withdrawn"] == 15000,
      f"{r.text[:80]} earn={_earn()} summary={s}")
check("W9 failure audited", len(_audits("withdrawal.fail", w2["id"])) == 1)
r = client.post(f"/samelabs/withdrawals/{w2['id']}/settle", json={"outcome": "failed"}, cookies=OPC)
with db() as con:
    rev2 = con.execute("select count(*) n from ledger where user_id=%s and idem_key=%s",
                       (prov, f"withdrawal-reversal:{w2['id']}")).fetchone()["n"]
check("W10 re-settling a failed request → 422, no second reversal",
      r.status_code == 422 and rev2 == 1 and _earn() == 10000, r.text[:120])
r = client.post(f"/samelabs/withdrawals/{w2['id']}/settle", json={"outcome": "bogus"}, cookies=OPC)
check("W11 unknown outcome → 422", r.status_code == 422)
r = client.post(f"/samelabs/withdrawals/{w1['id']}/settle", json={"outcome": "failed"},
                cookies=PVC)
check("W12 non-operator cannot settle", r.status_code == 403)

# ══ M. tool moderation hold ══
import socket as _socket
import remote as _remote_mod
_remote_mod._resolve_host = lambda host, port: [
    (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
owner_name = _u + "ow"
owner = _mkuser(owner_name)
OWC = {COOKIE_NAME: create_browser_session(owner)}
tid = "mod" + _os.urandom(3).hex()
key = f"{owner_name}/{tid}"
_manifest = {"tool_id": tid, "label": "Mod", "url": "https://pub.example/x",
             "format_families": "image", "accept": [".png"], "cost": 5}
def _publish(k):
    # public listing normally follows a passing self-test; fixture shortcut
    with db() as con:
        con.execute("update platform_tools set visibility='public' where tool_key=%s", (k,))
r = client.post("/api/usertools", json=_manifest, cookies=OWC)
_publish(key)
from governance.pricing import get_pricing
from governance.catalog import list_catalog
def _listed():
    return any(t["id"] == key for t in list_catalog(None))
check("M0 community tool created and listed", r.status_code == 200 and _listed(), r.text[:120])

r = client.post("/samelabs/tool-holds", json={"tool_key": key, "reason": "abuse"}, cookies=OPC)
check("M1 hold → delisted + one audit",
      r.status_code == 200 and not _listed() and get_pricing(key)["enabled"] is False
      and len(_audits("tool.hold", key)) == 1, r.text[:120])
r = client.post("/samelabs/tool-holds", json={"tool_key": key}, cookies=OPC)
check("M2 repeat hold → 409 already_held", r.status_code == 409)
r = client.patch(f"/api/usertools/{tid}", json={"cost": 7}, cookies=OWC)
check("M3 owner price edit applies the price but cannot relist (B3)",
      r.status_code == 200 and get_pricing(key)["cost"] == 7 and not _listed(), r.text[:120])
r = client.get("/api/usertools", cookies=OWC)
_mine = [t for t in r.json().get("tools", []) if t["tool_key"] == key]
check("M4 owner sees the hold", _mine and _mine[0]["held"] is True, _mine[:1])
r = client.post(f"/samelabs/tools/{key}/pricing", json={"cost": 7, "enabled": True}, cookies=OPC)
check("M5 even the pricing toggle cannot switch a held key on",
      get_pricing(key)["enabled"] is False and not _listed(), r.status_code)
r = client.delete(f"/api/usertools/{tid}", cookies=OWC)
r = client.post("/api/usertools", json=_manifest, cookies=OWC)
check("M6 delete + re-create of a held key → 409 tool_held",
      r.status_code == 409 and r.json().get("error") == "tool_held", r.text[:120])
r = client.get("/samelabs/tools", cookies=OPC)
check("M7 console marks the held key", r.status_code == 200 and "审核下架" in r.text)
r = client.post("/samelabs/tool-holds/release", json={"tool_key": key}, cookies=OPC)
check("M8 release of a deleted tool: hold gone, stays delisted, audited",
      r.status_code == 200 and r.json().get("relisted") is False and not _listed()
      and len(_audits("tool.release", key)) == 1, r.text[:120])
r = client.post("/api/usertools", json=_manifest, cookies=OWC)
check("M9 after release the owner can publish the key again", r.status_code == 200, r.text[:120])

# operator pause without hold (plain commerce switch) is also not undone by the owner
tid2 = "mod2" + _os.urandom(3).hex(); key2 = f"{owner_name}/{tid2}"
client.post("/api/usertools", json={**_manifest, "tool_id": tid2}, cookies=OWC)
_publish(key2)
r = client.post(f"/samelabs/tools/{key2}/pricing", json={"cost": 5, "enabled": False}, cookies=OPC)
check("M10a console pricing route reaches namespaced community keys",
      r.status_code == 200 and get_pricing(key2)["enabled"] is False, r.status_code)
r = client.patch(f"/samelabs/tools/{key2}/presentation", json={"featured": True, "sort_order": 3},
                 cookies=OPC)
check("M10b console presentation route reaches namespaced community keys", r.status_code == 200,
      r.status_code)
client.patch(f"/api/usertools/{tid2}", json={"cost": 9}, cookies=OWC)
check("M10 owner price edit keeps an operator pause (B3)",
      get_pricing(key2)["enabled"] is False and get_pricing(key2)["cost"] == 9)

print(f"\n{'FAIL' if FAIL else 'PASS'}: {len(FAIL)} failure(s)")
_sys.exit(1 if FAIL else 0)
