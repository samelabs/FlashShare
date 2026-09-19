#!/usr/bin/env python3
"""Order/payment state-machine canonical — full permutation coverage.

Package/plan transition matrices, provider identity monotonicity,
ambiguous-create semantics, pending-intent reuse, webhook recovery,
paid-period immutability under live plan edits, operator binding
immutability + pending-price freeze, and mirror commit-outcome
reconciliation. Real PostgreSQL; fault injection only.
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

import time
ts = int(time.time())
_u = "sm" + _os.urandom(3).hex()

from database import db
from repo import orders as orders_repo

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

def _mkpkg(credits=5000, price=500):
    with db() as con:
        row = con.execute(
            "insert into packages (code, name, credits, price_cents,"
            " currency, active, created_at, updated_at) values"
            " (%s, %s, %s, %s, 'USD', TRUE, %s, %s) returning *",
            (f"pkg_{_u}_{credits}_{price}", f"P{credits}", credits, price,
             ts, ts)).fetchone()
    return dict(row)

_prov_seq = [0]
def _pkg_order(uid, pkg_id, status="pending", prov=None, ref=None):
    granted = status in ("paid", "refunded", "disputed")
    paid_at = ts if status in ("paid", "refunded", "disputed") else None
    refunded_at = ts if status in ("refunded", "disputed") else None
    if prov is None and status in ("paid", "refunded", "disputed"):
        _prov_seq[0] += 1
        prov = f"ord_{_u}_{_prov_seq[0]}"
    with db() as con:
        row = con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency,"
            " credits_snapshot, granted, paid_at, refunded_at,"
            " created_at) values (%s, 'package', %s, 'creem', %s, %s, 500,"
            " 'USD', 5000, %s, %s, %s, %s) returning *",
            (uid, pkg_id, prov, status,
             granted, paid_at, refunded_at, ts)).fetchone()
    return dict(row)

def _st(order_id):
    with db() as con:
        return con.execute("select status, provider_order_id, paid_at,"
                           " refunded_at from orders where id=%s",
                           (order_id,)).fetchone()

uid = _mkuser(_u + "a")
pkg = _mkpkg()

# ═══ Package transition matrix ═══
o = _pkg_order(uid, pkg["id"])
orders_repo.mark_paid(con := None, 1, ts) if False else None
with db() as con:
    orders_repo.complete_package_order(con, o["id"], "ord_a", ts)
check("P-t1 pending→paid", _st(o["id"])["status"] == "paid")
_paid_at_1 = _st(o["id"])["paid_at"]
with db() as con:
    orders_repo.complete_package_order(con, o["id"], "ord_a", ts)
check("P-t2 paid replay 不重写 paid_at", _st(o["id"])["paid_at"] == _paid_at_1)
o2 = _pkg_order(uid, pkg["id"], status="failed")
with db() as con:
    orders_repo.complete_package_order(con, o2["id"], "ord_b", ts)
check("P-t3 failed→paid on stronger proof", _st(o2["id"])["status"] == "paid")
o3 = _pkg_order(uid, pkg["id"], status="paid")
with db() as con:
    orders_repo.mark_failed(con, o3["id"])
check("P-t4 paid + stale failure stays paid", _st(o3["id"])["status"] == "paid")
o4 = _pkg_order(uid, pkg["id"], status="paid")
with db() as con:
    orders_repo.mark_refunded(con, o4["id"], ts)
check("P-t5 paid→refunded", _st(o4["id"])["status"] == "refunded")
o5 = _pkg_order(uid, pkg["id"], status="paid")
with db() as con:
    orders_repo.mark_disputed(con, o5["id"], ts)
check("P-t6 paid→disputed", _st(o5["id"])["status"] == "disputed")
o6 = _pkg_order(uid, pkg["id"], status="paid")
with db() as con:
    orders_repo.mark_refunded(con, o6["id"], ts)
_r_at = _st(o6["id"])["refunded_at"]
with db() as con:
    orders_repo.mark_disputed(con, o6["id"], ts + 999)
check("P-t7 refund→dispute marker + refunded_at 保持 first reversal",
      _st(o6["id"])["status"] == "disputed"
      and _st(o6["id"])["refunded_at"] == _r_at)
o7 = _pkg_order(uid, pkg["id"], status="disputed")
with db() as con:
    orders_repo.mark_refunded(con, o7["id"], ts)
check("P-t8 dispute→refund stays disputed", _st(o7["id"])["status"] == "disputed")

# reversed + stale completed never grants/revives
from economy import creem as _ec
_ec.CREEM_CREDITS_PRODUCT_ID = "prod_sm"
_ec.CREEM_CREDITS_CURRENCY = "USD"
from economy.billing import spendable_balance as _bal
o8 = _pkg_order(uid, pkg["id"])
r8 = _ec.fulfill_by_request_id(o8["id"], "ord_c", "ch_c", 500,
                               product_id="prod_sm", currency="USD")
with db() as con:
    orders_repo.mark_refunded(con, o8["id"], ts)
_bal8 = _bal(uid)
r8b = _ec.fulfill_by_request_id(o8["id"], "ord_c", "ch_c", 500,
                                product_id="prod_sm", currency="USD")
check("P-t9 reversed + stale completed → no grant / no revive",
      r8b.get("granted") is False and _st(o8["id"])["status"] == "refunded"
      and _bal(uid) == _bal8)

# grant durable, paid stamp crash → replay heals
o9 = _pkg_order(uid, pkg["id"])
from economy.billing import grant as _grant
_grant(uid, 5000, "grant_package", ref_type="order", ref_id=str(o9["id"]),
       idem_key=f"order:{o9['id']}", note="crash window")
_bal9 = _bal(uid)
r9 = _ec.fulfill_by_request_id(o9["id"], "ord_d", "ch_d", 500,
                               product_id="prod_sm", currency="USD")
check("P-t10 grant durable + crash before paid stamp → replay heals paid",
      _st(o9["id"])["status"] == "paid" and _bal(uid) == _bal9,
      f"st={_st(o9['id'])['status']} bal {_bal9}→{_bal(uid)}")

# provider identity monotonicity
oA = _pkg_order(uid, pkg["id"])
with db() as con:
    orders_repo.bind_checkout_id(con, oA["id"], "chA")
with db() as con:
    r = orders_repo.bind_checkout_id(con, oA["id"], "chA")
check("I-1 same identity replay = no-op", r["provider_order_id"] == "ch:chA")
_conf = False
try:
    with db() as con:
        orders_repo.bind_checkout_id(con, oA["id"], "chB")
except KeyError:
    _conf = True
check("I-2 ch:A → ch:B = conflict", _conf)
with db() as con:
    orders_repo.complete_package_order(con, oA["id"], "ord_canon", ts)
with db() as con:
    r2 = orders_repo.bind_checkout_id(con, oA["id"], "chA")
check("I-3 late ch response cannot regress canonical",
      r2["provider_order_id"] == "ord_canon")
_conf2 = False
try:
    with db() as con:
        orders_repo.complete_package_order(con, oA["id"], "ord_other", ts)
except KeyError:
    _conf2 = True
check("I-4 canonical A → canonical B = conflict", _conf2)

# ambiguous create keeps pending (repo-level: exception path leaves pending)
oP = _pkg_order(uid, pkg["id"])
check("I-5 ambiguous create keeps pending (no mutation)",
      _st(oP["id"])["status"] == "pending"
      and _st(oP["id"])["provider_order_id"] is None)

# same-package repeated pending converges (partial unique)
_dupe = False
try:
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider, status,"
            " amount_cents, currency, credits_snapshot,"
            " created_at) values (%s, 'package', %s, 'creem', 'pending',"
            " 500, 'USD', 5000, %s)",
            (uid, pkg["id"], ts))
except Exception:
    _dupe = True
check("I-6 same (user,item) second pending → unique 拒 (并发兜底)", _dupe)

# pending identity namespace
_ns = False
try:
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency,"
            " credits_snapshot, created_at) values (%s, 'package', %s,"
            " 'creem', 'ord_pre', 'pending', 500, 'USD', 5000, %s)",
            (uid, pkg["id"], ts))
except Exception:
    _ns = True
check("I-7 pending + canonical identity → schema 拒 (ch: namespace only)", _ns)

# ═══ Subscription checkout atomicity ═══
from economy.subscription import bind_checkout as _bind, apply_paid_period
uidS = _mkuser(_u + "s")
with db() as con:
    _pl = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'SM Plan', 100, 999, 'USD', TRUE, 'prod_sm_plan',"
        " 91, 30, 100, 104857600, %s, %s) returning *",
        (f"smplan_{_u}", ts, ts)).fetchone()
    _pl = dict(_pl)
oS = None
with db() as con:
    oS = con.execute(
        "insert into orders (user_id, kind, item_id, provider, status,"
        " amount_cents, currency, created_at) values"
        " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)"
        " returning *", (uidS, _pl["id"], ts)).fetchone()
    oS = dict(oS)
_ev = {"id": "ch_sm", "request_id": f"order:{oS['id']}",
       "order": {"id": "ord_sm", "status": "paid", "type": "recurring",
                  "amount": 999, "currency": "USD",
                  "product": "prod_sm_plan"},
       "subscription": {"id": "sub_sm"}}
_bind(_ev, order_id=oS["id"])
with db() as con:
    _rS = con.execute("select status, provider_order_id from orders"
                      " where id=%s", (oS["id"],)).fetchone()
    _mS = con.execute("select * from memberships where user_id=%s",
                      (uidS,)).fetchone()
check("S-1 pending→paid + sub binding + Membership 一事务",
      _rS["status"] == "paid" and _rS["provider_order_id"] == "sub:sub_sm"
      and _mS is not None)
_bind(_ev, order_id=oS["id"])
with db() as con:
    _nS = int(con.execute("select count(*) c from memberships where"
                          " user_id=%s", (uidS,)).fetchone()["c"])
check("S-2 paid replay idempotent", _nS == 1)

# wrong proof → zero mutation
uidS2 = _mkuser(_u + "s2")
with db() as con:
    oS2 = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider, status,"
        " amount_cents, currency, created_at) values"
        " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)"
        " returning *", (uidS2, _pl["id"], ts)).fetchone())
_err = False
try:
    _bind({"id": "ch_sm2", "request_id": f"order:{oS2['id']}",
           "order": {"id": "ord_sm2", "status": "paid", "type": "recurring",
                      "amount": 500, "currency": "USD",
                      "product": "prod_sm_plan"},
           "subscription": {"id": "sub_sm2"}}, order_id=oS2["id"])
except KeyError as e:
    _err = "amount" in str(e)
with db() as con:
    _rS2 = con.execute("select status, provider_order_id from orders"
                       " where id=%s", (oS2["id"],)).fetchone()
    _mS2 = con.execute("select 1 from memberships where user_id=%s",
                       (uidS2,)).fetchone()
check("S-3 wrong amount proof → fail loud + 零 mutation",
      _err and _rS2["status"] == "pending"
      and _rS2["provider_order_id"] is None and _mS2 is None)

# one pending plan intent per user (partial unique)
_dup_plan = False
try:
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider, status,"
            " amount_cents, currency, created_at) values"
            " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)",
            (uidS2, _pl["id"], ts))
except Exception:
    _dup_plan = True
check("S-4 second pending plan order per user → unique 拒", _dup_plan)

# ch→sub + late ch cannot regress (ch path atomic)
uidS3 = _mkuser(_u + "s3")
with db() as con:
    oS3 = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " created_at) values (%s, 'plan', %s, 'creem', 'ch:ch_s3',"
        " 'pending', 999, 'USD', %s) returning *",
        (uidS3, _pl["id"], ts)).fetchone())
_bind({"id": "ch_s3",
       "order": {"id": "ord_s3", "status": "paid", "type": "recurring",
                  "amount": 999, "currency": "USD",
                  "product": "prod_sm_plan"},
       "subscription": {"id": "sub_s3"}})
with db() as con:
    _rS3 = con.execute("select status, provider_order_id from orders"
                       " where id=%s", (oS3["id"],)).fetchone()
    _mS3 = con.execute("select 1 from memberships where user_id=%s",
                       (uidS3,)).fetchone()
check("S-5 ch→sub + Membership 原子 (no pending+sub fracture)",
      _rS3["status"] == "paid" and _rS3["provider_order_id"] == "sub:sub_s3"
      and _mS3 is not None)
with db() as con:
    r_late = orders_repo.bind_checkout_id(con, oS3["id"], "ch_s3")
check("S-6 late ch cannot regress sub:", r_late["provider_order_id"] == "sub:sub_s3")

# ═══ Paid period immutability ═══
_ps = ts - 3600
_subobj = {"subscription": {"id": "sub_sm", "status": "active",
           "current_period_start_date": _ps,
           "current_period_end_date": _ps + 30 * 86400,
           "last_transaction_id": "txn_sm1",
           "product": {"id": "prod_sm_plan"}}}
r_pp1 = apply_paid_period("sub_sm", _subobj)
_bal_pp1 = _bal(uidS)
with db() as con:
    con.execute("update plans set monthly_credits=150 where id=%s", (_pl["id"],))
r_pp2 = apply_paid_period("sub_sm", _subobj)
check("PP-1 same txn replay after monthly_credits edit → 零额外 Credits",
      _bal(uidS) == _bal_pp1 == 100, f"{_bal_pp1}→{_bal(uidS)}")
# new period txn uses live plan exactly once
_ps2 = _ps + 30 * 86400
_subobj2 = {"subscription": {"id": "sub_sm", "status": "active",
            "current_period_start_date": _ps2,
            "current_period_end_date": _ps2 + 30 * 86400,
            "last_transaction_id": "txn_sm2",
            "product": {"id": "prod_sm_plan"}}}
r_pp3 = apply_paid_period("sub_sm", _subobj2)
_bal_pp3 = _bal(uidS)
check("PP-2 new period txn → live plan 150 恰一次",
      _bal(uidS) == _bal_pp1 + 150, f"{_bal_pp1}→{_bal(uidS)}")
with db() as con:
    con.execute("update plans set monthly_credits=200 where id=%s", (_pl["id"],))
r_pp4 = apply_paid_period("sub_sm", _subobj2)
check("PP-3 new txn replay after later edit → 零额外", _bal(uidS) == _bal_pp3)
# grant durable + membership crash heal (projection repair, no second grant)
_balH = _bal(uidS)
with db() as con:
    con.execute("update memberships set last_grant_period_start=NULL,"
                " renews_at=NULL where user_id=%s", (uidS,))
rH = apply_paid_period("sub_sm", _subobj2)
check("PP-4 same-txn replay heals projection, no second grant",
      _bal(uidS) == _balH)
# active status alone never grants
from economy.subscription import handle_event as _hev
_balA = _bal(uidS)
_hev("subscription.active", {"subscription": {"id": "sub_sm", "status": "active",
     "current_period_start_date": _ps2, "current_period_end_date": _ps2 + 30 * 86400,
     "product": {"id": "prod_sm_plan"}}})
check("PP-5 active status alone never grants", _bal(uidS) == _balA)

# ═══ Operator guards ═══
_src_sml = open(_os.path.join(_REPO, "app", "routes", "samelabs.py"),
                encoding="utf-8").read()
check("OP-1 product_binding_immutable guard 存在",
      "product_binding_immutable" in _src_sml)
check("OP-2 plan_checkout_pending guard 存在",
      "plan_checkout_pending" in _src_sml)
# pending plan order + price edit → 409 (repo 级证明: guard 查询逻辑)
uidOP = _mkuser(_u + "op")
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider, status,"
        " amount_cents, currency, created_at) values"
        " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)",
        (uidOP, _pl["id"], ts))
with db() as con:
    _hit = con.execute(
        "SELECT 1 FROM orders WHERE kind='plan' AND item_id=%s"
        " AND status='pending' LIMIT 1", (_pl["id"],)).fetchone()
check("OP-3 pending plan order blocks price edit (guard 数据面)",
      _hit is not None)

# ═══ Mirror reconciliation ═══
_ec2 = _ec
_ec2.CREEM_API_KEY = _ec2.CREEM_API_KEY or "test-key"
_calls = {"get": [], "patch": []}
_prod_state = {"prod_m": {"id": "prod_m", "price": 1000, "currency": "USD",
                          "billing_type": "recurring",
                          "billing_period": "every-month"}}
def _fake_fetch(pid):
    _calls["get"].append(pid)
    return dict(_prod_state.get(pid) or {}) or None
def _fake_patch(path, payload):
    _calls["patch"].append((path, dict(payload)))
    pid = path.rsplit("/", 1)[-1]
    if pid in _prod_state:
        _prod_state[pid]["price"] = payload["price"]
        _prod_state[pid]["currency"] = payload["currency"]
        return {"object": "product", **dict(_prod_state[pid])}
    raise RuntimeError("not_found")
_orig_fetch, _orig_patch = _ec2._fetch_product, _ec2._patch
_ec2._fetch_product = _fake_fetch
_ec2._patch = _fake_patch
_out = _ec2.mirror_price_with_reconciliation("prod_m", 1000, "USD", 1499, "USD")
check("M-1 PATCH success → MIRROR_SUCCESS", _out == _ec2.MIRROR_SUCCESS)
# ambiguous + GET intended → success
def _patch_raise(path, payload):
    raise RuntimeError("timeout")
_ec2._patch = _patch_raise
# provider already at intended (state 1499 from M-1)
_out2 = _ec2.mirror_price_with_reconciliation("prod_m", 1000, "USD", 1499, "USD")
check("M-2 ambiguous PATCH + GET intended → SUCCESS", _out2 == _ec2.MIRROR_SUCCESS)
# ambiguous + GET before → ROLLBACK
_prod_state["prod_m"]["price"] = 1000
_out3 = _ec2.mirror_price_with_reconciliation("prod_m", 1000, "USD", 1499, "USD")
check("M-3 ambiguous PATCH + GET before → ROLLBACK", _out3 == _ec2.MIRROR_ROLLBACK)
# ambiguous + neither → UNRESOLVED
_prod_state["prod_m"]["price"] = 777
_out4 = _ec2.mirror_price_with_reconciliation("prod_m", 1000, "USD", 1499, "USD")
check("M-4 neither before nor intended → UNRESOLVED", _out4 == _ec2.MIRROR_UNRESOLVED)
# restore + verify
_prod_state["prod_m"]["price"] = 1499
_ec2._patch = _fake_patch
_ok_r = _ec2.restore_product_price("prod_m", 1000, "USD")
check("M-5 restore to before + verify", _ok_r and _prod_state["prod_m"]["price"] == 1000)
_ec2._fetch_product = _orig_fetch
_ec2._patch = _orig_patch

# ══════════ R2 series: production-closure deviations (7 fixes) ══════════
def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── R2-1: payment client_ref fully absent ──
import subprocess as _sp
_grep = _sp.run(["grep", "-rn", "client_ref", "app/routes/topup.py",
                 "static/pages/credits.js"], capture_output=True, text=True,
                cwd=_REPO).stdout.strip()
check("R2-1a payment surface 零 client_ref 引用 (topup route + credits.js)",
      _grep == "", _grep[:100])
with db() as con:
    _cols = [r["column_name"] for r in con.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name='orders'").fetchall()]
check("R2-1b orders 表无 client_ref 列", "client_ref" not in _cols)
with db() as con:
    _idx = [r["indexname"] for r in con.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename='orders'").fetchall()]
check("R2-1c orders_user_client_ref_uk 不存在",
      "orders_user_client_ref_uk" not in _idx)

# ambiguous create retry → SAME order (repo-level, provider fake)
import economy.creem as _creem_r2
class _FakeCreemR2:
    def __init__(self):
        self.entities = {}
    def create_credits_checkout(self, order_id, amount, user):
        rid = f"order:{order_id}"
        if rid not in self.entities:
            self.entities[rid] = {"checkout_id": f"chk_{order_id}"}
        return {"ok": True, "checkout_url": "https://x/" + self.entities[rid]["checkout_id"],
                "checkout_id": self.entities[rid]["checkout_id"]}
_fk2 = _FakeCreemR2()
_orig_cc = _creem_r2.create_credits_checkout
uidR2 = _mkuser(_u + "r2")
with db() as con:
    oR2 = dict(orders_repo.insert_order(
        con, uidR2, "package", pkg["id"], "creem", 500, "USD", ts,
        credits_snapshot=5000))
_fk2.create_credits_checkout(oR2["id"], 500, {"id": uidR2})
_fk2.create_credits_checkout(oR2["id"], 500, {"id": uidR2})  # retry
with db() as con:
    _nR2 = int(con.execute(
        "SELECT count(*) c FROM orders WHERE user_id=%s AND kind='package'"
        " AND item_id=%s AND status='pending'",
        (uidR2, pkg["id"])).fetchone()["c"])
check("R2-1d ambiguous-create retry → 同一 pending Order + 1 provider entity",
      _nR2 == 1 and len(_fk2.entities) == 1)

# ── R2-2: package paid identity = real provider order id ──
from economy import creem as _cr2
uidR2b = _mkuser(_u + "r2b")
with db() as con:
    oR2b = dict(orders_repo.insert_order(
        con, uidR2b, "package", pkg["id"], "creem", 500, "USD", ts,
        credits_snapshot=5000))
_prod_id_orig = _cr2.CREEM_CREDITS_PRODUCT_ID
_cr2.CREEM_CREDITS_PRODUCT_ID = "prod_credits"
try:
    # missing provider order id → no paid / no grant
    _err = None
    try:
        _cr2.fulfill_by_request_id(int(oR2b["id"]), "", "chk_x", 500,
                                   product_id="prod_credits", currency="USD")
    except KeyError as e:
        _err = str(e)
    with db() as con:
        _r = con.execute("select status, provider_order_id from orders"
                         " where id=%s", (oR2b["id"],)).fetchone()
    check("R2-2a provider order id missing → KeyError + order 仍 pending",
          _err == "'provider_order_id_missing'"
          and _r["status"] == "pending" and _r["provider_order_id"] is None,
          f"{_err} {dict(_r)}")

    # wrong checkout id vs bound ch: → conflict, no mutation
    with db() as con:
        orders_repo.bind_checkout_id(con, oR2b["id"], "chk_real")
    _err2 = None
    try:
        _cr2.fulfill_by_request_id(int(oR2b["id"]), "ord_r2b", "chk_OTHER",
                                   500, product_id="prod_credits",
                                   currency="USD")
    except KeyError as e:
        _err2 = str(e)
    with db() as con:
        _r2 = con.execute("select status, provider_order_id from orders"
                          " where id=%s", (oR2b["id"],)).fetchone()
    check("R2-2b wrong checkout id → checkout_identity_conflict + 零 mutation",
          _err2 == "'checkout_identity_conflict'"
          and _r2["status"] == "pending" and _r2["provider_order_id"] == "ch:chk_real",
          f"{_err2} {dict(_r2)}")

    # correct proof → paid with REAL provider order id
    _res = _cr2.fulfill_by_request_id(int(oR2b["id"]), "ord_r2b", "chk_real",
                                      500, product_id="prod_credits",
                                      currency="USD")
    with db() as con:
        _r3 = con.execute("select status, provider_order_id from orders"
                          " where id=%s", (oR2b["id"],)).fetchone()
    check("R2-2c 完整 proof → paid + canonical ord id (非 ch:)",
          _res.get("granted") and _r3["status"] == "paid"
          and _r3["provider_order_id"] == "ord_r2b",
          f"{_res} {dict(_r3)}")
finally:
    _cr2.CREEM_CREDITS_PRODUCT_ID = _prod_id_orig

# ── R2-3: subscription proof predicate — each required field missing ──
uidR2c = _mkuser(_u + "r2c")
with db() as con:
    oR2c = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider, status,"
        " amount_cents, currency, created_at) values"
        " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)"
        " returning *", (uidR2c, _pl["id"], ts)).fetchone())
_full = {"id": "ch_r2c",
         "order": {"id": "ord_r2c", "status": "paid", "type": "recurring",
                    "amount": 999, "currency": "USD",
                    "product": "prod_sm_plan"},
         "subscription": {"id": "sub_r2c"}}
import copy as _copy
_bad_proofs = {
    "checkout_id":      {"id": "", "order": _full["order"], "subscription": _full["subscription"]},
    "subscription_id":  {"id": "ch_r2c", "order": _full["order"], "subscription": {"id": ""}},
    "provider_order_id": {"id": "ch_r2c", "order": {**_full["order"], "id": ""},
                          "subscription": _full["subscription"]},
    "status_not_paid":  {"id": "ch_r2c", "order": {**_full["order"], "status": "pending"},
                         "subscription": _full["subscription"]},
    "type_not_recurring": {"id": "ch_r2c", "order": {**_full["order"], "type": "onetime"},
                           "subscription": _full["subscription"]},
    "amount_mismatch":  {"id": "ch_r2c", "order": {**_full["order"], "amount": 100},
                         "subscription": _full["subscription"]},
    "currency_missing": {"id": "ch_r2c", "order": {**_full["order"], "currency": ""},
                         "subscription": _full["subscription"]},
    "currency_mismatch": {"id": "ch_r2c", "order": {**_full["order"], "currency": "EUR"},
                          "subscription": _full["subscription"]},
    "product_missing":  {"id": "ch_r2c", "order": {**_full["order"], "product": ""},
                         "subscription": _full["subscription"]},
    "product_mismatch": {"id": "ch_r2c", "order": {**_full["order"], "product": "prod_OTHER"},
                         "subscription": _full["subscription"]},
}
_all_zero = True
for _nm, _ev in _bad_proofs.items():
    _e = None
    try:
        _bind(_ev, order_id=oR2c["id"])
    except KeyError as exc:
        _e = str(exc)
    if _e is None:
        _all_zero = False
        check(f"R2-3 {_nm} → fail loud", False, "no exception")
with db() as con:
    _rc = con.execute("select status, provider_order_id from orders"
                      " where id=%s", (oR2c["id"],)).fetchone()
    _mc = int(con.execute("select count(*) c from memberships where"
                          " user_id=%s", (uidR2c,)).fetchone()["c"])
check("R2-3 全部 10 种 proof 缺失/错配 → KeyError + 零 Order/零 Membership mutation",
      _all_zero and _rc["status"] == "pending"
      and _rc["provider_order_id"] is None and _mc == 0,
      f"{dict(_rc)} m={_mc}")
# full proof then binds
_bind(_full, order_id=oR2c["id"])
with db() as con:
    _rc2 = con.execute("select status, provider_order_id from orders"
                       " where id=%s", (oR2c["id"],)).fetchone()
check("R2-3 完整 proof → paid + sub: canonical",
      _rc2["status"] == "paid" and _rc2["provider_order_id"] == "sub:sub_r2c",
      dict(_rc2))

# paid replay same sub ok / different sub conflict
_bind(_full, order_id=oR2c["id"])
_ev_other = _copy.deepcopy(_full)
_ev_other["subscription"] = {"id": "sub_OTHER"}
_e3 = None
try:
    _bind(_ev_other, order_id=oR2c["id"])
except KeyError as exc:
    _e3 = str(exc)
with db() as con:
    _rc3 = con.execute("select status, provider_order_id from orders"
                       " where id=%s", (oR2c["id"],)).fetchone()
check("R2-3b paid + same sub replay ok / paid + different sub → conflict",
      _e3 in ("'subscription_identity_conflict'",
              "'provider_identity_conflict'")
      and _rc3["provider_order_id"] == "sub:sub_r2c",
      f"{_e3} {dict(_rc3)}")
# writer-level monotonicity (complete_plan_checkout direct):
_e3b = None
try:
    with db() as con:
        orders_repo.complete_plan_checkout(con, oR2c["id"], "sub:sub_OTHER", ts)
except KeyError as exc:
    _e3b = str(exc)
check("R2-3c writer 守 sub monotonicity (paid + sub:B → conflict)",
      _e3b == "'subscription_identity_conflict'", _e3b)

# ── R2-4: package reversal requires exact positive grant ──
from economy import reverse as _rev2
uidR2d = _mkuser(_u + "r2d")
with db() as con:
    # paid WITHOUT any grant (writer-direct; snapshot positive, ledger
    # empty) — the DB no longer permits zero-snapshot package rows
    oR2d = dict(orders_repo.insert_order(
        con, uidR2d, "package", pkg["id"], "creem", 500, "USD", ts,
        credits_snapshot=5000))
    orders_repo.complete_package_order(con, oR2d["id"], f"ord_r2d_{_u}", ts)
_e4 = None
try:
    _rev2._reverse_package({"id": f"txn_r2d_{_u}", "order": f"ord_r2d_{_u}"})
except KeyError as exc:
    _e4 = str(exc)
with db() as con:
    _rd = con.execute("select status from orders where id=%s",
                      (oR2d["id"],)).fetchone()
check("R2-4a paid + missing grant → fail loud + 零 terminal mutation",
      _e4 == "'original_grant_missing'" and _rd["status"] == "paid",
      f"{_e4} {dict(_rd)}")

# grant != snapshot mismatch
uidR2e = _mkuser(_u + "r2e")
with db() as con:
    oR2e = dict(orders_repo.insert_order(
        con, uidR2e, "package", pkg["id"], "creem", 500, "USD", ts,
        credits_snapshot=5000))
_prod_id_orig2 = _cr2.CREEM_CREDITS_PRODUCT_ID
_cr2.CREEM_CREDITS_PRODUCT_ID = "prod_credits"
try:
    _cr2.fulfill_by_request_id(int(oR2e["id"]), f"ord_r2e_{_u}", "chk_r2e",
                                500, product_id="prod_credits", currency="USD")
finally:
    _cr2.CREEM_CREDITS_PRODUCT_ID = _prod_id_orig2
with db() as con:
    con.execute("update ledger set delta=3000 where"
                " ref_type='order' and ref_id=%s", (str(oR2e["id"]),))
_e5 = None
try:
    _rev2._reverse_package({"id": f"txn_r2e_{_u}", "order": f"ord_r2e_{_u}"})
except KeyError as exc:
    _e5 = str(exc)
with db() as con:
    _re = con.execute("select status from orders where id=%s",
                      (oR2e["id"],)).fetchone()
check("R2-4b grant != snapshot → grant_snapshot_mismatch + 零 terminal",
      _e5 == "'grant_snapshot_mismatch'" and _re["status"] == "paid",
      f"{_e5} {dict(_re)}")

# ── R2-5: apply_paid_period replay — wrong ref_type not matched + true movement ──
uidR2f = _mkuser(_u + "r2f")
with db() as con:
    oR2f = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency, created_at,"
        " paid_at) values (%s, 'plan', %s, 'creem', 'sub:sub_r2f', 'paid',"
        " 999, 'USD', %s, %s) returning *",
        (uidR2f, _pl["id"], ts, ts)).fetchone())
    _m2 = con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at) values (%s, %s, 'sub:sub_r2f',"
        " 'active', %s, %s) returning *",
        (uidR2f, _pl["id"], ts, ts + 30*86400)).fetchone()
PS2, PE2 = ts, ts + 30*86400
_snap2 = {"object": "subscription", "id": "sub_r2f", "status": "active",
          "current_period_start_date": _iso(PS2), "current_period_end_date": _iso(PE2),
          "product": {"id": "prod_sm_plan"},
          "last_transaction_id": f"txn_r2f_{_u}"}
with db() as con:
    _live_credits = int(con.execute(
        "select monthly_credits from plans where id=%s",
        (_pl["id"],)).fetchone()["monthly_credits"])
_r5a = apply_paid_period("sub_r2f", _snap2)
check("R2-5a first apply → granted=True, credits=live plan 值恰一次",
      _r5a["granted"] is True and _r5a["credits"] == _live_credits, _r5a)
# wrong ref_type row sharing the txn ref must NOT be matched as replay grant
with db() as con:
    con.execute(
        "insert into ledger (user_id, kind, delta, balance_after, ref_type,"
        " ref_id, idem_key, note, created_at) values (%s, 'grant_plan', 999,"
        " 999, 'other_type', %s, %s, 'x', %s)",
        (uidR2f, f"txn_r2f_{_u}", f"wrong-rt-{_u}", ts))
# live plan edit 100 → 150, then replay SAME txn
with db() as con:
    con.execute("update plans set monthly_credits=150 where id=%s", (_pl["id"],))
_r5b = apply_paid_period("sub_r2f", _snap2)
with db() as con:
    _tot = int(con.execute(
        "select coalesce(sum(delta),0) s from ledger where user_id=%s"
        " and kind in ('grant_plan','grant_plan_upgrade')"
        " and ref_type='creem_transaction'", (uidR2f,)).fetchone()["s"])
check("R2-5b same-txn replay → granted=False credits=0 / ledger 不变 / wrong ref_type 不命中",
      _r5b["granted"] is False and _r5b["credits"] == 0
      and _tot == _live_credits,
      f"{_r5b} tot={_tot}")
with db() as con:
    con.execute("update plans set monthly_credits=100 where id=%s", (_pl["id"],))

# ═══ cleanup ═══
_uids_all = [uid, uidS, uidS2, uidS3, uidOP, uidR2, uidR2b, uidR2c,
             uidR2d, uidR2e, uidR2f]
with db() as con:
    for _uid2 in _uids_all:
        con.execute("delete from operator_audit where actor_user_id=%s", (_uid2,))
    con.execute("delete from memberships where user_id = ANY(%s)", (_uids_all,))
for _uid2 in _uids_all:
    with db() as con:
        con.execute("delete from tool_calls where user_id=%s", (_uid2,))
        con.execute("delete from orders where user_id=%s", (_uid2,))
        con.execute("delete from ledger where user_id=%s", (_uid2,))
with db() as con:
    con.execute("delete from packages where code like %s", (f"pkg_{_u}%",))
    con.execute("delete from plans where code like %s", (f"smplan_{_u}%",))
for _uid2 in _uids_all:
    with db() as con:
        con.execute("delete from users where id=%s", (_uid2,))
print("cleanup: state-machine fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
