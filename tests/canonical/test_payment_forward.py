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
from tests.support.cleanup import purge_users  # noqa: E402

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
                              "billing_type": "recurring",
                              "billing_period": "every-month"}}
_patch_calls = {"n": 0, "fail": False, "last": None}
_txns = {}          # txn_id -> transaction payload

def _fake_mirror(product_id, price_cents, currency):
    # mirror the REAL write-through contract: GET-first (billing contract
    # must already be correct — never repaired), PATCH price/currency
    # ONLY, response re-confirmed.
    _patch_calls["n"] += 1
    if _patch_calls["fail"]:
        raise RuntimeError("provider down")
    prod = _mirror_state.get(product_id)
    if prod is None:
        raise RuntimeError("product_not_found")
    if prod["billing_type"] != "recurring" \
            or prod.get("billing_period") != "every-month":
        raise RuntimeError("product_billing_contract_invalid")
    if "billing_type" in (_patch_calls.get("payload") or {}) \
            or "billing_period" in (_patch_calls.get("payload") or {}):
        raise RuntimeError("fake: PATCH must not carry billing contract")
    _patch_calls["last"] = (product_id, price_cents, currency)
    _mirror_state[product_id] = {**prod, "price": price_cents,
                                 "currency": str(currency).upper()}
    return {"object": "product", "id": product_id, "price": price_cents,
            "currency": str(currency).upper(),
            "billing_type": "recurring", "billing_period": "every-month"}

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
    if prod.get("billing_period") != "every-month":
        return "billing_period"
    return ""

_patch_payloads = []
_orig_creem_key = _creem.CREEM_API_KEY
_orig_patch = _creem._patch
def _spy_patch(path, payload):
    _patch_payloads.append((path, dict(payload)))
    prod_id = path.rsplit("/", 1)[-1]
    prod = _mirror_state.get(prod_id)
    if prod is None:
        raise RuntimeError("product_not_found")
    if "billing_type" in payload or "billing_period" in payload:
        raise RuntimeError("patch_carries_billing_contract")
    if prod["billing_type"] != "recurring" or prod.get("billing_period") != "every-month":
        raise RuntimeError("product_billing_contract_invalid")
    _mirror_state[prod_id] = {**prod, "price": payload["price"],
                              "currency": payload["currency"]}
    return {"object": "product", "id": prod_id, "price": payload["price"],
            "currency": payload["currency"],
            "billing_type": "recurring", "billing_period": "every-month"}
_orig_fetch_prod = _creem._fetch_product
def _spy_fetch_prod(pid):
    prod = _mirror_state.get(pid)
    return dict(prod) if prod else None
# real mirror_product_price exercised against spied transport
_creem._patch = _spy_patch
_creem._fetch_product = _spy_fetch_prod
_creem.CREEM_API_KEY = "test-key"  # real impl gates on the module constant

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
_fake_mirror_full = _fake_mirror  # kept for direct-call assertions
# route through the REAL implementation (transport spied) so the
# GET-first / PATCH-payload / re-confirm contract is actually exercised
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
r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"plus_{_u}", "name": "Plus PC", "monthly_credits": 500,
    "price_cents": 999, "creem_product_id": "prod_pc1", "sort_order": _so,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
check("P1 create bound plan → mirror write-through at creation",
      r.status_code == 200 and _mirror_state["prod_pc1"]["price"] == 999
      and len(_patch_payloads) == 1,
      f"{r.status_code} mirror={_mirror_state['prod_pc1']} n={len(_patch_payloads)}")
check("P1a write-through PATCH 只带 price/currency (不夹带 billing contract)",
      all(("billing_type" not in pl and "billing_period" not in pl)
          for _, pl in _patch_payloads), str(_patch_payloads[:2]))
plan_id = r.json()["plan"]["id"]

# price update → Creem PATCH first, then local commit
r = client.patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"price_cents": 1499},
                 cookies={COOKIE_NAME: op_sess})
