#!/usr/bin/env python3
"""Payment reverse canonical — refund/dispute → exact economic reversal.

Proves over real functions/routes (Creem isolated by module fakes, DB
isolated):
  package: +10000 → spend 7000 → full refund → -7000 (negative legal,
           exact original grant, snapshot-checked, catalog edits
           irrelevant);
  subscription: reversal reads ONLY transaction-linked grant rows
           (never current Plan monthly_credits); current-period refund/
           dispute expires Membership NOW; historical reversal leaves
           the current period untouched;
  idempotency: payment-reversal:{txn} survives refund replay, dispute
           replay, refund-then-dispute, and concurrent double fire;
  fail-closed: partial refund / unknown txn / provider fetch failure →
           zero mutation + 500; subscription.update alone grants zero.
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

from database import db

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

_u = "rv" + _os.urandom(4).hex()

from economy.billing import spendable_balance, grant, consume, revoke
from repo import orders as orders_repo

# ═══ fake provider transaction store (fetch_transaction is patched) ═══
from economy import reconcile as _rec
from economy import creem as _creem

_TXNS = {}
def _fake_fetch_transaction(txn_id):
    _FETCHED.append(txn_id)
    t = _TXNS.get(txn_id)
    return dict(t) if t else None
_rec.fetch_transaction = _fake_fetch_transaction
_FETCH_FAIL = {"fail": False}
_orig_fetch_transaction = _rec.fetch_transaction
def _fetch_maybe_failing(txn_id):
    if _FETCH_FAIL["fail"]:
        return None
    return _orig_fetch_transaction(txn_id)
_rec.fetch_transaction = _fetch_maybe_failing

# webhook signature: set a test secret + compute HMAC like Creem
import hmac as _hmac
import hashlib as _hashlib
import json as _json
_creem.CREEM_WEBHOOK_SECRET = "rv-test-secret"
def _signed(event):
    raw = _json.dumps(event).encode()
    sig = _hmac.new(b"rv-test-secret", raw, _hashlib.sha256).hexdigest()
    return raw, sig

from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)

_FETCHED = []   # txn ids the provider fetch actually received

def _fire(event_type, txn_id, *, obj_id=None, obj_type=None,
          txn_nested=True, txn_id_override=None):
    """Fire a signed webhook with the REAL Creem shape:
    event.id=evt_*; object.id=ref_*/disp_* (NOT the transaction id);
    object.object=refund|dispute; object.transaction.id=tran_*.
    Returns (status, json)."""
    if obj_id is None:
        obj_id = ("ref_" if event_type == "refund.created" else "disp_") + txn_id
    obj = {"id": obj_id,
           "object": obj_type or ("refund" if event_type == "refund.created"
                                  else "dispute")}
    if txn_nested:
        obj["transaction"] = {"id": txn_id_override or txn_id}
    event = {"id": f"evt_{txn_id}", "eventType": event_type, "object": obj}
    raw, sig = _signed(event)
    r = client.post("/webhooks/creem", content=raw,
                    headers={"creem-signature": sig,
                             "Content-Type": "application/json"})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}

def _iso(v):
    import datetime
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).isoformat()

# ══════════ A. PACKAGE reversal ══════════
buyer = _mkuser(_u + "b1")
# a credits package
with db() as con:
    _rv_pkg_sort = int(con.execute(
        "select coalesce(max(sort_order), 0) + 1 s from packages"
    ).fetchone()["s"])
    pkg = con.execute(
        "insert into packages (code, name, credits, price_cents, currency,"
        " active, sort_order, created_at, updated_at) values (%s, 'RV Pkg',"
        " 10000, 999, 'USD', TRUE, %s, %s, %s) returning id",
        (f"pkg_{_u}", _rv_pkg_sort, ts, ts)).fetchone()
    pkg_id = int(pkg["id"])

# local order (as checkout would create) with frozen snapshot 10000
with db() as con:
    o = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, credits_snapshot, created_at)"
        " values (%s, 'package', %s, 'creem', %s, 'pending', 999, 'USD', 10000, %s)"
        " returning *", (buyer, pkg_id, f"ch:rv_{_u}", ts)).fetchone()
    order_id = int(o["id"])

# fulfill it via the canonical fulfillment (real Order.id, spec#2)
_creem.CREEM_CREDITS_PRODUCT_ID = "prod_credits_rv"
_creem.CREEM_CREDITS_CURRENCY = "USD"
res = _creem.fulfill_by_request_id(
    order_id, provider_order_id=f"ord_{_u}", checkout_id=f"rv_{_u}",
    amount_cents=999, product_id="prod_credits_rv", currency="USD")
check("A0 fulfill grants 10000 (snapshot) + provider id 收敛 ord_xxx",
      res.get("granted") and int(res.get("credits")) == 10000,
      str(res))
with db() as con:
    row = con.execute("select provider_order_id, status from orders where id=%s",
                      (order_id,)).fetchone()
check("A0a local order now bound to canonical ord_xxx",
      row["provider_order_id"] == f"ord_{_u}" and row["status"] == "paid",
      str(dict(row)))
check("A0b spendable = 10000", spendable_balance(buyer) == 10000,
      str(spendable_balance(buyer)))

# user spends 7000
consume(buyer, 7000, "consume", ref_type="test", ref_id="spend1",
        idem_key=f"spend:{_u}:1", note="rv test spend")
check("A1 spent 7000 → balance 3000", spendable_balance(buyer) == 3000,
      str(spendable_balance(buyer)))

# operator edits the package to 5000 — reversal must NOT care
with db() as con:
    con.execute("update packages set credits=5000 where id=%s", (pkg_id,))

# full refund
_TXNS[f"txn_{_u}r1"] = {
    "object": "transaction", "id": f"txn_{_u}r1", "status": "refunded",
    "type": "payment", "order": f"ord_{_u}",
}
sc, body = _fire("refund.created", f"txn_{_u}r1")
check("A2 full refund → 200 + exact reversal fact",
      sc == 200 and body.get("reversed") is True and body.get("credits") == -10000,
      f"{sc} {body}")
check("A3 balance -7000 (debt legal, no insufficient check)",
      spendable_balance(buyer) == -7000, str(spendable_balance(buyer)))
with db() as con:
    orow = con.execute("select status, refunded_at from orders where id=%s",
                       (order_id,)).fetchone()
check("A4 order terminal state = refunded", orow["status"] == "refunded"
      and orow["refunded_at"] is not None, str(dict(orow)))

# replay same refund → no double revoke
sc, body = _fire("refund.created", f"txn_{_u}r1")
check("A5 refund replay → idempotent no double revoke (balance still -7000)",
      sc == 200 and spendable_balance(buyer) == -7000,
      f"{sc} bal={spendable_balance(buyer)}")

# same transaction then disputed → still no second reversal
_TXNS[f"txn_{_u}r1"] = {**_TXNS[f"txn_{_u}r1"], "status": "chargeback"}
sc, body = _fire("dispute.created", f"txn_{_u}r1")
check("A6 refund→dispute same txn → single reversal only",
      sc == 200 and spendable_balance(buyer) == -7000,
      f"{sc} bal={spendable_balance(buyer)}")

# ══════════ B. dispute full chargeback exact reversal ══════════
buyer2 = _mkuser(_u + "b2")
with db() as con:
    o2 = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, credits_snapshot, created_at)"
        " values (%s, 'package', %s, 'creem', %s, 'pending', 999, 'USD', 10000, %s)"
        " returning *", (buyer2, pkg_id, f"ch:rv2_{_u}", ts)).fetchone()
    order2_id = int(o2["id"])
_creem.fulfill_by_request_id(
    order2_id, provider_order_id=f"ord2_{_u}", checkout_id=f"rv2_{_u}",
    amount_cents=999, product_id="prod_credits_rv", currency="USD")
_TXNS[f"txn_{_u}d1"] = {
    "object": "transaction", "id": f"txn_{_u}d1", "status": "chargeback",
    "type": "payment", "order": f"ord2_{_u}",
}
b0 = spendable_balance(buyer2)
sc, body = _fire("dispute.created", f"txn_{_u}d1")
check("B1 dispute full chargeback → exact -10000",
      sc == 200 and spendable_balance(buyer2) == b0 - 10000,
      f"{sc} bal={spendable_balance(buyer2)}")
with db() as con:
    orow = con.execute("select status from orders where id=%s",
                       (order2_id,)).fetchone()
check("B2 order terminal = disputed", orow["status"] == "disputed",
      str(dict(orow)))

# ══════════ C. partial refund → zero mutation + 500 ══════════
buyer3 = _mkuser(_u + "b3")
with db() as con:
    o3 = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, credits_snapshot, created_at)"
        " values (%s, 'package', %s, 'creem', %s, 'pending', 999, 'USD', 10000, %s)"
        " returning *", (buyer3, pkg_id, f"ch:rv3_{_u}", ts)).fetchone()
    order3_id = int(o3["id"])
_creem.fulfill_by_request_id(
    order3_id, provider_order_id=f"ord3_{_u}", checkout_id=f"rv3_{_u}",
    amount_cents=999, product_id="prod_credits_rv", currency="USD")
b3_0 = spendable_balance(buyer3)
_TXNS[f"txn_{_u}p1"] = {
    "object": "transaction", "id": f"txn_{_u}p1", "status": "partially_refunded",
    "type": "payment", "order": f"ord3_{_u}",
}
sc, body = _fire("refund.created", f"txn_{_u}p1")
check("C1 partial refund → 500 partial_refund_unresolved + zero mutation",
      sc == 500 and body.get("error") == "partial_refund_unresolved"
      and spendable_balance(buyer3) == b3_0,
      f"{sc} {body} bal={spendable_balance(buyer3)}")
with db() as con:
    orow = con.execute("select status from orders where id=%s",
                       (order3_id,)).fetchone()
check("C2 order untouched (still paid)", orow["status"] == "paid",
      str(dict(orow)))

# ══════════ D. unknown txn / fetch failure → 500 + zero mutation ══════════
sc, body = _fire("refund.created", "txn_unknown_rv")
check("D1 unknown transaction → 500 zero mutation",
      sc == 500 and spendable_balance(buyer3) == b3_0,
      f"{sc} {body}")
_FETCH_FAIL["fail"] = True
_TXNS[f"txn_{_u}f1"] = {
    "object": "transaction", "id": f"txn_{_u}f1", "status": "refunded",
    "type": "payment", "order": f"ord3_{_u}",
}
sc, body = _fire("refund.created", f"txn_{_u}f1")
check("D2 provider fetch failure → 500 zero mutation",
      sc == 500 and spendable_balance(buyer3) == b3_0,
      f"{sc} {body}")
_FETCH_FAIL["fail"] = False

# grant/snapshot mismatch → fail loud (construct synthetic order)
buyer4 = _mkuser(_u + "b4")
with db() as con:
    o4 = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, credits_snapshot, created_at)"
        " values (%s, 'package', %s, 'creem', %s, 'pending', 999, 'USD', 10000, %s)"
        " returning *", (buyer4, pkg_id, f"ch:rv4_{_u}", ts)).fetchone()
    order4_id = int(o4["id"])
_creem.fulfill_by_request_id(
    order4_id, provider_order_id=f"ord4_{_u}", checkout_id=f"rv4_{_u}",
    amount_cents=999, product_id="prod_credits_rv", currency="USD")
# doctor the grant ledger delta to disagree with the snapshot
with db() as con:
    con.execute(
        "update ledger set delta = 5000 where user_id=%s and idem_key=%s",
        (buyer4, f"order:{order4_id}"))
_TXNS[f"txn_{_u}m1"] = {
    "object": "transaction", "id": f"txn_{_u}m1", "status": "refunded",
    "type": "payment", "order": f"ord4_{_u}",
}
sc, body = _fire("refund.created", f"txn_{_u}m1")
check("D3 grant/snapshot mismatch → 500 fail loud, no guess",
      sc == 500 and "mismatch" in str(body.get("error")),
      f"{sc} {body}")

# ══════════ S. SUBSCRIPTION reversal ══════════
from economy.subscription import handle_event as _hev

def _sub_snap(status, ps, pe, product, sub_id, txn=None):
    s = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": _iso(ps),
         "current_period_end_date": _iso(pe),
         "product": {"id": product}}
    if txn:
        s["last_transaction_id"] = txn
    return s

# two paid plans (plus/pro) with distinct products
from app.web_auth import create_browser_session, COOKIE_NAME
op = _mkuser(_u + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess = create_browser_session(op)
def _mkplan(code, prod, price, monthly, so=None):
    if so is None:
        with db() as _c:
            so = int(_c.execute(
                "select coalesce(max(sort_order), 0) + 1 s from plans"
            ).fetchone()["s"])
    _creem.CREEM_API_KEY = "test-key"
    r = client.post("/samelabs/commerce/products/plans", json={
        "code": f"{code}_{_u}", "name": code.upper(), "monthly_credits": monthly,
        "price_cents": price, "creem_product_id": prod, "sort_order": so,
        "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
        "expiry_days": [7], "download_limits": [50],
    }, cookies={COOKIE_NAME: op_sess})
    assert r.status_code == 200, r.text
    return int(r.json()["plan"]["id"])
# mirror state for plan creation write-through
_ms = getattr(__builtins__, "_MIRROR", None)
import economy.creem as _cm
# reuse forward-style mirror fakes minimally: patch transport
_MIRROR = {"prod_rv_plus": {"price": 999, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"},
           "prod_rv_pro": {"price": 2999, "currency": "USD",
                           "billing_type": "recurring",
                           "billing_period": "every-month"}}
def _fp(pid):
    d = _MIRROR.get(pid)
    return dict(d) if d else None
def _pt(path, payload):
    pid = path.rsplit("/", 1)[-1]
    d = _MIRROR[pid]
    _MIRROR[pid] = {**d, "price": payload["price"],
                    "currency": payload["currency"]}
    return {"object": "product", "id": pid, "price": payload["price"],
            "currency": payload["currency"], "billing_type": "recurring",
            "billing_period": "every-month"}
_cm._fetch_product = _fp
_cm._patch = _pt
_cm.CREEM_API_KEY = "test-key"

plus_id = _mkplan("rvplus", "prod_rv_plus", 999, 100)
pro_id = _mkplan("rvpro", "prod_rv_pro", 2999, 500)

PS = ts - 5 * 86400
PE = ts + 25 * 86400

def _seed_sub_user(name, plan_id, product, sub_id):
    uid = _mkuser(name)
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid', 999,"
            " 'USD', %s, %s)",
            (uid, plan_id, "sub:" + sub_id, ts, ts))
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id,"
            " status, started_at, renews_at, last_grant_period_start, updated_at)"
            " values (%s, %s, %s, 'active', %s, %s, %s, %s)",
            (uid, plan_id, "sub:" + sub_id, PS, PE, PS, ts))
    return uid

# S1/S2 sequence: base paid FIRST, then update sync, then upgrade paid.
u1 = _seed_sub_user(_u + "s1", plus_id, "prod_rv_plus", f"sub_{_u}s1")
b1 = spendable_balance(u1)
# S1: update alone on a FRESH member (no prior paid) must grant nothing
_hev("subscription.update", _sub_snap("active", PS, PE, "prod_rv_plus",
                                      f"sub_{_u}s1"))
check("S1 subscription.update 单独到达 → zero Credits (paid-proof only)",
      spendable_balance(u1) == b1,
      f"bal={spendable_balance(u1)} pre={b1}")
# S2a: base paid (plus) → +100
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u}s1", txn=f"txn_{_u}s1a"))
b1_after_base = spendable_balance(u1)
check("S2a base paid grant = 100 (plus)",
      b1_after_base - b1 == 100, f"{b1}→{b1_after_base}")
with db() as con:
    g = con.execute(
        "select kind, ref_type, ref_id from ledger where user_id=%s"
        " and kind in ('grant_plan','grant_plan_upgrade')"
        " and ref_type='creem_transaction'", (u1,)).fetchall()
check("S2b base grant ref = creem_transaction / txn id",
      len(g) == 1 and g[0]["kind"] == "grant_plan"
      and g[0]["ref_id"] == f"txn_{_u}s1a", str([dict(x) for x in g]))
# S2c: upgrade — update syncs Membership to pro (ZERO credits), the
# upgrade paid transaction produces the +400 top-up in apply_paid_period
b_pre_sync = spendable_balance(u1)
_hev("subscription.update", _sub_snap("active", PS, PE, "prod_rv_pro",
                                      f"sub_{_u}s1"))
check("S2c0 update sync alone still grants zero (already base-paid)",
      spendable_balance(u1) == b_pre_sync,
      f"{b_pre_sync}→{spendable_balance(u1)}")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_pro",
                                    f"sub_{_u}s1", txn=f"txn_{_u}s1b"))
check("S2c paid upgrade → apply_paid_period 补差 +400 (100→500)",
      spendable_balance(u1) - b1_after_base == 400,
      f"{b1_after_base}→{spendable_balance(u1)}")
with db() as con:
    g2 = con.execute(
        "select kind, ref_id from ledger where user_id=%s"
        " and kind='grant_plan_upgrade'", (u1,)).fetchall()
check("S2d upgrade grant ref = txn id",
      len(g2) == 1 and g2[0]["ref_id"] == f"txn_{_u}s1b",
      str([dict(x) for x in g2]))

# S3: full refund of BASE txn → exact base reversal (-100)
_TXNS[f"txn_{_u}s1a"] = {"object": "transaction", "id": f"txn_{_u}s1a",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u}s1",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
b_pre = spendable_balance(u1)
sc, body = _fire("refund.created", f"txn_{_u}s1a")
check("S3 base refund → exact -100 reversal",
      sc == 200 and spendable_balance(u1) == b_pre - 100,
      f"{sc} {body} bal={spendable_balance(u1)}")

# S3b: spend 300 → refunds will pull below zero (real debt)
consume(u1, 300, "consume", ref_type="test", ref_id="spends1",
        idem_key=f"spend:{_u}:s1", note="rv sub spend")

# S4: full refund of UPGRADE txn → exact -400
_TXNS[f"txn_{_u}s1b"] = {"object": "transaction", "id": f"txn_{_u}s1b",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u}s1",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
b_pre = spendable_balance(u1)
sc, body = _fire("refund.created", f"txn_{_u}s1b")
check("S4 upgrade refund → exact -400 reversal",
      sc == 200 and spendable_balance(u1) == b_pre - 400,
      f"{sc} {body} bal={spendable_balance(u1)}")
check("S4b reversal pulled spendable negative (debt): 500-300-100-400 = -300",
      spendable_balance(u1) == -300, str(spendable_balance(u1)))

# S5: current-period refund → Membership immediately expired
u5 = _seed_sub_user(_u + "s5", plus_id, "prod_rv_plus", f"sub_{_u}s5")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u}s5", txn=f"txn_{_u}s5"))
_TXNS[f"txn_{_u}s5"] = {"object": "transaction", "id": f"txn_{_u}s5",
                        "status": "refunded", "type": "invoice",
                        "subscription": f"sub_{_u}s5",
                        "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u}s5")
with db() as con:
    m5 = con.execute(
        "select status, renews_at from memberships where user_id=%s",
        (u5,)).fetchone()
import time as _t2
_now5 = int(_t2.time())
check("S5 current-period refund → Membership expired immediately",
      sc == 200 and m5["status"] == "expired"
      and int(m5["renews_at"]) <= _now5 + 2,
      f"{sc} {dict(m5)} now={_now5}")

# S6: current-period dispute → immediately expired
u6 = _seed_sub_user(_u + "s6", plus_id, "prod_rv_plus", f"sub_{_u}s6")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u}s6", txn=f"txn_{_u}s6"))
_TXNS[f"txn_{_u}s6"] = {"object": "transaction", "id": f"txn_{_u}s6",
                        "status": "chargeback", "type": "invoice",
                        "subscription": f"sub_{_u}s6",
                        "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("dispute.created", f"txn_{_u}s6")
with db() as con:
    m6 = con.execute(
        "select status from memberships where user_id=%s", (u6,)).fetchone()
check("S6 current-period dispute → Membership expired",
      sc == 200 and m6["status"] == "expired", f"{sc} {dict(m6)}")

# S7: HISTORICAL period refund → current paid Membership untouched
u7 = _seed_sub_user(_u + "s7", plus_id, "prod_rv_plus", f"sub_{_u}s7")
OLD_PS, OLD_PE = ts - 40 * 86400, ts - 10 * 86400   # january
CUR_PS, CUR_PE = ts - 5 * 86400, ts + 25 * 86400    # february paid & current
_hev("subscription.paid", _sub_snap("active", OLD_PS, OLD_PE, "prod_rv_plus",
                                    f"sub_{_u}s7", txn=f"txn_{_u}s7old"))
_hev("subscription.paid", _sub_snap("active", CUR_PS, CUR_PE, "prod_rv_plus",
                                    f"sub_{_u}s7", txn=f"txn_{_u}s7cur"))
with db() as con:
    con.execute(
        "update memberships set last_grant_period_start=%s where user_id=%s",
        (CUR_PS, u7))
_TXNS[f"txn_{_u}s7old"] = {"object": "transaction", "id": f"txn_{_u}s7old",
                           "status": "refunded", "type": "invoice",
                           "subscription": f"sub_{_u}s7",
                           "period_start": _iso(OLD_PS), "period_end": _iso(OLD_PE)}
b_pre = spendable_balance(u7)
sc, body = _fire("refund.created", f"txn_{_u}s7old")
with db() as con:
    m7 = con.execute(
        "select status, renews_at, last_grant_period_start from memberships"
        " where user_id=%s", (u7,)).fetchone()
check("S7 historical refund → exact -100 but current Membership intact",
      sc == 200 and spendable_balance(u7) == b_pre - 100
      and m7["status"] == "active" and int(m7["renews_at"]) == CUR_PE
      and int(m7["last_grant_period_start"]) == CUR_PS,
      f"{sc} {body} {dict(m7)}")

# S8: wrong subscription / wrong type / unknown order → zero mutation + 500
u8 = _seed_sub_user(_u + "s8", plus_id, "prod_rv_plus", f"sub_{_u}s8")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u}s8", txn=f"txn_{_u}s8"))
b8 = spendable_balance(u8)
_TXNS[f"txn_{_u}s8w"] = {"object": "transaction", "id": f"txn_{_u}s8w",
                         "status": "refunded", "type": "invoice",
                         "subscription": "sub_somebody_else",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"txn_{_u}s8w")
check("S8a unknown subscription order → 500 zero mutation",
      sc == 500 and spendable_balance(u8) == b8, f"{sc} {body}")
_TXNS[f"txn_{_u}s8t"] = {"object": "transaction", "id": f"txn_{_u}s8t",
                         "status": "refunded", "type": "subscription",
                         "subscription": f"sub_{_u}s8"}
sc, body = _fire("refund.created", f"txn_{_u}s8t")
check("S8b wrong type → 500 zero mutation",
      sc == 500 and spendable_balance(u8) == b8, f"{sc} {body}")

# S9: period aggregation nets reversal (spec §9)
from repo import orders as _ord
with db() as con:
    net_cur = _ord.period_plan_credits(con, u7, CUR_PS)
    net_old = _ord.period_plan_credits(con, u7, OLD_PS)
check("S9 period aggregation: current period intact (100), reversed period = 0",
      net_cur == 100 and net_old == 0, f"cur={net_cur} old={net_old}")

# S10: concurrent double-fire same refund → single reversal
u10 = _seed_sub_user(_u + "s10", plus_id, "prod_rv_plus", f"sub_{_u}s10")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u}s10", txn=f"txn_{_u}s10"))
_TXNS[f"txn_{_u}s10"] = {"object": "transaction", "id": f"txn_{_u}s10",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u}s10",
                         "period_start": _iso(PS), "period_end": _iso(PE)}
b10 = spendable_balance(u10)
import threading
_res = {}
def _fire10():
    _res["r"] = _fire("refund.created", f"txn_{_u}s10")
t = threading.Thread(target=_fire10)
t.start()
sc2, _ = _fire("refund.created", f"txn_{_u}s10")
t.join(timeout=30)
rev_rows = None
with db() as con:
    rev_rows = con.execute(
        "select count(*) c from ledger where user_id=%s and kind='revoke'"
        " and idem_key=%s", (u10, f"payment-reversal:txn_{_u}s10")).fetchone()
check("S10 concurrent double fire → exactly one revoke row (-100 once)",
      spendable_balance(u10) == b10 - 100 and int(rev_rows["c"]) == 1,
      f"bal={spendable_balance(u10)} rows={dict(rev_rows)}")

# ══════════ R. identity shape / reversed-period gate / recovery ══════════

# R1/R2: official shapes — ref_x != tran_x, disp_x != tran_x; fetch MUST
# receive the NESTED transaction id, never the refund/dispute object id.
_FETCHED.clear()
sc, body = _fire("refund.created", f"tran_{_u}x1",
                 obj_id=f"ref_{_u}x1")
check("R1 official refund shape: fetch receives tran_x (not ref_x)",
      f"tran_{_u}x1" in _FETCHED and f"ref_{_u}x1" not in _FETCHED,
      str(_FETCHED[-3:]))
_FETCHED.clear()
_TXNS[f"tran_{_u}x2"] = {"object": "transaction", "id": f"tran_{_u}x2",
                         "status": "chargeback", "type": "payment",
                         "order": f"ord2_{_u}"}
sc, body = _fire("dispute.created", f"tran_{_u}x2", obj_id=f"disp_{_u}x2")
check("R2 official dispute shape: fetch receives tran_x (not disp_x)",
      _FETCHED and _FETCHED[-1] == f"tran_{_u}x2", str(_FETCHED[-2:]))

# R3: missing nested transaction → 500 / zero mutation
b_r3 = spendable_balance(buyer3)
sc, body = _fire("refund.created", "txn_never_fetched", txn_nested=False)
check("R3 missing nested transaction → 500 reversal_proof_missing + zero mutation",
      sc == 500 and body.get("error") == "reversal_proof_missing"
      and spendable_balance(buyer3) == b_r3, f"{sc} {body}")

# R4: wrong object type → 500
sc, body = _fire("refund.created", f"txn_{_u}s8",
                 obj_type="dispute")
check("R4 wrong object.object type → 500 object_type_mismatch",
      sc == 500 and "object_type_mismatch" in str(body.get("error")),
      f"{sc} {body}")

# R5-R8: reversed-period replay gate + new-period recovery
u_r = _seed_sub_user(_u + "rr", plus_id, "prod_rv_plus", f"sub_{_u}rr")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u}rr", txn=f"tran_{_u}rr1"))
b_base = spendable_balance(u_r)
_TXNS[f"tran_{_u}rr1"] = {"object": "transaction", "id": f"tran_{_u}rr1",
                          "status": "refunded", "type": "invoice",
                          "subscription": f"sub_{_u}rr",
                          "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", f"tran_{_u}rr1")
with db() as con:
    m_r = con.execute("select status, renews_at from memberships"
                      " where user_id=%s", (u_r,)).fetchone()
check("R5 current period refund → -100 + Membership expired",
      sc == 200 and spendable_balance(u_r) == b_base - 100
      and m_r["status"] == "expired",
      f"{sc} {body} {dict(m_r)}")

# R6: SAME period old paid replay → zero grant + remains expired
b_r6 = spendable_balance(u_r)
try:
    _hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                        f"sub_{_u}rr", txn=f"tran_{_u}rr1"))
    r6 = "no-raise"
except KeyError as e:
    r6 = str(e)
with db() as con:
    m_r6 = con.execute("select status from memberships where user_id=%s",
                       (u_r,)).fetchone()
check("R6 同 period 旧 paid replay → paid_period_reversed + zero grant + 仍 expired",
      "paid_period_reversed" in r6 and spendable_balance(u_r) == b_r6
      and m_r6["status"] == "expired",
      f"{r6} bal={spendable_balance(u_r)} {dict(m_r6)}")

# R7: SAME period ANOTHER old paid transaction replay → still gated
# (the period itself is reversed; a different old txn of the same
# period must not resurrect it either)
b_r7 = spendable_balance(u_r)
try:
    _hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                        f"sub_{_u}rr", txn=f"tran_{_u}rr_old2"))
    r7 = "no-raise"
except KeyError as e:
    r7 = str(e)
check("R7 同 period 另一旧 paid txn replay → 仍被 gate + zero grant",
      "paid_period_reversed" in r7 and spendable_balance(u_r) == b_r7,
      f"{r7} bal={spendable_balance(u_r)}")

# R8: NEW period, NEW matching paid transaction → normal recovery
PS2, PE2 = ts + 86400, ts + 31 * 86400
b_r8 = spendable_balance(u_r)
_hev("subscription.paid", _sub_snap("active", PS2, PE2, "prod_rv_plus",
                                    f"sub_{_u}rr", txn=f"tran_{_u}rr2"))
with db() as con:
    m_r8 = con.execute("select status, renews_at from memberships"
                       " where user_id=%s", (u_r,)).fetchone()
check("R8 新 period matching paid txn → 正常 +100 grant + Membership 重新 active",
      spendable_balance(u_r) == b_r8 + 100 and m_r8["status"] == "active"
      and int(m_r8["renews_at"]) == PE2,
      f"bal={spendable_balance(u_r)} {dict(m_r8)}")

# R9: missing / invalid period_end → zero reversal + 500
u_r9 = _seed_sub_user(_u + "r9", plus_id, "prod_rv_plus", f"sub_{_u}r9")
_hev("subscription.paid", _sub_snap("active", PS, PE, "prod_rv_plus",
                                    f"sub_{_u}r9", txn=f"tran_{_u}r9"))
b_r9 = spendable_balance(u_r9)
_TXNS[f"tran_{_u}r9"] = {"object": "transaction", "id": f"tran_{_u}r9",
                         "status": "refunded", "type": "invoice",
                         "subscription": f"sub_{_u}r9",
                         "period_start": _iso(PS), "period_end": None}
sc, body = _fire("refund.created", f"tran_{_u}r9")
_TXNS[f"tran_{_u}r9b"] = {"object": "transaction", "id": f"tran_{_u}r9b",
                          "status": "refunded", "type": "invoice",
                          "subscription": f"sub_{_u}r9",
                          "period_start": _iso(PE),
                          "period_end": _iso(PS)}   # end <= start
scb, bodyb = _fire("refund.created", f"tran_{_u}r9b")
check("R9 缺失/乱序 period_end → 500 + zero mutation (两形态)",
      sc == 500 and "period_proof_invalid" in str(body.get("error"))
      and scb == 500 and "period_proof_invalid" in str(bodyb.get("error"))
      and spendable_balance(u_r9) == b_r9,
      f"{sc}/{body.get('error')} {scb}/{bodyb.get('error')}")

# R10: dispute for an unbound / never-paid local intent → fail loud
u_r10 = _mkuser(_u + "r10")
# pending/failed rows may only carry NULL or ch:* identity. Therefore the
# legal unresolved shape is: 518 has a pending local intent with NO final
# provider identity, while the provider reversal references a canonical
# order identity that 518 never bound. This must fail loud with zero
# economic / Order mutation.
with db() as con:
    o10 = orders_repo.insert_order(
        con, u_r10, "package", pkg_id, "creem", 999, "USD", ts,
        credits_snapshot=100)
    order10 = int(o10["id"])
_TXNS[f"tran_{_u}r10"] = {"object": "transaction", "id": f"tran_{_u}r10",
                          "status": "chargeback", "type": "payment",
                          "order": f"ord10_{_u}"}
b_r10 = spendable_balance(u_r10)
sc, body = _fire("dispute.created", f"tran_{_u}r10")
with db() as con:
    or10 = con.execute(
        "select status, provider_order_id from orders where id=%s",
        (order10,)).fetchone()
check("R10 dispute for unbound never-paid order → fail loud + zero mutation",
      sc == 500 and "package_order_not_found" in str(body.get("error"))
      and spendable_balance(u_r10) == b_r10
      and or10["status"] == "pending" and or10["provider_order_id"] is None,
      f"{sc} {body} {dict(or10)}")

# R11: subscription reversal replay → reversed=false / credits=0
sc, body = _fire("refund.created", f"tran_{_u}rr1")
check("R11 replay response 事实: reversed=false / credits=0",
      sc == 200 and body.get("reversed") is False
      and int(body.get("credits") or 0) == 0, f"{sc} {body}")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
