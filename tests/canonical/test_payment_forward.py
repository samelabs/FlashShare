#!/usr/bin/env python3
"""Payment forward canonical — product facts → Creem proof → fulfillment.

P-series proves over real functions/routes (Creem isolated by module
fakes, DB isolated):
  subscription price authority (518 authoritative → Creem mirrored →
  checkout verified; mismatch fail closed; no recurring custom_price);
  package credits snapshot (checkout freezes quantity; later package
  edits never rewrite a checked-out order; replay no double grant);
  paid-period transaction proof (webhook proof fields; reconcile needs
  a matching paid invoice; negative transaction states never grant).
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
import json as _json
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

_u = "pc" + _os.urandom(4).hex()
buyer = None  # registered via HTTP below (needs a real password)

# ═══ Creem isolation (module fakes; no network) ═══
from economy import creem as _creem
from economy import reconcile as _rec

_mirror_state = {"prod_pc1": {"price": 999, "currency": "USD",
                              "billing_type": "recurring"}}
_patch_calls = {"n": 0, "fail": False, "last": None}
_txns = {}          # txn_id -> transaction payload

def _fake_mirror(product_id, price_cents, currency):
    _patch_calls["n"] += 1
    _patch_calls["last"] = (product_id, price_cents, currency)
    if _patch_calls["fail"]:
        raise RuntimeError("provider down")
    _mirror_state[product_id] = {"price": price_cents, "currency": currency,
                                 "billing_type": "recurring"}
    return {"object": "product", "id": product_id, "price": price_cents,
            "currency": currency, "billing_type": "recurring"}

def _fake_verify(product_id, price_cents, currency):
    prod = _mirror_state.get(product_id)
    if prod is None:
        return "not_found"
    if int(prod["price"]) != int(price_cents):
        return "price"
    if str(prod["currency"]).upper() != str(currency).upper():
        return "currency"
    if prod["billing_type"] != "recurring":
        return "billing_type"
    return ""

_orig_creem = (_creem.mirror_product_price, _creem.verify_product_mirror,
               _creem.create_subscription_checkout, _creem.create_credits_checkout,
               _creem.creem_enabled, _creem.credits_ready,
               _creem.CREEM_CREDITS_CURRENCY)
_credits_chk = {"n": 0}
def _fake_sub_checkout(order_id, plan, user):
    # mirror the REAL contract: verify the product mirror before any
    # checkout session is created (fakes must not weaken the boundary)
    err = _fake_verify(plan["creem_product_id"], int(plan["price_cents"]),
                       str(plan.get("currency") or "USD"))
    if err:
        raise RuntimeError(f"price_mirror_mismatch:{err}")
    return {"ok": True, "checkout_url": f"https://creem.test/s/{order_id}",
            "checkout_id": f"ch_pc_{order_id}"}
def _fake_credits_checkout(order_id, cents, user):
    _credits_chk["n"] += 1
    return {"ok": True, "checkout_url": f"https://creem.test/t/{order_id}",
            "checkout_id": f"ch_pc_{order_id}"}
_creem.mirror_product_price = _fake_mirror
_creem.verify_product_mirror = _fake_verify
_creem.create_subscription_checkout = _fake_sub_checkout
_creem.create_credits_checkout = _fake_credits_checkout
_creem.creem_enabled = lambda: True
_creem.credits_ready = lambda: True
_creem.CREEM_CREDITS_CURRENCY = "USD"

# ═══ P1: 518 price change writes through to Creem ═══
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)

op = _mkuser(_u + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess = create_browser_session(op)

# create a paid plan bound to prod_pc1 (mirror called at creation)
with db() as con:
    _so = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p WHERE p.code <> 'free'"
    ).fetchone()["so"])
r = client.post("/samelabs/plans", json={
    "code": f"plus_{_u}", "name": "Plus PC", "monthly_credits": 500,
    "price_cents": 999, "creem_product_id": "prod_pc1", "sort_order": _so,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
check("P1 create bound plan → mirror write-through at creation",
      r.status_code == 200 and _patch_calls["n"] == 1
      and _mirror_state["prod_pc1"]["price"] == 999,
      f"{r.status_code} patch={_patch_calls['n']}")
plan_id = r.json()["plan"]["id"]

# price update → Creem PATCH first, then local commit
r = client.patch(f"/samelabs/plans/{plan_id}", json={"price_cents": 1499},
                 cookies={COOKIE_NAME: op_sess})
check("P1 改价 → Creem Product update 被调用",
      r.status_code == 200 and _patch_calls["n"] == 2
      and _patch_calls["last"] == ("prod_pc1", 1499, "USD"),
      f"{r.status_code} last={_patch_calls['last']}")
with db() as con:
    local_price = con.execute("select price_cents from plans where id=%s",
                              (plan_id,)).fetchone()["price_cents"]
check("P1 provider 成功 → local price commit (1499)", int(local_price) == 1499)

# non-price edit → NO Creem call
n0 = _patch_calls["n"]
r = client.patch(f"/samelabs/plans/{plan_id}", json={"name": "Plus PC2"},
                 cookies={COOKIE_NAME: op_sess})
check("P1 非价格修改不调用 Creem",
      r.status_code == 200 and _patch_calls["n"] == n0,
      f"{r.status_code} calls {_patch_calls['n']}-{n0}")

# ═══ P2: Creem update fail → local price NOT committed ═══
_patch_calls["fail"] = True
r = client.patch(f"/samelabs/plans/{plan_id}", json={"price_cents": 1999},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    local_price2 = con.execute("select price_cents from plans where id=%s",
                               (plan_id,)).fetchone()["price_cents"]
check("P2 Creem 更新失败 → 502 + local price 不 commit",
      r.status_code == 502 and int(local_price2) == 1499,
      f"{r.status_code} price={local_price2}")
_patch_calls["fail"] = False

# ═══ P3/P4: checkout verification (mirror consistent → allowed; mismatch → fail closed) ═══
import urllib.parse as _up
st = client.post("/auth/register", data={
    "username": _u + "b", "email": f"{_u}b@t.co",
    "password": "password123", "password_confirm": "password123"},
    follow_redirects=False).status_code
lg = client.post("/auth/login", data={"login": _u + "b", "password": "password123"},
                 follow_redirects=False)
buyer_ck = (lg.cookies.get(COOKIE_NAME) or "")
with db() as con:
    buyer = int(con.execute(
        "select id from users where username=%s", (_u + "b",)).fetchone()["id"])
check("P0 buyer 注册+登录 ok", lg.status_code == 302 and buyer_ck, lg.status_code)

def _api(path, body):
    r = client.post(path, json=body, cookies={COOKIE_NAME: buyer_ck})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}

# consistent mirror (1499 in both) → checkout allowed
st, d = _api("/api/topup/subscribe", {"plan_id": plan_id})
check("P3 provider/local 价格一致 → checkout allowed",
      st == 200 and d.get("ok"), f"{st} {str(d)[:90]}")

# mismatch: mirror drifts to 999 while local says 1499 → fail closed
# (second buyer: the first buyer's pending order must not mask the test)
_mirror_state["prod_pc1"]["price"] = 999
client.post("/auth/register", data={
    "username": _u + "b2", "email": f"{_u}b2@t.co",
    "password": "password123", "password_confirm": "password123"})
lg2 = client.post("/auth/login", data={"login": _u + "b2", "password": "password123"},
                  follow_redirects=False)
_ck2 = lg2.cookies.get(COOKIE_NAME) or ""
r_mis = client.post("/api/topup/subscribe", json={"plan_id": plan_id},
                    cookies={COOKIE_NAME: _ck2})
st2, d2 = r_mis.status_code, r_mis.json()
check("P4 mirror mismatch → checkout fail closed (不按 Creem 价售卖)",
      st2 == 502 and "checkout_failed" in str(d2.get("error", "")),
      f"{st2} {str(d2)[:90]}")
with db() as con:
    local_price3 = con.execute("select price_cents from plans where id=%s",
                               (plan_id,)).fetchone()["price_cents"]
check("P4 mismatch 时 518 Plan 未被 checkout 路径改动",
      int(local_price3) == 1499, local_price3)

# recurring checkout payload has NO custom_price (source contract)
_src = open(_os.path.join(_REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
_sub_fn = _src[_src.index("def create_subscription_checkout"):
               _src.index("def upgrade_subscription")]
check("P5 subscription checkout payload 无 custom_price (源合同)",
      "custom_price" not in _sub_fn.replace("ZERO custom_price", ""))

# ═══ P6-P8: package credits snapshot ═══
with db() as con:
    pkg = con.execute(
        "insert into packages (code, name, credits, price_cents, currency,"
        " active, created_at, updated_at) values (%s, 'PC Pkg', 10000, 1000,"
        " 'USD', true, %s, %s) returning id",
        (f"pkg_{_u}", ts, ts)).fetchone()
    pkg_id = pkg["id"]
st, d = _api("/api/topup/checkout", {"package_id": pkg_id})
check("P6 package checkout ok + order 冻结 credits_snapshot=10000",
      st == 200 and d.get("ok"), f"{st} {str(d)[:90]}")
with db() as con:
    order = con.execute(
        "select id, credits_snapshot, status from orders where item_id=%s"
        " and kind='package' order by id desc limit 1", (pkg_id,)).fetchone()
check("P6 snapshot 字段落库 = 10000", int(order["credits_snapshot"]) == 10000,
      str(dict(order)))

# operator mutates the package AFTER checkout: 10000 → 20000
r = client.patch(f"/samelabs/packages/{pkg_id}", json={"credits": 20000},
                 cookies={COOKIE_NAME: op_sess})
check("P7 operator 改 package → 20000 (live 行可变)", r.status_code == 200)

# fulfillment reads ONLY the snapshot → grants 10000, not 20000
_out = _creem.fulfill(provider_order_id=f"ch_pc_{order['id']}",
                      checkout_id="", amount_cents=1000,
                      product_id="whatever-credits-product",
                      currency="USD") if False else None
# restore real fulfill (only checkout fakes needed isolation)
_creem.mirror_product_price, _creem.verify_product_mirror = _orig_creem[0], _orig_creem[1]
_creem.create_subscription_checkout = _orig_creem[2]
_creem.create_credits_checkout = _orig_creem[3]
from economy.billing import spendable_balance
_bal0 = spendable_balance(buyer)
_out = _orig_creem and None
# set the shared product contract env pieces fulfill checks
_creem.CREEM_CREDITS_PRODUCT_ID = "prod_credits_shared"
# fulfill validates product/amount/currency against order row + env
try:
    _creem.fulfill(provider_order_id="", checkout_id=f"ch_pc_{order['id']}",
                   amount_cents=1000, product_id="prod_credits_shared",
                   currency="USD")
    granted = spendable_balance(buyer) - _bal0
except Exception as e:
    granted = f"ERR {e}"
check("P7 fulfillment 只读 snapshot → grant 10000 (非 live 20000)",
      granted == 10000, str(granted))

# replay: same order fulfilled again → no double grant
_bal1 = spendable_balance(buyer)
try:
    _creem.fulfill(provider_order_id="", checkout_id=f"ch_pc_{order['id']}",
                   amount_cents=1000, product_id="prod_credits_shared",
                   currency="USD")
except Exception:
    pass
check("P8 webhook replay 不 double grant",
      spendable_balance(buyer) == _bal1, f"{_bal1} → {spendable_balance(buyer)}")

# reverse direction: package raised BEFORE checkout → snapshot freezes high value
with db() as con:
    con.execute("update packages set credits=30000 where id=%s", (pkg_id,))
_creem.creem_enabled = lambda: True
_creem.credits_ready = lambda: True
_creem.create_credits_checkout = _fake_credits_checkout
st, d = _api("/api/topup/checkout", {"package_id": pkg_id})
with db() as con:
    order2 = con.execute(
        "select id, credits_snapshot from orders where item_id=%s"
        " and kind='package' order by id desc limit 1", (pkg_id,)).fetchone()
check("P6b 反向: 改价后 checkout 冻结新值 30000",
      st == 200 and int(order2["credits_snapshot"]) == 30000,
      f"st={st} order={dict(order2)}")

# amount/currency/product fail-closed (existing behavior kept)
with db() as con:
    _prov2 = con.execute("select provider_order_id from orders where id=%s",
                         (order2["id"],)).fetchone()["provider_order_id"]
try:
    _creem.fulfill(provider_order_id=_prov2, checkout_id="",
                   amount_cents=999, product_id="prod_credits_shared",
                   currency="USD")
    check("P8b amount mismatch fail closed", False, "no raise")
except KeyError as e:
    check("P8b amount mismatch fail closed", "amount_mismatch" in str(e), str(e))

# ═══ P9-P16: paid-period transaction proof ═══
from economy.subscription import handle_event, _parse_ts
from repo import orders as orders_repo

PS, PE = ts - 86400, ts + 86400 * 29
def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

member = _mkuser(_u + "m")
sub_id = f"sub_pc_{_u}"
with db() as con:
    prows = con.execute("select id from plans where id=%s", (plan_id,)).fetchone()
    oid = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, created_at) values (%s, 'plan', %s,"
        " 'creem', %s, 'pending', 1499, 'USD', %s) returning id",
        (member, plan_id, "sub:" + sub_id, ts)).fetchone()["id"]
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
        " 'active', %s, %s, %s)",
        (member, plan_id, "sub:" + sub_id, PS, PE, ts))
    con.execute("update plans set monthly_credits=100 where id=%s", (plan_id,))

def _paid_event(with_txn="txn_ok", period=(PS, PE), sub=None, status="active",
                product="prod_pc1", include_txn_field=True):
    ssub = {
        "id": sub or sub_id, "status": status,
        "current_period_start_date": _iso(period[0]),
        "current_period_end_date": _iso(period[1]),
        "product": {"id": product},
    }
    if include_txn_field:
        ssub["last_transaction_id"] = with_txn
    return ("subscription.paid", {"object": "subscription", **ssub})

def _run_paid(**kw):
    et, obj = _paid_event(**kw)
    try:
        return {"ok": handle_event(et, obj)}
    except KeyError as e:
        return {"err": str(e)}

# P9: complete proof → grants exactly once
_bal2 = spendable_balance(member)
_txns["txn_ok"] = {"object": "transaction", "id": "txn_ok", "status": "paid",
                   "type": "invoice", "subscription": sub_id,
                   "period_start": PS, "period_end": PE}
r = _run_paid()
_bal3 = spendable_balance(member)
check("P9 subscription.paid 完整 proof → grant 一次 (100)",
      "ok" in r and _bal3 - _bal2 == 100, f"{r} Δ={_bal3 - _bal2}")

# P10: replay does not double grant
r = _run_paid()
check("P10 paid replay 不 double grant",
      spendable_balance(member) == _bal3, str(spendable_balance(member) - _bal3))

# P11: missing proof fields → fail loud (KeyError), no grant
_bal4 = spendable_balance(member)
r = _run_paid(include_txn_field=False)
check("P11 缺 last_transaction_id → fail loud 不 grant",
      "err" in r and "paid_proof_missing" in r["err"]
      and spendable_balance(member) == _bal4, r)

# ═══ reconcile proofs (P12-P16) ═══
def _rec_setup(txn_payload, sub_extra=None, sub_status="active",
               period=(PS, PE), sub=sub_id):
    snap = {"object": "subscription", "id": sub, "status": sub_status,
            "current_period_start_date": _iso(period[0]),
            "current_period_end_date": _iso(period[1]),
            "product": {"id": "prod_pc1"},
            "last_transaction_id": txn_payload["id"] if txn_payload else ""}
    if sub_extra:
        snap.update(sub_extra)
    _rec.fetch_subscription = (lambda _s, _snap=snap: _snap)
    _rec.fetch_transaction = (lambda _t, _p=txn_payload: _p)

member2 = _mkuser(_u + "m2")
sub2 = f"sub_pc2_{_u}"
with db() as con:
    oid2 = con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, created_at) values (%s, 'plan', %s,"
        " 'creem', %s, 'pending', 1499, 'USD', %s) returning id",
        (member2, plan_id, "sub:" + sub2, ts)).fetchone()["id"]
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
        " 'active', %s, %s, %s)",
        (member2, plan_id, "sub:" + sub2, PS, PE, ts))

_txid = 0
def _txn(status="paid", typ="invoice", subscription=None, ps=PS, pe=PE):
    global _txid
    _txid += 1
    return {"object": "transaction", "id": f"txn_r{_txid}", "status": status,
            "type": typ, "subscription": subscription or sub2,
            "period_start": ps, "period_end": pe}

# P12: matching paid invoice → reconcile grants
_bal5 = spendable_balance(member2)
_rec_setup(_txn())
r = _rec.reconcile_user(member2)
check("P12 reconcile + matching paid invoice → grant",
      r["granted"] == 1 and spendable_balance(member2) - _bal5 == 100, f"{r}")

# P13: reconcile after webhook does not double grant
_rec_setup(_txn())
r = _rec.reconcile_user(member2)
check("P13 reconcile 在 webhook 后不 double grant",
      r["granted"] == 0, f"{r}")

# P14: negative transaction states never grant
member3 = _mkuser(_u + "m3")
sub3 = f"sub_pc3_{_u}"
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, created_at) values (%s, 'plan', %s,"
        " 'creem', %s, 'pending', 1499, 'USD', %s)",
        (member3, plan_id, "sub:" + sub3, ts))
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
        " 'active', %s, %s, %s)",
        (member3, plan_id, "sub:" + sub3, PS, PE, ts))
_orig_fetch_sub, _orig_fetch_txn = _rec.fetch_subscription, _rec.fetch_transaction
def _rec3(txn, sub_status="active", sub=None):
    _s = sub or sub3
    _rec.fetch_subscription = lambda _x, _s=_s, _st=sub_status: {
        "object": "subscription", "id": _s, "status": _st,
        "current_period_start_date": _iso(PS), "current_period_end_date": _iso(PE),
        "product": {"id": "prod_pc1"},
        "last_transaction_id": (txn or {}).get("id", "")}
    _rec.fetch_transaction = lambda _t, _p=txn: _p

for status in ("pending", "refunded", "partialRefund", "chargedBack"):
    _rec3(_txn(status=status))
    r = _rec.reconcile_user(member3)
    check(f"P14 active + transaction {status} → 不 grant",
          r["granted"] == 0, str(r))
# wrong subscription
_rec3(_txn(subscription="sub_someone_else"))
r = _rec.reconcile_user(member3)
check("P14 transaction 属其它 subscription → 不 grant", r["granted"] == 0, str(r))
# wrong period
_rec3(_txn(ps=PS - 86400, pe=PE - 86400))
r = _rec.reconcile_user(member3)
check("P14 transaction period 不匹配 → 不 grant", r["granted"] == 0, str(r))
# missing transaction
_rec3(None)
r = _rec.reconcile_user(member3)
check("P14 transaction 缺失 → 不 grant", r["granted"] == 0, str(r))
# wrong type (payment, not invoice)
_rec3(_txn(typ="payment"))
r = _rec.reconcile_user(member3)
check("P14 transaction type=payment (非 invoice) → 不 grant",
      r["granted"] == 0, str(r))
# active status alone (no txn id at all)
_rec.fetch_subscription = lambda _x: {
    "object": "subscription", "id": sub3, "status": "active",
    "current_period_start_date": _iso(PS), "current_period_end_date": _iso(PE),
    "product": {"id": "prod_pc1"}}
r = _rec.reconcile_user(member3)
check("P14 active status 本身绝不 grant (无 proof)", r["granted"] == 0, str(r))

# P15: scheduled_cancel + matching paid transaction heals a lost webhook
member4 = _mkuser(_u + "m4")
sub4 = f"sub_pc4_{_u}"
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, created_at) values (%s, 'plan', %s,"
        " 'creem', %s, 'pending', 1499, 'USD', %s)",
        (member4, plan_id, "sub:" + sub4, ts))
    # local row stays 'active' (DB CHECK vocabulary) with the lifecycle
    # marker set — reconcile's eligibility looks at the PROVIDER snapshot
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
        " 'active', %s, %s, %s)",
        (member4, plan_id, "sub:" + sub4, PS, PE, ts))
_rec.fetch_subscription = lambda _x: {
    "object": "subscription", "id": sub4, "status": "scheduled_cancel",
    "current_period_start_date": _iso(PS), "current_period_end_date": _iso(PE),
    "product": {"id": "prod_pc1"}, "last_transaction_id": "txn_sc"}
_rec.fetch_transaction = lambda _t: {
    "object": "transaction", "id": "txn_sc", "status": "paid",
    "type": "invoice", "subscription": sub4,
    "period_start": PS, "period_end": PE}
_bal6 = spendable_balance(member4)
r = _rec.reconcile_user(member4)
check("P15 scheduled_cancel + matching paid txn → 补授丢失 webhook",
      r["granted"] == 1 and spendable_balance(member4) - _bal6 == 100, str(r))

# P16: provider failure → best-effort skip, never grant, never raise
_rec.fetch_subscription = lambda _x: None
try:
    r = _rec.reconcile_user(member4)
    check("P16 provider 失败 → 不抛不 grant", r["granted"] == 0, str(r))
except Exception as e:
    check("P16 provider 失败 → 不抛不 grant", False, str(e))

# ═══ semantic guards ═══
_csrc = open(_os.path.join(_REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
check("S1 fulfillment 不再读 live package credits (源内无 package_credits 调用)",
      "package_credits" not in _csrc)
_rsrc = open(_os.path.join(_REPO, "core", "economy", "reconcile.py"), encoding="utf-8").read()
check("S2 reconcile 有 transaction proof 谓词",
      "transaction_proves_paid_period" in _rsrc and "last_transaction_id" in _rsrc)
_ssrc = open(_os.path.join(_REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
check("S3 paid-period writer 仍只有一个 implementation (apply_paid_period)",
      _ssrc.count("def apply_paid_period") == 1)

# ═══ restore fakes ═══
(_creem.mirror_product_price, _creem.verify_product_mirror,
 _creem.create_subscription_checkout, _creem.create_credits_checkout,
 _creem.creem_enabled, _creem.credits_ready,
 _creem.CREEM_CREDITS_CURRENCY) = _orig_creem
_rec.fetch_subscription, _rec.fetch_transaction = _orig_fetch_sub, _orig_fetch_txn

# ═══ cleanup ═══
with db() as con:
    for uid in (buyer, op, member, member2, member3, member4):
        con.execute("delete from users where id=%s", (uid,))
    con.execute("delete from plans where id=%s", (plan_id,))
    con.execute("delete from packages where id=%s", (pkg_id,))

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print(f"FAIL: 无 — 全绿 ({COUNT} checks)")