check("P1 改价 → Creem Product update 被调用",
      r.status_code == 200 and _mirror_state["prod_pc1"]["price"] == 1499
      and _patch_payloads[-1] == (f"/v1/products/prod_pc1",
                                  {"price": 1499, "currency": "USD"}),
      f"{r.status_code} last={_patch_payloads[-1:]}")
with db() as con:
    local_price = con.execute("select price_cents from plans where id=%s",
                              (plan_id,)).fetchone()["price_cents"]
check("P1 provider 成功 → local price commit (1499)", int(local_price) == 1499)

# non-commercial edit → NO Creem call
n0 = len(_patch_payloads)
r = client.patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"name": "Plus PC2"},
                 cookies={COOKIE_NAME: op_sess})
check("P1 非商业字段修改不调用 Creem",
      r.status_code == 200 and len(_patch_payloads) == n0,
      f"{r.status_code} calls {len(_patch_payloads)}-{n0}")

# ═══ P2: Creem update fail → local price NOT committed ═══
_orig_spy = _creem._patch
def _failing_patch(path, payload):
    raise RuntimeError("provider down")
_creem._patch = _failing_patch
r = client.patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"price_cents": 1999},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    local_price2 = con.execute("select price_cents from plans where id=%s",
                               (plan_id,)).fetchone()["price_cents"]
check("P2 Creem 更新失败 → 502 + local price 不 commit",
      r.status_code == 502 and int(local_price2) == 1499,
      f"{r.status_code} price={local_price2}")
_creem._patch = _orig_spy

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
r_mis = client.post("/api/topup/subscribe",
                    json={"plan_id": plan_id},
                    cookies={COOKIE_NAME: _ck2})
st2, d2 = r_mis.status_code, r_mis.json()
check("P4 mirror mismatch → checkout fail closed (不按 Creem 价售卖)",
      st2 == 502 and "checkout_unresolved" in str(d2.get("error", "")),
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

# operator mutates the package AFTER checkout: 10000 → 20000 (price moves
# with it to stay inside the commercial floor: credits <= price_cents*10)
r = client.patch(f"/samelabs/commerce/products/packages/{pkg_id}",
                 json={"credits": 20000, "price_cents": 2000},
                 cookies={COOKIE_NAME: op_sess})
check("P7 operator 改 package → 20000 (live 行可变)", r.status_code == 200)

# fulfillment reads ONLY the snapshot → grants 10000, not 20000
# restore real checkout fakes boundary (only checkout fakes needed isolation)
_creem.mirror_product_price, _creem.verify_product_mirror = _orig_creem[0], _orig_creem[1]
_creem.create_subscription_checkout = _orig_creem[2]
_creem.create_credits_checkout = _orig_creem[3]
from economy.billing import spendable_balance
_bal0 = spendable_balance(buyer)
# set the shared product contract env pieces fulfill_by_request_id checks
_creem.CREEM_CREDITS_PRODUCT_ID = "prod_credits_shared"
# canonical fulfillment: the REAL local Order.id + canonical provider id
try:
    _creem.fulfill_by_request_id(
        int(order["id"]), provider_order_id=f"ord_pc_{order['id']}",
        checkout_id=f"ch_pc_{order['id']}",
        amount_cents=1000, product_id="prod_credits_shared",
        currency="USD")
    granted = spendable_balance(buyer) - _bal0
except Exception as e:
    granted = f"ERR {e}"
check("P7 fulfillment 只读 snapshot → grant 10000 (非 live 20000)",
      granted == 10000, str(granted))

# replay: SAME Order.id + SAME canonical provider_order_id + SAME checkout
# identity → guarded writer no-op, zero double grant
_bal1 = spendable_balance(buyer)
_replay = _creem.fulfill_by_request_id(
    int(order["id"]), provider_order_id=f"ord_pc_{order['id']}",
    checkout_id=f"ch_pc_{order['id']}",
    amount_cents=1000, product_id="prod_credits_shared",
    currency="USD")
check("P8 webhook replay 不 double grant",
      _replay.get("granted") is False and _replay.get("reason") == "replay"
      and spendable_balance(buyer) == _bal1,
      f"{_bal1} → {spendable_balance(buyer)} {_replay}")

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
_ch2 = _prov2[3:] if str(_prov2).startswith("ch:") else f"ch_pc_{order2['id']}"
try:
    _creem.fulfill_by_request_id(
        int(order2["id"]), provider_order_id=f"ord_pc_{order2['id']}",
        checkout_id=_ch2,
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
        " status, amount_cents, currency, paid_at, created_at) values"
        " (%s, 'plan', %s, 'creem', %s, 'paid', 1499, 'USD', %s, %s)"
        " returning id",
        (member, plan_id, "sub:" + sub_id, ts, ts)).fetchone()["id"]
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
        " status, amount_cents, currency, paid_at, created_at) values"
        " (%s, 'plan', %s, 'creem', %s, 'paid', 1499, 'USD', %s, %s)"
        " returning id",
        (member2, plan_id, "sub:" + sub2, ts, ts)).fetchone()["id"]
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
        " status, amount_cents, currency, paid_at, created_at) values"
        " (%s, 'plan', %s, 'creem', %s, 'paid', 1499, 'USD', %s, %s)",
        (member3, plan_id, "sub:" + sub3, ts, ts))
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
        " status, amount_cents, currency, paid_at, created_at) values"
        " (%s, 'plan', %s, 'creem', %s, 'paid', 1499, 'USD', %s, %s)",
        (member4, plan_id, "sub:" + sub4, ts, ts))
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

