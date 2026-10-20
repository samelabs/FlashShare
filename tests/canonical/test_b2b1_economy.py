#!/usr/bin/env python3
"""Economy closure canonical — commission fail-closed, payout_failed,
partial-refund review queue, upgrade-refund boundary, outputs enforcement.

Real gate.run / orchestrator / earnings / reverse / webhook route; Creem
isolated by module fakes; DB isolated (fail-closed env gate).

  commission: missing/invalid/out-of-range rate → refused BEFORE consume
              (zero charge); system tools run; operator write audited;
              0/100 boundary settles the split exactly.
  withdrawal: approved → payout_failed returns the exact amount to the
              earn pool in ONE transaction (idem payout-failed:{id});
              replay never double-refunds; CHECK constraint holds.
  reviews:    partially_refunded → review row + 200, zero mutation;
              replay converges; resolve is a status flip + audit.
  upgrade:    base-payment classification comes from the period payment
              record, not ledger grants — a zero-credit paid plan's
              upgrade refund never ends the membership; the base payment
              refund does (zero-credit periods included).
  outputs:    a result item kind outside the declared outputs fails the
              call through the standard refund chain (local + remote).
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:130]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import asyncio
import json as _json
import time
ts = int(time.time())
_u = "b2" + _os.urandom(4).hex()

from database import db
from economy.billing import grant as _grant, spendable_balance
from repo import operator_audit

def _mkuser(name, payout_channel=None):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at, payout_channel)"
            " values (%s, %s, 'x', 'T', 'active', %s, %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts, payout_channel)).fetchone()
    return int(row["id"])

# ══════════ 1. commission: fail-closed settlement split + operator write ══════════
from execution import gate
from execution.results import StagedItems, StagedFileItem
from execution.invocation import ToolInvocation, SourceFile
from governance.commission import commission_rate

def _set_rate(v):
    with db() as con:
        if v is None:
            con.execute("delete from settings where key='commission_rate'")
        else:
            con.execute(
                "insert into settings (key, value, updated_at) values"
                " ('commission_rate', %s, %s) on conflict (key) do update"
                " set value = excluded.value", (str(v), ts))

def _mktool(tool_id, owner_id=None, cost=100, outputs_json='["file"]'):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled,"
            " outputs_json, created_at, updated_at, accept_json, format_family)"
            " values (%s, %s, %s, 'local', %s,"
            " 'public', TRUE, %s, %s, %s, '[\".png\"]', 'image')",
            (tool_id, owner_id, tool_id, _json.dumps({"en": "B2"}),
             outputs_json, ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts))

_ref_seq = [0]
def _inv(tool_id, ref=None):
    if ref is None:
        _ref_seq[0] += 1
        ref = f"fix:{tool_id}:{_ref_seq[0]}:{_u}"
    return ToolInvocation(tool_id=tool_id, files=[SourceFile("p", "t")],
                          params={},
                          delivery={"expiry_days": 7, "allowed_downloads": 5},
                          client_ref=ref)

def _mkstaged(tag, kind="file"):
    if kind == "file":
        return StagedItems(order=["file"], inline=[], files=[StagedFileItem(
            gokapi_id=f"gk_{_u}_{tag}", filename="o.bin",
            content_type="application/octet-stream", size=10,
            expiry_days=7, allowed_downloads=5,
            expire_at=ts + 86400, url_hotlink="")], ip="127.0.0.1")
    return StagedItems(order=[kind], inline=[{"kind": kind, "text": "hi"}],
                       files=[], ip="127.0.0.1")

def _staged_fn(tag):
    async def _f():
        return _mkstaged(tag)
    return _f

def _run(user, tool_id, ref, staged_fn, owner=None, outputs_json=None):
    tool = {"id": tool_id, "tool_key": tool_id,
            "owner_id": owner}
    if outputs_json is not None:
        tool["outputs_json"] = outputs_json
    return asyncio.run(gate.run(
        {"id": user}, tool, _inv(tool_id, ref), staged_fn))

def _split_for(call_id):
    with db() as con:
        e = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='provider_earn' and ref_id=%s", (str(call_id),)).fetchone()
        c = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='platform_commission' and ref_id=%s",
            (str(call_id),)).fetchone()
        r = con.execute(
            "select coalesce(sum(delta),0) s from ledger where"
            " kind='platform_revenue' and ref_id=%s",
            (str(call_id),)).fetchone()
    return int(e["s"]), int(c["s"]), int(r["s"])

owner = _mkuser(_u + "dev")
_set_rate(30)

# boundary rate 0 → provider keeps everything, platform nothing
u0 = _mkuser(_u + "u0")
t0 = f"b2t0_{_u}"
_mktool(t0, owner_id=owner, cost=100)
_grant(u0, 10000, "manual_topup", note="b2")
_set_rate(0)
res0, err0, _ = _run(u0, t0, None, _staged_fn("r0"), owner=owner)
e0, c0, r0 = _split_for(res0["call_id"]) if res0 else (0, 0, 0)
check("K1 rate 0 → payout 100 / commission 0 / revenue 0",
      res0 is not None and err0 is None and (e0, c0, r0) == (100, 0, 0),
      f"{err0} {e0},{c0},{r0}")
# boundary rate 100 → platform takes all commission, provider nothing
_set_rate(100)
res100, err100, _ = _run(u0, t0, None, _staged_fn("r100"), owner=owner)
e100, c100, r100 = _split_for(res100["call_id"]) if res100 else (0, 0, 0)
check("K2 rate 100 → payout 0 / commission 100 / revenue 0",
      res100 is not None and err100 is None and (e100, c100, r100) == (0, 100, 0),
      f"{err100} {e100},{c100},{r100}")
_set_rate(30)

# operator write: audited old → new; only whole 0..100 ints accepted
from app.web_auth import create_browser_session, COOKIE_NAME
op = _mkuser(_u + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess = create_browser_session(op)
from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)
OP_H = {"Cookie": f"{COOKIE_NAME}={op_sess}"}

r = client.post("/samelabs/economy/commission", json={"rate": 55}, headers=OP_H)
with db() as con:
    val = con.execute("select value from settings where"
                      " key='commission_rate'").fetchone()["value"]
    aud = con.execute(
        "select before_json, after_json from operator_audit where"
        " action='commission.update' order by id desc limit 1").fetchone()
check("K3 operator set 55 → settings + audit 30→55",
      r.status_code == 200 and r.json().get("rate") == 55 and val == "55"
      and aud and aud["before_json"]["rate"] == 30
      and aud["after_json"]["rate"] == 55,
      f"{r.status_code} {r.text} val={val} aud={aud}")
for bad in ({"rate": 101}, {"rate": -1}, {"rate": "50"}, {"rate": 1.5},
            {"rate": True}, {}):
    r = client.post("/samelabs/economy/commission", json=bad, headers=OP_H)
    if r.status_code != 422:
        check(f"K4 invalid commission {bad} → 422", False, r.text)
        break
else:
    with db() as con:
        val = con.execute("select value from settings where"
                          " key='commission_rate'").fetchone()["value"]
    check("K4 every non-int/out-of-range write → 422, value untouched",
          val == "55", str(val))
r = client.post("/samelabs/economy/commission", json={"rate": 55},
                follow_redirects=False)
check("K5 anonymous commission write → refused (operator gate redirect)",
      r.status_code in (302, 401, 403), str(r.status_code))
_set_rate(30)

# ══════════ 2. withdrawal payout_failed ══════════
from economy.earnings import (request_withdrawal, approve_withdrawal,
                              mark_withdrawal_payout_failed,
                              earnings_summary)
from repo import ledger as ledger_repo

def _seed_earn(uid, amount):
    with db() as con:
        ledger_repo.lock_user(con, uid)
        base = ledger_repo.earn_pool_balance(con, uid)
        ledger_repo.insert_row(con, uid, amount, base + amount,
                               "provider_earn", "test", f"seed-{uid}",
                               f"b2-seed-earn:{uid}:{amount}",
                               "b2 seed earn", ts)

prov = _mkuser(_u + "prov", payout_channel="stripe:acct_b2")
_seed_earn(prov, 50000)          # $50.00
wd = request_withdrawal(prov, 30000, note="b2")
wid = int(wd["id"])
approve_withdrawal(wid, admin_user_id=op)
pool_after_approve = None
with db() as con:
    pool_after_approve = ledger_repo.earn_pool_balance(con, prov)
sums_after_approve = earnings_summary(prov)

row = mark_withdrawal_payout_failed(wid, "bank rejected the transfer",
                                    admin_user_id=op)
with db() as con:
    r2 = con.execute("select status from withdrawals where id=%s",
                     (wid,)).fetchone()
    refund_rows = con.execute(
        "select id, delta, balance_after from ledger where user_id=%s and"
        " idem_key=%s", (prov, f"payout-failed:{wid}")).fetchall()
    pool_after_fail = ledger_repo.earn_pool_balance(con, prov)
    aud = con.execute(
        "select after_json from operator_audit where"
        " action='withdrawal.payout_failed' and target_id=%s"
        " order by id desc limit 1", (str(wid),)).fetchone()
check("W1 approved → payout_failed, earn pool restored in-ledger",
      row["status"] == "payout_failed" and r2["status"] == "payout_failed"
      and len(refund_rows) == 1 and int(refund_rows[0]["delta"]) == 30000
      and int(refund_rows[0]["balance_after"]) == int(pool_after_approve) + 30000
      and int(pool_after_fail) == int(pool_after_approve) + 30000,
      f"{dict(row)} pool {pool_after_approve}→{pool_after_fail}")
check("W2 payout_failed audit carries the required reason",
      aud and aud["after_json"].get("reason")
      == "bank rejected the transfer", str(aud))
check("W3 summary: returned amount available again, not 'withdrawn'",
      earnings_summary(prov)["available"]
      == sums_after_approve["available"] + 30000
      and earnings_summary(prov)["withdrawn"] == 0,
      str(earnings_summary(prov)))
row_replay = mark_withdrawal_payout_failed(wid, "second attempt",
                                           admin_user_id=op)
with db() as con:
    n_refund = con.execute(
        "select count(*) c from ledger where user_id=%s and idem_key=%s",
        (prov, f"payout-failed:{wid}")).fetchone()["c"]
check("W4 replay → no-op success, never double refund",
      row_replay["status"] == "payout_failed" and n_refund == 1, str(n_refund))
for bad in ((wid, "", op), (wid, "   ", op), (wid, 123, op)):
    try:
        mark_withdrawal_payout_failed(*bad)
        check("W5 missing/blank reason refused", False, str(bad))
        break
    except ValueError:
        pass
else:
    check("W5 missing/blank/non-string reason refused (typed)", True)
try:
    mark_withdrawal_payout_failed(wid + 100000, "nope", admin_user_id=op)
    check("W6 unknown withdrawal → not_found", False)
except KeyError:
    check("W6 unknown withdrawal → not_found", True)
# paid after payout_failed is forbidden
try:
    from economy.earnings import mark_withdrawal_paid
    mark_withdrawal_paid(wid, "ref-b2", admin_user_id=op)
    check("W7 payout_failed → paid forbidden", False)
except ValueError as exc:
    check("W7 payout_failed → paid forbidden (transition_conflict)",
          str(exc) == "transition_conflict", str(exc))
# CHECK constraint: payout_failed rows cannot carry paid evidence
try:
    with db() as con:
        con.execute(
            "insert into withdrawals (user_id, amount, status,"
            " payout_channel_snapshot, created_at, reviewed_at, reviewed_by,"
            " paid_at, paid_by, payout_ref) values (%s, 1000, 'payout_failed',"
            " 'x', %s, %s, %s, %s, %s, 'r')",
            (prov, ts, ts, op, ts, op))
    check("W8 DB CHECK: payout_failed + paid evidence rejected", False)
except Exception as exc:
    check("W8 DB CHECK: payout_failed + paid evidence rejected",
          "withdrawals_payout_failed_ck" in str(exc)
          or "check" in str(exc).lower(), str(exc)[:80])

# ══════════ 3. partial refund → operator review queue (200) ══════════
from economy import reconcile as _rec
from economy import creem as _creem
_TXNS = {}
def _fake_fetch(txn_id):
    t = _TXNS.get(txn_id)
    return dict(t) if t else None
_rec.fetch_transaction = _fake_fetch
import hmac as _hmac, hashlib as _hashlib
_creem.CREEM_WEBHOOK_SECRET = "b2-test-secret"
def _signed(event):
    raw = _json.dumps(event).encode()
    return raw, _hmac.new(b"b2-test-secret", raw, _hashlib.sha256).hexdigest()

def _fire(event_type, txn_id, event_id=None):
    obj = {"id": ("ref_" if event_type == "refund.created" else "disp_") + txn_id,
           "object": "refund" if event_type == "refund.created" else "dispute",
           "transaction": {"id": txn_id}}
    event = {"id": event_id or f"evt_{txn_id}", "eventType": event_type,
             "object": obj}
    raw, sig = _signed(event)
    resp = client.post("/webhooks/creem", content=raw,
                       headers={"creem-signature": sig,
                                "Content-Type": "application/json"})
    return resp.status_code, resp.json()

buyer = _mkuser(_u + "buyer")
b_pre = spendable_balance(buyer)
_TXNS[f"txn_{_u}part"] = {"object": "transaction", "id": f"txn_{_u}part",
                          "status": "partially_refunded", "type": "payment"}
sc, body = _fire("refund.created", f"txn_{_u}part")
with db() as con:
    rv = con.execute(
        "select * from payment_reviews where txn_id=%s order by id desc"
        " limit 1", (f"txn_{_u}part",)).fetchone()
check("P1 partial refund → 200 + review row + ZERO mutation",
      sc == 200 and body.get("kind") == "review" and rv is not None
      and rv["status"] == "open"
      and spendable_balance(buyer) == b_pre,
      f"{sc} {body} bal={spendable_balance(buyer)}")
sc2, body2 = _fire("refund.created", f"txn_{_u}part")
with db() as con:
    n_rv = con.execute(
        "select count(*) c from payment_reviews where txn_id=%s",
        (f"txn_{_u}part",)).fetchone()["c"]
check("P2 same-event replay → still 200, still ONE row",
      sc2 == 200 and n_rv == 1, f"{sc2} n={n_rv}")
with db() as con:
    rid = int(rv["id"])
r = client.post(f"/samelabs/economy/reviews/{rid}/resolve", json={},
                headers=OP_H)
with db() as con:
    rv2 = con.execute("select status, resolved_by from payment_reviews"
                      " where id=%s", (rid,)).fetchone()
    aud = con.execute(
        "select id from operator_audit where action='payment_review.resolve'"
        " and target_id=%s", (str(rid),)).fetchone()
check("P3 operator resolve → resolved + audit, balance still untouched",
      r.status_code == 200 and rv2["status"] == "resolved"
      and aud is not None and spendable_balance(buyer) == b_pre,
      f"{r.status_code} {r.text}")
r = client.post(f"/samelabs/economy/reviews/{rid}/resolve", json={},
                headers=OP_H)
with db() as con:
    n_aud = con.execute(
        "select count(*) c from operator_audit where"
        " action='payment_review.resolve' and target_id=%s",
        (str(rid),)).fetchone()["c"]
check("P4 resolve replay → no-op, no duplicate audit",
      r.status_code == 200 and n_aud == 1, str(n_aud))

# ══════════ 4. upgrade refund boundary (period payment record) ══════════
from economy.subscription import handle_event as _hev

def _iso(e):
    import datetime
    return datetime.datetime.fromtimestamp(
        e, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _sub_snap(status, ps, pe, product, sub_id, txn=None):
    s = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": _iso(ps),
         "current_period_end_date": _iso(pe),
         "product": {"id": product}}
    if txn:
        s["last_transaction_id"] = txn
    return s

def _mkplan(code, prod, price, monthly):
    with db() as con:
        so = int(con.execute(
            "select coalesce(max(sort_order), 0) + 1 s from plans"
        ).fetchone()["s"])
        row = con.execute(
            "insert into plans (code, name, monthly_credits, period_days,"
            " price_cents, currency, creem_product_id, sort_order, active,"
            " created_at, updated_at)"
            " values (%s, %s, %s, 30, %s, 'USD', %s, %s, TRUE, %s, %s)"
            " returning id",
            (f"{code}_{_u}", code.upper(), monthly, price, prod, so,
             ts, ts)).fetchone()
    return int(row["id"])

PS = ts - 5 * 86400
PE = ts + 25 * 86400

def _seed_sub(name, plan_id, product, sub_id):
    uid = _mkuser(name)
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid', 999,"
            " 'USD', %s, %s)", (uid, plan_id, "sub:" + sub_id, ts, ts))
        con.execute(
            "insert into memberships (user_id, plan_id,"
            " provider_subscription_id, status, started_at, renews_at,"
            " last_grant_period_start, updated_at)"
            " values (%s, %s, %s, 'active', %s, %s, %s, %s)",
            (uid, plan_id, "sub:" + sub_id, PS, PE, PS, ts))
    return uid

def _membership(uid):
    with db() as con:
        m = con.execute("select * from memberships where user_id=%s",
                        (uid,)).fetchone()
    return dict(m) if m else None

zero_id = _mkplan("b2zero", f"prod_b2_zero_{_u}", 999, 0)
big_id = _mkplan("b2big", f"prod_b2_big_{_u}", 2999, 400)
from governance import plan_catalog as _pc
_pc.catalog(refresh=True)   # the test process caches tiers at first use

# zero-credit base paid → NO grant, but the base payment IS recorded
uz = _seed_sub(_u + "z1", zero_id, f"prod_b2_zero_{_u}", f"sub_{_u}z1")
bz = spendable_balance(uz)
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_zero_{_u}",
                                    f"sub_{_u}z1", txn=f"txn_{_u}z1a"))
with db() as con:
    rec = con.execute(
        "select base_txn_id from subscription_period_payments where"
        " sub_id=%s and period_start=%s", (f"sub_{_u}z1", PS)).fetchone()
    n_grants = con.execute(
        "select count(*) c from ledger where user_id=%s and kind in"
        " ('grant_plan','grant_plan_upgrade')", (uz,)).fetchone()["c"]
check("U1 zero-credit base paid → zero grant, base payment recorded",
      spendable_balance(uz) == bz and rec is not None
      and rec["base_txn_id"] == f"txn_{_u}z1a" and n_grants == 0,
      f"{dict(rec) if rec else None} grants={n_grants}")
# upgrade (update sync → upgrade charge same period) → top-up +400
_hev("subscription.update", _sub_snap("active", PS, PE,
                                      f"prod_b2_big_{_u}", f"sub_{_u}z1"))
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_big_{_u}",
                                    f"sub_{_u}z1", txn=f"txn_{_u}z1b"))
b_up = spendable_balance(uz)
with db() as con:
    rec2 = con.execute(
        "select base_txn_id from subscription_period_payments where"
        " sub_id=%s and period_start=%s", (f"sub_{_u}z1", PS)).fetchone()
check("U2 upgrade charge mints +400, base record NOT overwritten",
      b_up - bz == 400 and rec2["base_txn_id"] == f"txn_{_u}z1a",
      f"{bz}→{b_up} base={rec2['base_txn_id']}")
# refund the UPGRADE fee → revoke 400, membership STILL valid
_TXNS[f"txn_{_u}z1b"] = {"object": "transaction", "id": f"txn_{_u}z1b",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u}z1",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u}z1b")
m = _membership(uz)
check("U3 upgrade refund → exact -400, membership stays active",
      sc == 200 and spendable_balance(uz) == b_up - 400
      and m["status"] == "active" and int(m["renews_at"]) == PE,
      f"{sc} {body} bal={spendable_balance(uz)} m={m['status']}")
# refund the BASE payment of the zero-credit period → membership ends
_TXNS[f"txn_{_u}z1a"] = {"object": "transaction", "id": f"txn_{_u}z1a",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u}z1",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u}z1a")
m = _membership(uz)
check("U4 zero-credit base refund → membership expires now, zero revoke",
      sc == 200 and spendable_balance(uz) == b_up - 400
      and m["status"] == "expired",
      f"{sc} {body} m={m['status']}")

# zero → zero upgrade: refund of the upgrade fee leaves membership valid,
# zero credits reversed
z2_id = _mkplan("b2zero2", f"prod_b2_zero2_{_u}", 1999, 0)
_pc.catalog(refresh=True)
u2 = _seed_sub(_u + "z2", zero_id, f"prod_b2_zero_{_u}", f"sub_{_u}z2")
b2 = spendable_balance(u2)
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_zero_{_u}",
                                    f"sub_{_u}z2", txn=f"txn_{_u}z2a"))
_hev("subscription.update", _sub_snap("active", PS, PE,
                                      f"prod_b2_zero2_{_u}", f"sub_{_u}z2"))
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_zero2_{_u}",
                                    f"sub_{_u}z2", txn=f"txn_{_u}z2b"))
_TXNS[f"txn_{_u}z2b"] = {"object": "transaction", "id": f"txn_{_u}z2b",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u}z2",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u}z2b")
m = _membership(u2)
check("U5 zero→zero upgrade refund → membership active, 0 credits moved",
      sc == 200 and spendable_balance(u2) == b2 and m["status"] == "active",
      f"{sc} {body} m={m['status']} bal={spendable_balance(u2)}")

# upgrade callback LOST (no paid event, no grant) → refund still cannot
# end the base-paid period
u3 = _seed_sub(_u + "z3", zero_id, f"prod_b2_zero_{_u}", f"sub_{_u}z3")
b3 = spendable_balance(u3)
_hev("subscription.paid", _sub_snap("active", PS, PE,
                                    f"prod_b2_zero_{_u}",
                                    f"sub_{_u}z3", txn=f"txn_{_u}z3a"))
_TXNS[f"txn_{_u}z3lost"] = {"object": "transaction",
                            "id": f"txn_{_u}z3lost", "status": "refunded",
                            "type": "invoice", "subscription": f"sub_{_u}z3",
                            "period_start": _iso(PS),
                            "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u}z3lost")
m = _membership(u3)
check("U6 lost upgrade callback + refund → no expire, no revoke",
      sc == 200 and spendable_balance(u3) == b3 and m["status"] == "active",
      f"{sc} {body} m={m['status']}")
# never-applied period → unprovable, fail loud 500, zero mutation
_TXNS[f"txn_{_u}never"] = {"object": "transaction", "id": f"txn_{_u}never",
                           "status": "refunded", "type": "invoice",
                           "subscription": f"sub_{_u}z3",
                           "period_start": _iso(PS + 40 * 86400),
                           "period_end": _iso(PE + 40 * 86400)}
b3 = spendable_balance(u3)
sc, body = _fire("refund.created", f"txn_{_u}never")
m = _membership(u3)
check("U7 no period record → 500 fail loud, zero mutation",
      sc == 500 and body.get("error") == "subscription_base_payment_unproven"
      and spendable_balance(u3) == b3 and m["status"] == "active",
      f"{sc} {body}")

# ══════════ 5. outputs runtime enforcement ══════════
from execution import orchestrator as _orch

def _fake_process(return_items):
    def _pt(tool_id, sources, params, body=None):
        return {"items": return_items}
    return _pt

DELIVERY = {"expiry_days": 7, "allowed_downloads": 5}
FILE_ITEM = {"kind": "file", "filename": "o.png",
             "content_type": "image/png", "data": b"\x89PNG fake"}
TEXT_ITEM = {"kind": "text", "text": "hello"}

def _drive(tool, user_id, return_items, cost=100, owner_id=owner):
    """Full gate chain with a local orchestrate that runs the REAL
    execute_local (worker = fake handler returning `return_items`)."""
    async def orchestrate():
        return await _orch.execute_local(
            {"ip": "127.0.0.1"}, tool, [], {},
            {"id": user_id},
            _fake_process(return_items), None, DELIVERY)
    _ref_seq[0] += 1
    inv = ToolInvocation(tool_id=tool["id"], files=[], params={},
                         delivery=DELIVERY,
                         client_ref=f"b2out:{_ref_seq[0]}:{_u}")
    return asyncio.run(
        gate.run({"id": user_id}, tool, inv, orchestrate))

# storage object count before (fake gokapi is the physical authority)
from gokapi import list_gokapi_files as _list_gokapi
_ok, _objs_before = _list_gokapi()

uto = _mkuser(_u + "out")
ttx = f"b2out_{_u}"
_mktool(ttx, owner_id=owner, cost=100, outputs_json='["text"]')
_grant(uto, 10000, "manual_topup", note="b2")
b_out_pre = spendable_balance(uto)
tool_row = {"id": ttx, "tool_key": ttx,
            "owner_id": owner, "outputs_json": '["text"]'}
res, err, code = _drive(tool_row, uto, [TEXT_ITEM, FILE_ITEM])
with db() as con:
    crow = con.execute(
        "select id, status, error, result_public_id from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (uto, ttx)).fetchone()
    n_refund = con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'"
        " and note like %s", (uto, f"%{ttx}%")).fetchone()["c"]
_ok, _objs_after = _list_gokapi()
check("O1 text-only tool returned a file → call failed typed, refund exact",
      res is None and err == {"ok": False, "error": "err_output_mismatch"}
      and code == 400 and crow["status"] == "failed"
      and crow["error"] == "err_output_mismatch"
      and spendable_balance(uto) == b_out_pre and n_refund >= 1,
      f"{err} {code} row={dict(crow)} bal={spendable_balance(uto)}")
check("O2 zero result items landed, zero staged objects",
      crow["result_public_id"] is None
      and len(_objs_after) == len(_objs_before),
      f"gokapi {len(_objs_before)}→{len(_objs_after)}")

# declared text+file → the same output set succeeds
ttx2 = f"b2out2_{_u}"
_mktool(ttx2, owner_id=owner, cost=100, outputs_json='["text","file"]')
_grant(uto, 10000, "manual_topup", note="b2")
tool_row2 = {"id": ttx2, "tool_key": ttx2,
             "owner_id": owner, "outputs_json": '["text","file"]'}
res2, err2, code2 = _drive(tool_row2, uto, [TEXT_ITEM, FILE_ITEM])
check("O3 declared text+file → same output set succeeds",
      res2 is not None and err2 is None
      and [i["kind"] for i in res2["result"]["items"]] == ["text", "file"],
      f"{err2} {code2}")

# remote path: drive_http returning an undeclared kind fails BEFORE staging
import remote as _remote_mod
_orig_drive = _remote_mod.drive_http
_remote_mod.drive_http = lambda decl, sources, params, headers, trusted, body=None, canonical_body=None, body_format=None: {
    "items": [{"kind": "json", "data": {"x": 1}}]}
trt = f"b2rt_{_u}"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id,"
        " execution, label_json, visibility, enabled,"
        " outputs_json, endpoint_json, created_at, updated_at, accept_json, format_family)"
        " values (%s, %s, %s, 'http', %s, 'public',"
        " TRUE, '[\"text\"]', %s, %s, %s, '[\".png\"]', 'image')",
        (trt, owner, trt, _json.dumps({"en": "B2R"}),
         _json.dumps({"url": "http://127.0.0.1:9/none"}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 100, TRUE, %s)", (trt, ts))
tool_rt = {"id": trt, "tool_key": trt,
           "owner_id": owner, "outputs_json": '["text"]',
           "endpoint_json": _json.dumps({"url": "http://127.0.0.1:9/none"})}
async def _drive_remote():
    return await _orch.execute_remote(
        {"ip": "127.0.0.1"}, tool_rt, [], {}, {"id": uto}, DELIVERY)

try:
    got = asyncio.run(_drive_remote())
except Exception as exc:
    got = exc
_remote_mod.drive_http = _orig_drive
check("O4 remote path: undeclared json item → typed failure before staging",
      isinstance(got, Exception) and str(got) == "err_output_mismatch",
      str(got)[:100])

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
_sys.exit(1 if FAIL else 0)