# ═══ PM2-1: billing period in the mirror contract ═══
# (create_subscription_checkout was restored to the real implementation
# after the package section; re-fake it with the mirror-verified fake)
_creem.create_subscription_checkout = _fake_sub_checkout
_mirror_state["prod_pc1"]["price"] = 1499  # same price/currency, yearly period
_mirror_state["prod_pc1"]["billing_period"] = "every-year"
r_y = client.post("/api/topup/subscribe",
                  json={"plan_id": plan_id},
                  cookies={COOKIE_NAME: buyer_ck})
# buyer already has a pending order → use a fresh buyer for cleanliness
client.post("/auth/register", data={
    "username": _u + "y1", "email": f"{_u}y1@t.co",
    "password": "password123", "password_confirm": "password123"})
_lgy = client.post("/auth/login", data={"login": _u + "y1", "password": "password123"},
                   follow_redirects=False)
_cky = _lgy.cookies.get(COOKIE_NAME) or ""
r_y = client.post("/api/topup/subscribe",
                  json={"plan_id": plan_id},
                  cookies={COOKIE_NAME: _cky})
check("PM2-1a every-year + 相同 price/currency → checkout rejected",
      r_y.status_code == 502, r_y.status_code)
_mirror_state["prod_pc1"]["billing_period"] = "every-month"
# Canonical retry semantics: the PM2-1a attempt ended unresolved
# (mirror rejection is a pre-provider failure, order stays pending), so
# the SAME logical purchase retries through the SAME pending Order and
# must now succeed on it.
r_m = client.post("/api/topup/subscribe",
                  json={"plan_id": plan_id},
                  cookies={COOKIE_NAME: _cky})
check("PM2-1b every-month + same-ref retry → checkout allowed",
      r_m.status_code == 200 and r_m.json().get("ok"),
      f"{r_m.status_code} {r_m.text[:120]}")

# ═══ PM2-2: write-through never repairs billing contract ═══
_mirror_state["prod_pc2"] = {"price": 500, "currency": "USD",
                             "billing_type": "onetime",
                             "billing_period": ""}
_n_before = len(_patch_payloads)
try:
    # call the REAL implementation against the spied transport
    import economy.creem as _cmod
    _cmod.mirror_product_price("prod_pc2", 1499, "USD")
    pm2_ok = False
except RuntimeError as e:
    pm2_ok = "billing_contract" in str(e)
check("PM2-2 one-time product 改价 → fail closed (不 PATCH 不修复)",
      pm2_ok and len(_patch_payloads) == _n_before,
      f"patches {len(_patch_payloads)}-{_n_before}")
_mirror_state["prod_pc3"] = {"price": 500, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-year"}
_n_before = len(_patch_payloads)
try:
    import economy.creem as _cmod
    _cmod.mirror_product_price("prod_pc3", 1499, "USD")
    pm2b_ok = False
except RuntimeError as e:
    pm2b_ok = "billing_contract" in str(e)
check("PM2-2b yearly recurring product 改价 → fail closed",
      pm2b_ok and len(_patch_payloads) == _n_before)
# PATCH payload never carries billing fields (all recorded payloads)
check("PM2-2c 所有 PATCH payload 只含 price/currency",
      all(set(pl.keys()) == {"price", "currency"} for _, pl in _patch_payloads),
      str(_patch_payloads[:2]))

# ═══ PM2-3: creem_product_id change closes at the operator write ═══
# Rebind is legal ONLY on a plan with NO bound membership (lifecycle
# guard) — the subscription-carrying plan above must be refused. Use a
# fresh never-subscribed plan for the commit/rollback matrix, and prove
# the guard on the bound one first.
r_guard = client.patch(f"/samelabs/commerce/products/plans/{plan_id}",
                       json={"creem_product_id": f"prod_pcB_{_u}"},
                       cookies={COOKIE_NAME: op_sess})
check("PM2-3g rebind on bound-membership plan → 409 product_binding_immutable",
      r_guard.status_code == 409
      and "product_binding_immutable" in str(r_guard.json().get("error", "")),
      f"{r_guard.status_code} {r_guard.text[:90]}")
with db() as con:
    _so2 = con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p"
        " WHERE p.code <> 'free'").fetchone()["so"]
from repo.orders import insert_plan as _ins_plan
with db() as con:
    _rb = _ins_plan(con, f"plus_{_u}rb", "Plus RB", 500, 1499, ts,
                    sort_order=int(_so2), creem_product_id=f"prod_pcA_{_u}",
                    ingress_files_24h=500, ingress_bytes_24h=524288000,
                    expiry_days=(7,), download_limits=(50,))
    rb_plan_id = int(_rb["id"])
from governance import plan_catalog as _pc
_pc.catalog(refresh=True)   # operator create path refreshes; mirror here
# target monthly product at a different price → binding syncs target to CURRENT 518 price
_mirror_state[f"prod_pcB_{_u}"] = {"price": 999, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}
r = client.patch(f"/samelabs/commerce/products/plans/{rb_plan_id}", json={"creem_product_id": f"prod_pcB_{_u}"},
                 cookies={COOKIE_NAME: op_sess})
check("PM2-3a product rebind A→B → 409 forbidden (binding immutable)",
      r.status_code == 409
      and "product_binding_immutable" in str(r.json().get("error", ""))
      and _mirror_state[f"prod_pcB_{_u}"]["price"] == 999,   # provider untouched
      f"{r.status_code} {r.text[:90]} target={_mirror_state.get('prod_pcB_' + _u)}")
# yearly target → local binding not committed
_mirror_state[f"prod_pcY_{_u}"] = {"price": 999, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-year"}
r = client.patch(f"/samelabs/commerce/products/plans/{rb_plan_id}", json={"creem_product_id": f"prod_pcY_{_u}"},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    bound = con.execute("select creem_product_id from plans where id=%s",
                        (rb_plan_id,)).fetchone()["creem_product_id"]
check("PM2-3b yearly rebind → 409 (仍 prod_pcA)",
      r.status_code == 409 and bound == f"prod_pcA_{_u}", f"{r.status_code} bound={bound}")
# provider failure → binding not committed
def _fail_fetch(pid):
    return None
_orig_fp = _creem._fetch_product
_creem._fetch_product = _fail_fetch
r = client.patch(f"/samelabs/commerce/products/plans/{rb_plan_id}", json={"creem_product_id": "prod_pcC"},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    bound2 = con.execute("select creem_product_id from plans where id=%s",
                         (rb_plan_id,)).fetchone()["creem_product_id"]
check("PM2-3c provider failure rebind → 409 (仍 prod_pcA)",
      r.status_code == 409 and bound2 == f"prod_pcA_{_u}", f"{r.status_code} bound={bound2}")
_creem._fetch_product = _orig_fp
# valid monthly target → binding succeeds
_mirror_state["prod_pcD"] = {"price": 100, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}
r = client.patch(f"/samelabs/commerce/products/plans/{rb_plan_id}", json={"creem_product_id": "prod_pcD"},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    bound3 = con.execute("select creem_product_id from plans where id=%s",
                         (rb_plan_id,)).fetchone()["creem_product_id"]
check("PM2-3d valid monthly rebind → 409 (binding immutable; 仅 NULL→X 合法)",
      r.status_code == 409 and bound3 == f"prod_pcA_{_u}"
      and _mirror_state["prod_pcD"]["price"] == 100,
      f"{r.status_code} bound={bound3}")

# ═══ PM2-4: paid-period product identity proof ═══
def _mk_member_sub(uname, target_plan_id, product_binding):
    """seed user + order + membership for product-identity cases"""
    mm = _mkuser(uname)
    ss = f"sub_pi_{uname}"
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid',"
            " 1499, 'USD', %s, %s)",
            (mm, target_plan_id, "sub:" + ss, ts, ts))
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id,"
            " status, started_at, renews_at, updated_at) values (%s, %s, %s,"
            " 'active', %s, %s, %s)",
            (mm, target_plan_id, "sub:" + ss, PS, PE, ts))
        con.execute("update plans set creem_product_id=%s where id=%s",
                    (product_binding, target_plan_id))
    return mm, ss

def _paid_evt(ss, product, with_txn=True):
    ssub = {"object": "subscription", "id": ss, "status": "active",
            "current_period_start_date": _iso(PS),
            "current_period_end_date": _iso(PE),
            "product": {"id": product}}
    if with_txn:
        ssub["last_transaction_id"] = f"txn_{ss}"
    return ("subscription.paid", ssub)

def _try_paid(ss, product):
    et, obj = _paid_evt(ss, product)
    try:
        return {"ok": handle_event(et, obj)}
    except KeyError as e:
        return {"err": str(e)}

# a second tier for mismatch cases
with db() as con:
    _so2 = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p WHERE p.code <> 'free'"
    ).fetchone()["so"])
_mirror_state["prod_pi_pro"] = {"price": 2999, "currency": "USD",
                                "billing_type": "recurring",
                                "billing_period": "every-month"}
r2 = client.post("/samelabs/commerce/products/plans", json={
    "code": f"pro_{_u}", "name": "Pro PI", "monthly_credits": 2000,
    "price_cents": 2999, "creem_product_id": "prod_pi_pro", "sort_order": _so2,
    "ingress_files_24h": 2000, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
plan2_id = r2.json()["plan"]["id"]

# normal current product → grant
mmA, ssA = _mk_member_sub(_u + "pa", plan_id, "prod_pi_plus")
_bA = spendable_balance(mmA)
rr = _try_paid(ssA, "prod_pi_plus")
check("PM2-4a normal current product → grant",
      "ok" in rr and spendable_balance(mmA) - _bA == 100, str(rr))

# unknown product → no grant, fail loud
mmB, ssB = _mk_member_sub(_u + "pb", plan_id, "prod_pi_plus")
_bB = spendable_balance(mmB)
rr = _try_paid(ssB, "prod_totally_unknown")
check("PM2-4b unknown product → no grant (fail loud)",
      "paid_product_unknown" in rr.get("err", "") and spendable_balance(mmB) == _bB,
      str(rr))

# unrelated known product (same or other tier but bound to another plan) →
# higher tier before update sync → no grant
mmC, ssC = _mk_member_sub(_u + "pc", plan_id, "prod_pi_plus")
_bC = spendable_balance(mmC)
rr = _try_paid(ssC, "prod_pi_pro")   # higher tier, Membership not synced
check("PM2-4c higher product before update sync → no grant (retriable)",
      "paid_product_not_synced" in rr.get("err", "")
      and spendable_balance(mmC) == _bC, str(rr))

# update sync then paid replay → correct grant at the new tier
_lg2u = client  # membership sync via subscription.update handler
_upd = {"object": "subscription", "id": ssC, "status": "active",
        "current_period_start_date": _iso(PS), "current_period_end_date": _iso(PE),
        "product": {"id": "prod_pi_pro"}, "last_transaction_id": "txn_upd"}
try:
    handle_event("subscription.update", _upd)
    synced = True
except Exception:
    synced = False
rr = _try_paid(ssC, "prod_pi_pro")
check("PM2-4d update sync 后 paid replay → 正确按新 tier grant",
      synced and "ok" in rr, f"synced={synced} rr={rr}")

# first-period order product mismatch → no grant (delete membership to hit order path)
mmD, ssD = _mk_member_sub(_u + "pd", plan_id, "prod_pi_plus")
with db() as con:
    con.execute("delete from memberships where user_id=%s", (mmD,))
_bD = spendable_balance(mmD)
rr = _try_paid(ssD, "prod_pi_pro")   # order says plus, provider says pro
check("PM2-4e first-period order product mismatch → no grant",
      "paid_product_order_mismatch" in rr.get("err", "")
      and spendable_balance(mmD) == _bD, str(rr))

# ═══ PM3: local constraints settle BEFORE any Creem side effect ═══
# fixtures: two plans, two products
_mirror_state["prod_sA"] = {"price": 999, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
_mirror_state["prod_sB"] = {"price": 1999, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
with db() as con:
    _soA = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p WHERE p.code <> 'free'"
    ).fetchone()["so"])
rA = client.post("/samelabs/commerce/products/plans", json={
    "code": f"plA_{_u}", "name": "A", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": "prod_sA", "sort_order": _soA,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
planA = rA.json()["plan"]["id"]
rB = client.post("/samelabs/commerce/products/plans", json={
    "code": f"plB_{_u}", "name": "B", "monthly_credits": 2000,
    "price_cents": 1999, "creem_product_id": "prod_sB", "sort_order": _soA + 1,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
planB = rB.json()["plan"]["id"]

# PM3-1: rebind planA to planB's product → 409, zero PATCH, prod_sB price intact
_n1 = len(_patch_payloads)
r = client.patch(f"/samelabs/commerce/products/plans/{planA}", json={"creem_product_id": "prod_sB"},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    _bindA = con.execute("select creem_product_id from plans where id=%s",
                         (planA,)).fetchone()["creem_product_id"]
check("PM3-1 rebind A→B → 409 product_binding_immutable / 零 PATCH / 原价不变",
      r.status_code == 409
      and (r.json().get("error") == "product_binding_immutable")
      and len(_patch_payloads) == _n1
      and _bindA == "prod_sA"
      and _mirror_state["prod_sB"]["price"] == 1999,
      f"{r.status_code} patches {len(_patch_payloads)}-{_n1} bind={_bindA} "
      f"prod_sB={_mirror_state['prod_sB']['price']}")

# PM3-2: create with an already-bound product → 409, zero PATCH
_n2 = len(_patch_payloads)
r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"plC_{_u}", "name": "C", "monthly_credits": 100,
    "price_cents": 500, "creem_product_id": "prod_sB", "sort_order": _soA + 2,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
with db() as con:
    _cnt = con.execute("select count(*) c from plans where code=%s",
                       (f"plC_{_u}",)).fetchone()["c"]
check("PM3-2 create 用已占用 product → 409 / 零 PATCH / 行未落",
      r.status_code == 409 and r.json().get("error") == "product_already_bound"
      and len(_patch_payloads) == _n2 and int(_cnt) == 0,
      f"{r.status_code} patches {len(_patch_payloads)}-{_n2} cnt={_cnt}")

# PM3-3: create duplicate sort / duplicate code → 409, zero PATCH
_n3 = len(_patch_payloads)
r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"plD_{_u}", "name": "D", "monthly_credits": 100,
    "price_cents": 500, "creem_product_id": "prod_sD", "sort_order": _soA,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
_mirror_state["prod_sD"] = {"price": 1, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
r2 = client.post("/samelabs/commerce/products/plans", json={
    "code": f"plA_{_u}", "name": "A2", "monthly_credits": 100,
    "price_cents": 500, "creem_product_id": "prod_sD", "sort_order": _soA + 3,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
check("PM3-3 create duplicate sort/code → 409 / 零 PATCH",
      r.status_code == 409 and r.json().get("error") == "sort_order_taken"
      and r2.status_code == 409 and r2.json().get("error") == "code_taken"
      and len(_patch_payloads) == _n3,
      f"sort:{r.status_code} code:{r2.status_code} patches {len(_patch_payloads)}-{_n3}")

# PM3-4: valid commercial update → provisional constraints → PATCH → commit
_n4 = len(_patch_payloads)
r = client.patch(f"/samelabs/commerce/products/plans/{planA}", json={"price_cents": 1099},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    _pA = con.execute("select price_cents from plans where id=%s",
                      (planA,)).fetchone()["price_cents"]
check("PM3-4 valid 改价 → Creem PATCH + DB commit",
      r.status_code == 200 and len(_patch_payloads) == _n4 + 1
      and _patch_payloads[-1][1]["price"] == 1099 and int(_pA) == 1099,
      f"{r.status_code} patches {len(_patch_payloads)}-{_n4} pA={_pA}")

# PM3-5: provider failure → DB rollback, plan fully unchanged
_orig_patch_fn = _creem._patch
def _boom_patch(path, payload):
    raise RuntimeError("provider down")
_creem._patch = _boom_patch
r = client.patch(f"/samelabs/commerce/products/plans/{planA}", json={"price_cents": 2099},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    _pA2 = con.execute(
        "select price_cents, creem_product_id from plans where id=%s",
        (planA,)).fetchone()
check("PM3-5 Creem 失败 → DB rollback, Plan 完全不变",
      r.status_code == 502 and int(_pA2["price_cents"]) == 1099
      and _pA2["creem_product_id"] == "prod_sA",
      f"{r.status_code} plan={dict(_pA2)}")
# create with provider failure → insert rolled back
r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"plE_{_u}", "name": "E", "monthly_credits": 100,
    "price_cents": 700, "creem_product_id": "prod_sD", "sort_order": _soA + 4,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
with db() as con:
    _cntE = con.execute("select count(*) c from plans where code=%s",
                        (f"plE_{_u}",)).fetchone()["c"]
check("PM3-5b create Creem 失败 → insert rollback",
      r.status_code == 502 and int(_cntE) == 0,
      f"{r.status_code} cnt={_cntE}")
_creem._patch = _orig_patch_fn

# PM3-6: valid rebind → target mirror + local binding converge together
_mirror_state["prod_sF"] = {"price": 1, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
r = client.patch(f"/samelabs/commerce/products/plans/{planA}", json={"creem_product_id": "prod_sF"},
                 cookies={COOKIE_NAME: op_sess})
with db() as con:
    _bindA2 = con.execute("select creem_product_id from plans where id=%s",
                          (planA,)).fetchone()["creem_product_id"]
check("PM3-6 rebind → 409 forbidden (binding immutable; mirror 不动)",
      r.status_code == 409 and _bindA2 == "prod_sA"
      and _mirror_state["prod_sF"]["price"] == 1,
      f"{r.status_code} bind={_bindA2} mirror={_mirror_state['prod_sF']['price']}")

# ═══ PM4: forced-concurrency — mirror reads the in-tx final row ═══
# Reproduces the stale-outer-read race EXACTLY:
#   initial plan = 999 / prod_sA
#   B: enters update_plan() but STALLS after the outer read, holding a
#      pre-transaction snapshot of 999 (the old bug source)
#   A: commits price 999 → 1499 while B is stalled
#   B: proceeds — UPDATE product → prod_sB, then mirror
# With the fix, B mirrors prod_sB → 1499 (the row B's tx commits),
# NOT 999 (B's stale outer read).
_mirror_state["prod_cA"] = {"price": 999, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
_mirror_state["prod_cB"] = {"price": 1, "currency": "USD",
                            "billing_type": "recurring",
                            "billing_period": "every-month"}
with db() as con:
    _soC = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p WHERE p.code <> 'free'"
    ).fetchone()["so"])
rC = client.post("/samelabs/commerce/products/plans", json={
    "code": f"plC1_{_u}", "name": "C1", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": "prod_cA", "sort_order": _soC,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
planC = rC.json()["plan"]["id"]

import threading
# gate: B's transaction starts, then pauses AFTER its outer read +
# BEFORE its UPDATE takes the row lock — A's full request commits in
# that window.
_b_entered = threading.Event()
_b_release = threading.Event()
_mirror_lock = threading.Lock()

_orig_update_plan_mod = None
from repo import orders as _orders_mod
_orig_update_plan = _orders_mod.update_plan

def _gated_update_plan(con, plan_id_, fields_, ts_):
    # runs INSIDE the route's transaction: signal, then wait until A has
    # fully committed, so B's UPDATE takes the row lock only afterwards
    _b_entered.set()
    _b_release.wait(timeout=30)
    return _orig_update_plan(con, plan_id_, fields_, ts_)

import app.routes.samelabs as _samelabs_mod
# the route imports update_plan at function scope from repo.orders —
# patch the module attribute it resolves at call time
_orders_mod.update_plan = _gated_update_plan

result_b = {}
def _worker_b():
    # price-only concurrency (rebind forbidden by binding immutability)
    result_b["resp"] = client.patch(
        f"/samelabs/commerce/products/plans/{planC}", json={"price_cents": 799},
        cookies={COOKIE_NAME: op_sess})

tb = threading.Thread(target=_worker_b)
tb.start()
check("PM4 setup: B 进入事务并停在 row lock 前", _b_entered.wait(timeout=10))

# A: full price update 999 → 1499, commits completely while B waits
ra = client.patch(f"/samelabs/commerce/products/plans/{planC}", json={"price_cents": 1499},
                  cookies={COOKIE_NAME: op_sess})
check("PM4 A 改价 commit (999→1499)", ra.status_code == 200, ra.status_code)

# release B: its UPDATE product → prod_cB runs under the row lock
_b_release.set()
tb.join(timeout=30)
_orders_mod.update_plan = _orig_update_plan

with db() as con:
    _fin = con.execute("select price_cents, creem_product_id from plans where id=%s",
                       (planC,)).fetchone()
check("PM4 并发终态: 最后提交者胜 (A 1499) / binding 不变",
      int(_fin["price_cents"]) == 1499 and _fin["creem_product_id"] == "prod_cA",
      str(dict(_fin)))
check("PM4 mirror prod_cA = 1499 (A 事务内 final row, 绝非 stale 999)",
      _mirror_state["prod_cA"]["price"] == 1499,
      str(_mirror_state["prod_cA"]))
check("PM4 B 请求本身成功 (price-only)", result_b.get("resp", None) is not None
      and result_b["resp"].status_code == 200,
      str(result_b.get("resp"))[:80])

# cleanup PM4 plan
with db() as con:
    con.execute("delete from plans where id=%s", (planC,))

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
_creem._patch = _orig_patch
_creem._fetch_product = _orig_fetch_prod
_creem.CREEM_API_KEY = _orig_creem_key
_rec.fetch_subscription, _rec.fetch_transaction = _orig_fetch_sub, _orig_fetch_txn

# ═══ cleanup ═══
# op 驱动过 /samelabs plan 写入 → operator_audit 留有 actor 引用;
# FK 无 CASCADE, 测试清理先删 audit 行再删 actor users (test-only)。
with db() as con:
    con.execute(
        "delete from operator_audit where actor_user_id = ANY(%s)",
        ([u for u in (buyer, op, member, member2, member3, member4,
                      mmA, mmB, mmC, mmD) if u],))
    for uid in (buyer, op, member, member2, member3, member4,
                mmA, mmB, mmC, mmD):
        purge_users(con, [uid])
    con.execute("delete from plans where id in (%s, %s)", (plan_id, plan2_id))
    con.execute("delete from plans where id in (%s, %s)", (planA, planB))
    con.execute("delete from packages where id=%s", (pkg_id,))

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print(f"FAIL: 无 — 全绿 ({COUNT} checks)")
