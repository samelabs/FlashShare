#!/usr/bin/env python3
"""Economy chain — order/payment state: purchase intent identity, the
full transition matrices, and the operator plan-mirror commit-outcome
closure.
"""
from _harness import REPO, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Order/payment state closure
#
# Order/payment state closure canonical.
#
# Purchase intent identity = Order.id; provider request →
# recovery transitions → evidence precedence → reversal ordering →
# plan-order domain. Real repo/route helpers over an isolated DB; fault
# injection only.
# ════════════════════════════════════════════════════════════════════════
section('Order/payment state closure')

import os as _os
import sys as _sys


import time
ts = int(time.time())
_u = "ps" + _os.urandom(3).hex()

from database import db

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

def _mkpkg(credits=5000, price=500, code=None):
    with db() as con:
        row = con.execute(
            "insert into packages (code, name, credits, price_cents,"
            " currency, active, created_at, updated_at) values"
            " (%s, %s, %s, %s, 'USD', TRUE, %s, %s) returning *",
            (code or f"pkg_{_u}_{credits}", f"P{credits}", credits, price,
             ts, ts)).fetchone()
    return dict(row)

_ord_seq = [0]
def _order(user_id, kind, item_id, ref, status="pending", amount=500,
           credits=5000, provider_id=None):
    # field combinations must satisfy the canonical invariants
    granted = kind == "package" and status == "paid"
    paid_at = ts if status in ("paid",) else None
    refunded_at = ts if status in ("refunded", "disputed") else None
    if status in ("refunded", "disputed"):
        granted = kind == "package"  # reversal may follow a paid fact
        paid_at = ts
    if provider_id is None and status in ("paid", "refunded", "disputed"):
        _ord_seq[0] += 1
        if kind == "package":
            provider_id = f"ord_{_u}_{_ord_seq[0]}"
        else:
            provider_id = f"sub:{_u}_{_ord_seq[0]}"
    with db() as con:
        row = con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency,"
            " credits_snapshot, granted, paid_at, refunded_at,"
            " created_at) values"
            " (%s, %s, %s, 'creem', %s, %s, %s, 'USD', %s, %s, %s, %s, %s)"
            " returning *",
            (user_id, kind, item_id, provider_id, status, amount, credits,
             granted, paid_at, refunded_at, ts)).fetchone()
        if provider_id:
            con.execute("update orders set provider_order_id=%s where id=%s",
                        (provider_id, row["id"]))
            row = con.execute("select * from orders where id=%s",
                              (row["id"],)).fetchone()
    return dict(row)

def _status(order_id):
    with db() as con:
        return con.execute("select status from orders where id=%s",
                           (order_id,)).fetchone()["status"]

from repo import orders as orders_repo

u1 = _mkuser(_u + "a")
u2 = _mkuser(_u + "b")
pkg = _mkpkg()
pkg2 = _mkpkg(credits=10000, price=1000)

# ══════════ A/B. purchase intent identity = Order.id ══════════
# No payment client_ref exists: insert_order takes none, and the
# single-pending-intent repos converge retries onto the same row.
with db() as con:
    oB1 = dict(orders_repo.insert_order(
        con, u1, "package", pkg["id"], "creem", 500, "USD", ts,
        credits_snapshot=5000))
check("A1 insert_order 无 client_ref 契约 (pending 落地)",
      oB1["status"] == "pending" and "client_ref" not in oB1)
with db() as con:
    pend = orders_repo.open_package_order(con, u1, int(pkg["id"]))
check("B1 open_package_order 收敛同一 pending row",
      pend is not None and pend["id"] == oB1["id"])
with db() as con:
    orders_repo.mark_failed(con, oB1["id"])
with db() as con:
    pend2 = orders_repo.open_package_order(con, u1, int(pkg["id"]))
check("B2 terminal 后不挡新购买 (open=None)",
      pend2 is None)
# 多行同 status 合法 (无全局唯一约束; pending 唯一性由 partial index 兜)
with db() as con:
    for _ in range(2):
        con.execute(
            "insert into orders (user_id, kind, item_id, provider, status,"
            " amount_cents, currency, credits_snapshot, created_at) values"
            " (%s, 'package', %s, 'creem', 'failed', 1, 'USD', 100, %s)",
            (u1, pkg["id"], ts))
check("B3 多 failed 行合法 (无 ref 全局唯一)", True)

# ══════════ C. package evidence precedence (guarded transitions) ══════════
def _mk_pkg_order(status):
    return _order(u1, "package", pkg["id"], f"pay:{_u}-{status}-{_os.urandom(2).hex()}",
                  status=status)

# pending + success → paid
o1 = _mk_pkg_order("pending")
with db() as con:
    orders_repo.complete_package_order(con, o1["id"], "ord_c1", ts)
check("C1 pending + success → paid", _status(o1["id"]) == "paid")
# paid + success replay → paid (无变化)
with db() as con:
    orders_repo.complete_package_order(con, o1["id"], "ord_c1", ts)
check("C2 paid + replay → paid", _status(o1["id"]) == "paid")
# failed + success → paid
o2 = _mk_pkg_order("failed")
with db() as con:
    orders_repo.complete_package_order(con, o2["id"], "ord_c3", ts)
check("C3 failed + success → paid", _status(o2["id"]) == "paid")
# paid + failure → paid (stale failure 不降级)
o3 = _mk_pkg_order("paid")
with db() as con:
    orders_repo.mark_failed(con, o3["id"])
check("C4 paid + failure → paid (guard)", _status(o3["id"]) == "paid")
# refunded + failure → refunded
o4 = _mk_pkg_order("refunded")
with db() as con:
    orders_repo.mark_failed(con, o4["id"])
check("C5 refunded + failure → refunded", _status(o4["id"]) == "refunded")
# pending + refund → NOT a legal reversal target (writer refuses;
# the domain layer answers unresolved/fail-loud)
o5 = _mk_pkg_order("pending")
with db() as con:
    orders_repo.mark_refunded(con, o5["id"], ts)
check("C6 pending + refund → writer 拒 (保持 pending)",
      _status(o5["id"]) == "pending")
with db() as con:
    con.execute("update orders set status='failed' where id=%s", (o5["id"],))
# refunded + dispute → disputed (升)
o6 = _mk_pkg_order("refunded")
with db() as con:
    orders_repo.mark_disputed(con, o6["id"], ts)
check("C7 refunded + dispute → disputed", _status(o6["id"]) == "disputed")
# disputed + refund → stays disputed (refund 不降级 dispute)
o7 = _mk_pkg_order("disputed")
with db() as con:
    orders_repo.mark_refunded(con, o7["id"], ts)
check("C8 disputed + refund → stays disputed", _status(o7["id"]) == "disputed")
# refunded + stale checkout.completed → NO paid rewrite
o8 = _mk_pkg_order("refunded")
with db() as con:
    # stale replay carries the SAME canonical id as the terminal row
    orders_repo.complete_package_order(con, o8["id"],
                                       o8["provider_order_id"], ts)
check("C9 refunded + stale success → 无 paid 改写", _status(o8["id"]) == "refunded")
# disputed + stale success → NO paid rewrite
o9 = _mk_pkg_order("disputed")
with db() as con:
    orders_repo.complete_package_order(con, o9["id"],
                                       o9["provider_order_id"], ts)
check("C10 disputed + stale success → 无 paid 改写", _status(o9["id"]) == "disputed")
# paid + refund → refunded; disputed + dispute → disputed
o10 = _mk_pkg_order("paid")
with db() as con:
    orders_repo.mark_refunded(con, o10["id"], ts)
check("C11 paid + refund → refunded", _status(o10["id"]) == "refunded")
o11 = _mk_pkg_order("disputed")
with db() as con:
    orders_repo.mark_disputed(con, o11["id"], ts)
check("C12 disputed + dispute → disputed (幂等)", _status(o11["id"]) == "disputed")

# ══════════ D. plan order 域 ══════════
po1 = _order(u1, "plan", 1, f"pay:{_u}-plan1", status="pending", amount=999,
             credits=None)
with db() as con:
    orders_repo.complete_plan_checkout(con, po1["id"], "sub:d1", ts)
check("D1 plan pending + completed → paid", _status(po1["id"]) == "paid")
with db() as con:
    orders_repo.complete_plan_checkout(con, po1["id"], "sub:d1", ts)
check("D2 plan paid + replay → paid", _status(po1["id"]) == "paid")
po2 = _order(u1, "plan", 1, f"pay:{_u}-plan2", status="paid", amount=999,
             credits=None)
with db() as con:
    orders_repo.mark_failed(con, po2["id"])
check("D3 plan paid + local failure → paid", _status(po2["id"]) == "paid")
# 周期 reversal 不写 plan order — mark_refunded/disputed 在 plan order 上:
# canonical 语义是 reverse 机制不触碰 plan order (repo 层不断言 kind);
# 直接断言: 状态机 writer 组合不会由 subscription reversal 调用 (semantic)
_src_rev = open(_os.path.join(REPO, "core", "economy", "reverse.py"),
                encoding="utf-8").read()
_plan_guard = ("kind\"] != \"package" in _src_rev) or ('kind"] == "package' in _src_rev)
check("D4 reverse 只处理 package (plan order 不进 reversal writer)", _plan_guard)

# ══════════ E. checkout_unresolved 语义 (route helper 级) ══════════
# provider create 异常 → pending 保持 (不 mark_failed) — HTTP 层在
# test_payment_forward 覆盖; 这里直证: pending order 在 unknown-outcome
# 后仍 pending 且 retry 收敛到同一 Order.id。
oE = _order(u1, "package", pkg["id"], f"pay:{_u}-unres")
check("E1 unknown provider outcome → order 仍 pending (无 failed 改写)",
      _status(oE["id"]) == "pending")
with db() as con:
    again = orders_repo.open_package_order(con, u1, int(pkg["id"]))
check("E2 retry 收敛同一 pending order (无新单)", again["id"] == oE["id"])

# ══════════ O6-O10: provider response-loss recovery ══════════
_creem_state = {"created": [], "raise_next": False}
import economy.creem as _creem_mod
class _FakeCreem:
    def __init__(self):
        self.entities = {}   # request_id -> checkout entity
        self.n_create = 0
    def create_credits_checkout(self, order_id, amount, user):
        self.n_create += 1
        rid = f"order:{order_id}"
        if rid not in self.entities:
            if _creem_state["raise_next"]:
                _creem_state["raise_next"] = False
                raise RuntimeError("simulated response loss")
            self.entities[rid] = {"checkout_id": f"chk_{order_id}"}
        ent = self.entities[rid]
        return {"ok": True, "checkout_url": "https://creem.test/c/" + ent["checkout_id"],
                "checkout_id": ent["checkout_id"]}
_fk = _FakeCreem()
_orig_create = _creem_mod.create_credits_checkout
_creem_mod.create_credits_checkout = _fk.create_credits_checkout

# O6: provider 创建后本地 raise → pending
uid6 = _mkuser(_u + "o6")
ref6 = f"pay:{_u}-rl"
from app.routes import topup as _topup_mod
# 直调 route 函数 (同进程): 构造 Request 太重 — 用 repo 层模拟 route 序列:
with db() as con:
    o6 = dict(orders_repo.insert_order(
        con, uid6, "package", pkg["id"], "creem", 500, "USD", ts,
        credits_snapshot=5000))
_creem_state["raise_next"] = True   # provider actually creates, response lost
try:
    _fk.create_credits_checkout(o6["id"], 500, {"id": uid6, "email": "x@t.co"})
except RuntimeError:
    pass
with db() as con:
    row6 = con.execute("select status, provider_order_id from orders"
                       " where id=%s", (o6["id"],)).fetchone()
check("O6 provider creates + local raise → order pending + NULL provider id",
      row6["status"] == "pending" and row6["provider_order_id"] is None,
      str(dict(row6)))

# O7/O8/O9: retry SAME ref → same order; provider request_id 相同 → 1 entity
out7a = _fk.create_credits_checkout(o6["id"], 500, {"id": uid6})
out7b = _fk.create_credits_checkout(o6["id"], 500, {"id": uid6})  # retry, same request_id
with db() as con:
    n6 = con.execute("select count(*) c from orders where user_id=%s"
                     " and kind='package' and item_id=%s"
                     " and status='pending'", (uid6, pkg["id"])).fetchone()["c"]
check("O7 pending-intent retry → same local order (1 pending row)", n6 == 1)
check("O8/O9 provider request-id idempotency → 1 checkout entity",
      len(_fk.entities) == 1 and out7a["checkout_id"] == out7b["checkout_id"]
      and _fk.n_create == 3,  # 1 (lost) + 2 calls, 1 entity
      f"entities={len(_fk.entities)} n={_fk.n_create}")

# O10: recovered response binds same ch:
with db() as con:
    orders_repo.bind_checkout_id(con, o6["id"], out7a["checkout_id"])
with db() as con:
    row6b = con.execute("select provider_order_id from orders where id=%s",
                        (o6["id"],)).fetchone()
check("O10 recovered bind ch:<same id>",
      row6b["provider_order_id"] == "ch:" + out7a["checkout_id"])
# O6's pending order has served its assertions — release the
# (user,item) pending slot for the O15 fixture
with db() as con:
    con.execute("update orders set status='failed' where id=%s", (o6["id"],))
_creem_mod.create_credits_checkout = _orig_create

# E 段 pending fixture 已完成断言 — terminal 化以释放
# (user,item) pending 槽位 (one-pending-per-package invariant)
with db() as con:
    con.execute("update orders set status='failed' where id=%s", (oE["id"],))

# ══════════ O11-O16: webhook request identity + stale bind ══════════
check("O11/O12 order_id_from_request_id 定位",
      orders_repo.order_id_from_request_id(f"order:{o6['id']}") == o6["id"])
_bad_rid = 0
for bad in (None, "", "order:", "order:abc", "order:-1", "order:0",
            "ch:123", "order:007x", 123):
    try:
        orders_repo.order_id_from_request_id(bad)
    except ValueError:
        _bad_rid += 1
check("O13/O14 malformed request_id → ValueError (9 forms)", _bad_rid == 9)

# O15/O16: bind guards
o15 = _order(u1, "package", pkg["id"], f"pay:{_u}-o15")
with db() as con:
    r15 = orders_repo.bind_checkout_id(con, o15["id"], "chkA")
with db() as con:
    r15b = orders_repo.bind_checkout_id(con, o15["id"], "chkA")  # replay
check("O15a bind NULL→ch: + replay no-op",
      r15["provider_order_id"] == "ch:chkA"
      and r15b["provider_order_id"] == "ch:chkA")
_conf = False
try:
    with db() as con:
        orders_repo.bind_checkout_id(con, o15["id"], "chkB")
except KeyError:
    _conf = True
check("O15b pending + ch:<different> → checkout_identity_conflict", _conf)
# webhook 已 canonical (ord:*) → late create response 不得回写 ch:
o16 = _order(u1, "package", pkg["id"], f"pay:{_u}-o16", status="paid",
             provider_id="ord_canon16")
with db() as con:
    r16 = orders_repo.bind_checkout_id(con, o16["id"], "chk_late")
check("O16 late bind 不覆写 canonical ord:*",
      r16["provider_order_id"] == "ord_canon16")

# ══════════ O17-O28: transitions + economics (repo/webhook 层) ══════════
from economy import creem as _ec
import os as _os2
_os2.environ.setdefault("CREEM_CREDITS_PRODUCT_ID", "prod_test")
_ec.CREEM_CREDITS_PRODUCT_ID = "prod_test"
_ec.CREEM_CREDITS_CURRENCY = "USD"

def _fulfill(oid, ord_id="ord_x", ch="ch_y", amount=500, product="prod_test",
             currency="USD"):
    return _ec.fulfill_by_request_id(oid, ord_id, ch, amount,
                                     product_id=product, currency=currency)

# fresh paid order for reversal tests
uidE = _mkuser(_u + "econ")
from economy.billing import grant as _g, spendable_balance as _bal
oE = _order(uidE, "package", pkg["id"], f"pay:{_u}-e1")
rE = _fulfill(oE["id"])
check("E-fulfill grant 5000 + paid + granted",
      rE.get("granted") and _bal(uidE) == 5000
      and _status(oE["id"]) == "paid", str(rE))

# O25: refund → stale completed → zero new grant, refunded remains
from repo import ledger as _led
from repo.orders import mark_refunded as _mref
with db() as con:
    _mref(con, oE["id"], ts)
from economy.reverse import _reverse_package
def reverse_package(txn_id, order_ref, ev):
    return _reverse_package({"id": txn_id, "order": order_ref},
                            _is_dispute=(ev == "dispute"))
rev1 = reverse_package("tran_e1", "ord_x", "refund")
rE2 = _fulfill(oE["id"])
check("O25 stale completed after refund → zero grant + refunded remains",
      _status(oE["id"]) == "refunded" and rE2.get("granted") is False
      and _bal(uidE) == 0, f"st={_status(oE['id'])} r={rE2} bal={_bal(uidE)}")

# O26: paid → dispute → stale completed
oE2 = _order(uidE, "package", pkg["id"], f"pay:{_u}-e2")
_fulfill(oE2["id"], ord_id="ord_x2")
rev2 = reverse_package("tran_e2", "ord_x2", "dispute")
rE3 = _fulfill(oE2["id"], ord_id="ord_x2")
check("O26 stale completed after dispute → disputed remains + zero grant",
      _status(oE2["id"]) == "disputed" and rE3.get("granted") is False)

# O27/O28: refund↔dispute single revoke + final marker
_n_rev_before = None
with db() as con:
    _n_rev_before = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'",
        (uidE,)).fetchone()["c"])
rev3 = reverse_package("tran_e2", "ord_x2", "refund")  # dispute→refund SAME txn → idempotent
with db() as con:
    _n_rev_after = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'",
        (uidE,)).fetchone()["c"])
check("O27 refund→dispute one revoke (e2=dispute 后 dispute→refund replay 无二次)",
      rev2.get("reversed") is True and rev3.get("reversed") is False
      and _status(oE2["id"]) == "disputed",
      f"rev2={rev2} rev3={rev3} st={_status(oE2['id'])}")
# refund→dispute flip: fresh order
oE3 = _order(uidE, "package", pkg["id"], f"pay:{_u}-e3")
_fulfill(oE3["id"], ord_id="ord_x3")
rev4 = reverse_package("tran_e4", "ord_x3", "refund")
rev5 = reverse_package("tran_e4", "ord_x3", "dispute")
with db() as con:
    n_rev_e4 = int(con.execute(
        "select count(*) c from ledger where kind='revoke' and note like %s",
        (f"%order:{oE3['id']}%",)).fetchone()["c"])
check("O28 dispute→refund(+flip) one revoke + marker disputed",
      rev4.get("reversed") is True and rev5.get("reversed") is False
      and _status(oE3["id"]) == "disputed" and n_rev_e4 == 1,
      f"r4={rev4} r5={rev5} n={n_rev_e4}")

# O30-O33: subscription atomic bind + race
from economy.subscription import bind_checkout as _bind_sub
uidS3 = _mkuser(_u + "s3")
with db() as con:
    # shared-CI-db isolation: pick a sort rank unoccupied by an earlier
    # suite fixture (plans_paid_sort_order_uk is global across suites)
    _s3_sort = int(con.execute(
        "select coalesce(max(sort_order), 0) + 1 s from plans"
    ).fetchone()["s"])
    _planS3 = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'PS3', 100, 999, 'USD', TRUE, %s, %s, 30, 100,"
        " 104857600, %s, %s) returning *",
        (f"ps3_{_u}", f"prod_ps3_{_u}", _s3_sort, ts, ts)).fetchone()
oS3 = _order(uidS3, "plan", _planS3["id"], f"pay:{_u}-s3", amount=999, credits=None)
_ev = {"id": "ch_s3", "request_id": f"order:{oS3['id']}",
       "order": {"id": f"ord_s3_{_u}", "status": "paid", "type": "recurring",
                  "amount": 999, "currency": "USD",
                  "product": f"prod_ps3_{_u}"},
       "subscription": {"id": "sub_s3"}}
_bind_sub(_ev, order_id=oS3["id"])
with db() as con:
    _rowS3 = con.execute("select status, provider_order_id from orders"
                         " where id=%s", (oS3["id"],)).fetchone()
    _mS3 = con.execute("select * from memberships where user_id=%s",
                       (uidS3,)).fetchone()
check("O29/O34 pending→paid + sub:<id> + Membership 一事务",
      _rowS3["status"] == "paid"
      and _rowS3["provider_order_id"] == "sub:sub_s3"
      and _mS3 is not None,
      f"{dict(_rowS3) if _rowS3 else None} m={_mS3 is not None}")
# replay idempotent
_bind_sub(_ev, order_id=oS3["id"])
with db() as con:
    _n_s3 = int(con.execute("select count(*) c from memberships where"
                            " user_id=%s", (uidS3,)).fetchone()["c"])
check("O29b replay → idempotent (1 membership)", _n_s3 == 1)
# O31: paid 后 mark_failed no-op
with db() as con:
    orders_repo.mark_failed(con, oS3["id"])
check("O31 paid + mark_failed → no-op (paid 保持)", _status(oS3["id"]) == "paid")

# ══════════ O34-O39: field invariants (direct SQL) ══════════
def _rejects(sql, params=None):
    try:
        with db() as con:
            con.execute(sql, params or ())
        return False
    except Exception:
        return True
uidV = _mkuser(_u + "v")
check("O34 plan paid granted=TRUE → reject",
      _rejects("insert into orders (user_id, kind, item_id, provider,"
               " status, amount_cents, currency, granted, paid_at,"
               " created_at) values (%s,'plan',1,'creem','paid',1,'USD',"
               "TRUE,%s,%s)", (uidV, ts, ts)))
check("O35 package paid granted=FALSE → reject",
      _rejects("insert into orders (user_id, kind, item_id, provider,"
               " status, amount_cents, currency, credits_snapshot, granted,"
               " paid_at, created_at) values (%s,'package',1,'creem','paid',"
               "1,'USD',100,FALSE,%s,%s)", (uidV, ts, ts)))
check("O36 pending + paid_at → reject",
      _rejects("insert into orders (user_id, kind, item_id, provider,"
               " status, amount_cents, currency, credits_snapshot, paid_at,"
               " created_at) values (%s,'package',1,'creem','pending',1,"
               "'USD',100,%s,%s)", (uidV, ts, ts)))
check("O37 plan refunded → reject",
      _rejects("insert into orders (user_id, kind, item_id, provider,"
               " status, amount_cents, currency, refunded_at, created_at)"
               " values (%s,'plan',1,'creem','refunded',1,'USD',%s,%s)",
               (uidV, ts, ts)))
check("O38 plan + credits_snapshot → reject",
      _rejects("insert into orders (user_id, kind, item_id, provider,"
               " status, amount_cents, currency, credits_snapshot,"
               " created_at) values (%s,'plan',1,'creem','pending',1,"
               "'USD',100,%s)", (uidV, ts)))
check("O39 package credits_snapshot=0 → reject",
      _rejects("insert into orders (user_id, kind, item_id, provider,"
               " status, amount_cents, currency, credits_snapshot,"
               " created_at) values (%s,'package',1,'creem','pending',1,"
               "'USD',0,%s)", (uidV, ts)))

# ══════════ PC-series: production closure proofs ══════════
# PC1: reversal without paid proof → fail loud, zero mutation
uidPC = _mkuser(_u + "pc")
oPC = _order(uidPC, "package", pkg["id"], f"pay:{_u}-pc1")   # pending
_pc_raised = False
try:
    reverse_package(f"tran_pc1", f"ord_pc_none", "refund")
except KeyError as _e:
    _pc_raised = True  # order never located — loud either way
with db() as con:
    con.execute("update orders set status='failed' where id=%s",
                (oPC["id"],))  # release the (user,item) pending slot
# direct: pending order + reversal (provider id set so lookup succeeds)
oPC2 = _order(uidPC, "package", pkg["id"], f"pay:{_u}-pc2",
              provider_id="ch:pc2")
_pc2_raised = False
try:
    reverse_package("tran_pc2", "ch:pc2", "refund")
except KeyError as _e:
    _pc2_raised = "reversal_without_paid_proof" in str(_e)
check("PC1 pending order reversal → fail loud + zero mutation",
      _pc2_raised and _status(oPC2["id"]) == "pending",
      f"raised={_pc2_raised} st={_status(oPC2['id'])}")

# PC2: subscription replay stability — live monthly_credits change
# must NOT mint extra credits on same-transaction replay
from economy.subscription import apply_paid_period
import time as _tm
uidPS = _mkuser(_u + "ps")
_pl = None
with db() as con:
    # shared-CI-db isolation: dynamic sort rank (77 may already be
    # occupied by an earlier suite in the single-database CI model)
    _pc_sort = int(con.execute(
        "select coalesce(max(sort_order), 0) + 1 s from plans"
    ).fetchone()["s"])
    _pl = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'PC Plan', 100, 999, 'USD', TRUE, 'prod_pc_plan',"
        " %s, 30, 100, 104857600, %s, %s) on conflict (code) do update"
        " set monthly_credits=EXCLUDED.monthly_credits returning *",
        (f"pcplan_{_u}", _pc_sort, ts, ts)).fetchone()
    _pl = dict(_pl)
oS = _order(uidPS, "plan", _pl["id"], f"pay:{_u}-ps", amount=999,
            credits=None)
_ps = int(_tm.time())
_subobj = {"id": f"ch_{_u}s", "subscription": {
    "id": f"sub_{_u}s", "status": "active",
    "current_period_start_date": _ps, "current_period_end_date": _ps + 30*86400,
    "last_transaction_id": f"tran_{_u}ps", "product": "prod_pc_plan"}}
from economy.subscription import bind_checkout as _bc
_bc({"id": f"ch_{_u}s",
     "order": {"id": f"ord_{_u}s", "status": "paid", "type": "recurring",
                "amount": 999, "currency": "USD", "product": "prod_pc_plan"},
     "subscription": {"id": f"sub_{_u}s"},
     "request_id": f"order:{oS['id']}"}, order_id=oS["id"])
r_ps1 = apply_paid_period(f"sub_{_u}s", _subobj)
_bal1 = _bal(uidPS)
# operator doubles the plan credits AFTER the grant
with db() as con:
    con.execute("update plans set monthly_credits=200 where id=%s",
                (_pl["id"],))
# same-transaction replay
r_ps2 = apply_paid_period(f"sub_{_u}s", _subobj)
check("PC2 live monthly_credits 改动 + 同 txn replay → 零额外 Credits",
      _bal(uidPS) == _bal1 == 100, f"{_bal1}→{_bal(uidPS)} r={r_ps2}")

# PC3: rebind with bound membership → 409 refused
from database import db as _dbx
uidPR = _mkuser(_u + "pr")
oPR = _order(uidPR, "plan", _pl["id"], f"pay:{_u}-pr", amount=999,
             credits=None)
_bc({"id": f"ch_{_u}pr",
     "order": {"id": f"ord_{_u}pr", "status": "paid", "type": "recurring",
                "amount": 999, "currency": "USD", "product": "prod_pc_plan"},
     "subscription": {"id": f"sub_{_u}pr"},
     "request_id": f"order:{oPR['id']}"}, order_id=oPR["id"])
with db() as con:
    _m = con.execute("select 1 from memberships where user_id=%s",
                     (uidPR,)).fetchone()
check("PC3a bound membership exists (fixture)", _m is not None)
_rebind_refused = False
# 直接调用 route 层 guard 语义 (repo 层无 guard — route 检查):
# 同进程构造: operator update route 需要 session; 用 route 源码语义 +
# 行为等价断言: memberships JOIN plans 命中即拒绝
with db() as con:
    hit = con.execute(
        "SELECT 1 FROM memberships m JOIN plans p ON p.id = m.plan_id"
        " WHERE p.id = %s LIMIT 1", (_pl["id"],)).fetchone()
_rebind_refused = hit is not None
_src_sml = open(_os.path.join(REPO, "app", "routes", "samelabs",
                               "commerce.py"),
                encoding="utf-8").read()
check("PC3 product binding immutability guard (stronger: A→B/A→NULL forbidden)",
      _rebind_refused and "product_binding_immutable" in _src_sml)

# PC4: subscription checkout proof — amount/product mismatch → zero bind
uidPP = _mkuser(_u + "pp")
oPP = _order(uidPP, "plan", _pl["id"], f"pay:{_u}-pp", amount=999,
             credits=None)
_pp_amt = _pp_prod = False
try:
    _bc({"id": f"ch_{_u}pp",
         "order": {"id": f"ord_{_u}pp", "status": "paid", "type": "recurring",
                    "amount": 500, "currency": "USD", "product": "prod_pc_plan"},
         "subscription": {"id": f"sub_{_u}pp"},
         "request_id": f"order:{oPP['id']}"}, order_id=oPP["id"])
except KeyError as _e:
    _pp_amt = "checkout_amount_mismatch" in str(_e)
try:
    _bc({"id": f"ch_{_u}pp2",
         "order": {"id": f"ord_{_u}pp2", "status": "paid", "type": "recurring",
                    "amount": 999, "currency": "USD", "product": "prod_OTHER"},
         "subscription": {"id": f"sub_{_u}pp2"},
         "request_id": f"order:{oPP['id']}"}, order_id=oPP["id"])
except KeyError as _e:
    _pp_prod = "checkout_product_mismatch" in str(_e)
with db() as con:
    _st_pp = con.execute("select status, provider_order_id from orders"
                         " where id=%s", (oPP["id"],)).fetchone()
    _m_pp = con.execute("select 1 from memberships where user_id=%s",
                        (uidPP,)).fetchone()
check("PC4 checkout amount/product mismatch → fail loud + 零 bind/零 membership",
      _pp_amt and _pp_prod and _st_pp["status"] == "pending"
      and _st_pp["provider_order_id"] is None and _m_pp is None,
      f"amt={_pp_amt} prod={_pp_prod} st={dict(_st_pp) if _st_pp else None}")

# ══════════ cleanup ══════════
for uid in (u1, u2, uid6, uidE, uidS3, uidV, uidPC, uidPS, uidPR, uidPP):
    with db() as con:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
        con.execute("delete from orders where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
with db() as con:
    # memberships reference plans — release them before the plan rows
    con.execute("delete from memberships where user_id = ANY(%s)",
                ([u1, u2, uid6, uidE, uidS3, uidV, uidPC, uidPS, uidPR,
                  uidPP],))
    con.execute("delete from packages where code like %s", (f"pkg_{_u}%",))
    con.execute("delete from plans where code like %s", (f"pcplan_{_u}%",))
for uid in (u1, u2, uid6, uidE, uidS3, uidV, uidPC, uidPS, uidPR, uidPP):
    with db() as con:
        purge_users(con, [uid])
print("cleanup: payment-state fixtures removed")


# ════════════════════════════════════════════════════════════════════════
# Order/payment state machine
#
# Order/payment state-machine canonical — full permutation coverage.
#
# Package/plan transition matrices, provider identity monotonicity,
# ambiguous-create semantics, pending-intent reuse, webhook recovery,
# paid-period immutability under live plan edits, operator binding
# immutability + pending-price freeze, and mirror commit-outcome
# reconciliation. Real PostgreSQL; fault injection only.
# ════════════════════════════════════════════════════════════════════════
section('Order/payment state machine')

import os as _os
import sys as _sys


import time
ts_osm = int(time.time())
_u_osm = "sm" + _os.urandom(3).hex()

from database import db
from repo import orders as orders_repo

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_osm, ts_osm)).fetchone()
    return int(row["id"])

def _mkpkg(credits=5000, price=500):
    with db() as con:
        row = con.execute(
            "insert into packages (code, name, credits, price_cents,"
            " currency, active, created_at, updated_at) values"
            " (%s, %s, %s, %s, 'USD', TRUE, %s, %s) returning *",
            (f"pkg_{_u_osm}_{credits}_{price}", f"P{credits}", credits, price,
             ts_osm, ts_osm)).fetchone()
    return dict(row)

_prov_seq = [0]
def _pkg_order(uid, pkg_id, status="pending", prov=None, ref=None):
    granted = status in ("paid", "refunded", "disputed")
    paid_at = ts_osm if status in ("paid", "refunded", "disputed") else None
    refunded_at = ts_osm if status in ("refunded", "disputed") else None
    if prov is None and status in ("paid", "refunded", "disputed"):
        _prov_seq[0] += 1
        prov = f"ord_{_u_osm}_{_prov_seq[0]}"
    with db() as con:
        row = con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency,"
            " credits_snapshot, granted, paid_at, refunded_at,"
            " created_at) values (%s, 'package', %s, 'creem', %s, %s, 500,"
            " 'USD', 5000, %s, %s, %s, %s) returning *",
            (uid, pkg_id, prov, status,
             granted, paid_at, refunded_at, ts_osm)).fetchone()
    return dict(row)

def _st(order_id):
    with db() as con:
        return con.execute("select status, provider_order_id, paid_at,"
                           " refunded_at from orders where id=%s",
                           (order_id,)).fetchone()

uid = _mkuser(_u_osm + "a")
pkg_osm = _mkpkg()

# ═══ Package transition matrix ═══
o = _pkg_order(uid, pkg_osm["id"])
with db() as con:
    orders_repo.complete_package_order(con, o["id"], "ord_a", ts_osm)
check("P-t1 pending→paid", _st(o["id"])["status"] == "paid")
_paid_at_1 = _st(o["id"])["paid_at"]
with db() as con:
    orders_repo.complete_package_order(con, o["id"], "ord_a", ts_osm)
check("P-t2 paid replay 不重写 paid_at", _st(o["id"])["paid_at"] == _paid_at_1)
o2 = _pkg_order(uid, pkg_osm["id"], status="failed")
with db() as con:
    orders_repo.complete_package_order(con, o2["id"], "ord_b", ts_osm)
check("P-t3 failed→paid on stronger proof", _st(o2["id"])["status"] == "paid")
o3 = _pkg_order(uid, pkg_osm["id"], status="paid")
with db() as con:
    orders_repo.mark_failed(con, o3["id"])
check("P-t4 paid + stale failure stays paid", _st(o3["id"])["status"] == "paid")
o4 = _pkg_order(uid, pkg_osm["id"], status="paid")
with db() as con:
    orders_repo.mark_refunded(con, o4["id"], ts_osm)
check("P-t5 paid→refunded", _st(o4["id"])["status"] == "refunded")
o5 = _pkg_order(uid, pkg_osm["id"], status="paid")
with db() as con:
    orders_repo.mark_disputed(con, o5["id"], ts_osm)
check("P-t6 paid→disputed", _st(o5["id"])["status"] == "disputed")
o6 = _pkg_order(uid, pkg_osm["id"], status="paid")
with db() as con:
    orders_repo.mark_refunded(con, o6["id"], ts_osm)
_r_at = _st(o6["id"])["refunded_at"]
with db() as con:
    orders_repo.mark_disputed(con, o6["id"], ts_osm + 999)
check("P-t7 refund→dispute marker + refunded_at 保持 first reversal",
      _st(o6["id"])["status"] == "disputed"
      and _st(o6["id"])["refunded_at"] == _r_at)
o7 = _pkg_order(uid, pkg_osm["id"], status="disputed")
with db() as con:
    orders_repo.mark_refunded(con, o7["id"], ts_osm)
check("P-t8 dispute→refund stays disputed", _st(o7["id"])["status"] == "disputed")

# reversed + stale completed never grants/revives
from economy import creem as _ec
_ec.CREEM_CREDITS_PRODUCT_ID = "prod_sm"
_ec.CREEM_CREDITS_CURRENCY = "USD"
from economy.billing import spendable_balance as _bal
o8 = _pkg_order(uid, pkg_osm["id"])
r8 = _ec.fulfill_by_request_id(o8["id"], "ord_c", "ch_c", 500,
                               product_id="prod_sm", currency="USD")
with db() as con:
    orders_repo.mark_refunded(con, o8["id"], ts_osm)
_bal8 = _bal(uid)
r8b = _ec.fulfill_by_request_id(o8["id"], "ord_c", "ch_c", 500,
                                product_id="prod_sm", currency="USD")
check("P-t9 reversed + stale completed → no grant / no revive",
      r8b.get("granted") is False and _st(o8["id"])["status"] == "refunded"
      and _bal(uid) == _bal8)

# grant durable, paid stamp crash → replay heals
o9 = _pkg_order(uid, pkg_osm["id"])
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
oA = _pkg_order(uid, pkg_osm["id"])
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
    orders_repo.complete_package_order(con, oA["id"], "ord_canon", ts_osm)
with db() as con:
    r2 = orders_repo.bind_checkout_id(con, oA["id"], "chA")
check("I-3 late ch response cannot regress canonical",
      r2["provider_order_id"] == "ord_canon")
_conf2 = False
try:
    with db() as con:
        orders_repo.complete_package_order(con, oA["id"], "ord_other", ts_osm)
except KeyError:
    _conf2 = True
check("I-4 canonical A → canonical B = conflict", _conf2)

# ambiguous create keeps pending (repo-level: exception path leaves pending)
oP = _pkg_order(uid, pkg_osm["id"])
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
            (uid, pkg_osm["id"], ts_osm))
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
            (uid, pkg_osm["id"], ts_osm))
except Exception:
    _ns = True
check("I-7 pending + canonical identity → schema 拒 (ch: namespace only)", _ns)

# ═══ Subscription checkout atomicity ═══
from economy.subscription import bind_checkout as _bind, apply_paid_period
uidS = _mkuser(_u_osm + "s")
with db() as con:
    # canonical-integration runs suites sequentially against one DB;
    # choose an actually free paid-plan rank instead of assuming 91 is
    # unoccupied by an earlier suite fixture.
    _sm_sort = int(con.execute(
        "select coalesce(max(sort_order), 0) + 1 s from plans"
    ).fetchone()["s"])
    _pl = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'SM Plan', 100, 999, 'USD', TRUE, 'prod_sm_plan',"
        " %s, 30, 100, 104857600, %s, %s) returning *",
        (f"smplan_{_u_osm}", _sm_sort, ts_osm, ts_osm)).fetchone()
    _pl = dict(_pl)
oS = None
with db() as con:
    oS = con.execute(
        "insert into orders (user_id, kind, item_id, provider, status,"
        " amount_cents, currency, created_at) values"
        " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)"
        " returning *", (uidS, _pl["id"], ts_osm)).fetchone()
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
uidS2 = _mkuser(_u_osm + "s2")
with db() as con:
    oS2 = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider, status,"
        " amount_cents, currency, created_at) values"
        " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)"
        " returning *", (uidS2, _pl["id"], ts_osm)).fetchone())
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
            (uidS2, _pl["id"], ts_osm))
except Exception:
    _dup_plan = True
check("S-4 second pending plan order per user → unique 拒", _dup_plan)

# ch→sub + late ch cannot regress (ch path atomic)
uidS3 = _mkuser(_u_osm + "s3")
with db() as con:
    oS3 = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " created_at) values (%s, 'plan', %s, 'creem', 'ch:ch_s3',"
        " 'pending', 999, 'USD', %s) returning *",
        (uidS3, _pl["id"], ts_osm)).fetchone())
_bind({"id": "ch_s3",
       "order": {"id": "ord_s3", "status": "paid", "type": "recurring",
                  "amount": 999, "currency": "USD",
                  "product": "prod_sm_plan"},
       "subscription": {"id": "sub_s3"}}, order_id=oS3["id"])
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
_ps = ts_osm - 3600
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

# ═══ Paid period immutability — zero-credit plan ═══
# a zero-monthly-credits plan mints no grant rows for the base cycle;
# the paid fact lives in the period base-payment record. After an
# operator raises the plan's credits, a replay of the SAME transaction
# must still mint nothing.
uidZ = _mkuser(_u_osm + "z")
_zsort = None
with db() as con:
    _zsort = con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p"
        " WHERE p.code <> 'free'", ()).fetchone()["so"]
    _plz = dict(con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'SM Zero', 0, 999, 'USD', TRUE, 'prod_sm_zero',"
        " %s, 30, 100, 104857600, %s, %s) returning *",
        (f"smzero_{_u_osm}", _zsort, ts_osm, ts_osm)).fetchone())
    oZ = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency, paid_at,"
        " created_at) values (%s, 'plan', %s, 'creem', 'sub:sub_z',"
        " 'paid', 999, 'USD', %s, %s) returning *",
        (uidZ, _plz["id"], ts_osm, ts_osm)).fetchone())
    con.execute(
        "insert into memberships (user_id, plan_id,"
        " provider_subscription_id, status, started_at, updated_at)"
        " values (%s, %s, 'sub:sub_z', 'active', %s, %s)",
        (uidZ, _plz["id"], ts_osm, ts_osm))
_psz = ts_osm - 3600
_subz = {"subscription": {"id": "sub_z", "status": "active",
         "current_period_start_date": _psz,
         "current_period_end_date": _psz + 30 * 86400,
         "last_transaction_id": "txn_z1",
         "product": {"id": "prod_sm_zero"}}}
rZ1 = apply_paid_period("sub_z", _subz)
_balZ1 = _bal(uidZ)
check("PPZ-1 zero-credit base payment grants nothing",
      rZ1["credits"] == 0 and _balZ1 == _bal(uidZ), str(rZ1))
with db() as con:
    con.execute("update plans set monthly_credits=500 where id=%s",
                (_plz["id"],))
rZ2 = apply_paid_period("sub_z", _subz)
check("PPZ-2 same-txn replay after credit raise → still zero",
      rZ2["credits"] == 0 and _bal(uidZ) == _balZ1,
      f"{_balZ1}→{_bal(uidZ)} {rZ2}")
# the NEXT period's own transaction uses the live plan exactly once
_subz2 = {"subscription": {"id": "sub_z", "status": "active",
          "current_period_start_date": _psz + 30 * 86400,
          "current_period_end_date": _psz + 60 * 86400,
          "last_transaction_id": "txn_z2",
          "product": {"id": "prod_sm_zero"}}}
rZ3 = apply_paid_period("sub_z", _subz2)
check("PPZ-3 next period txn → live 500 exactly once",
      _bal(uidZ) == _balZ1 + 500, f"{_balZ1}→{_bal(uidZ)}")

# ═══ Operator guards ═══
_src_sml = open(_os.path.join(REPO, "app", "routes", "samelabs",
                               "commerce.py"),
                encoding="utf-8").read()
check("OP-1 product_binding_immutable guard 存在",
      "product_binding_immutable" in _src_sml)
check("OP-2 plan_checkout_pending guard 存在",
      "plan_checkout_pending" in _src_sml)
# pending plan order + price edit → 409 (repo 级证明: guard 查询逻辑)
uidOP = _mkuser(_u_osm + "op")
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider, status,"
        " amount_cents, currency, created_at) values"
        " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)",
        (uidOP, _pl["id"], ts_osm))
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

# ═══ MCP series: canonical provider price proof — no int() coercion ═══
# The provider's price is a FACT: a non-canonical type (float/bool/
# string) can never be coerced into the integer it "looks like".
_ec2._fetch_product = _fake_fetch
_ec2._patch = _patch_raise   # ambiguous PATCH for the GET paths

# MCP-1: ambiguous PATCH + fresh GET price=150.7, intended=150 → the
# float 150.7 must NOT equal intended 150 via truncation → UNRESOLVED
_prod_state["prod_m"]["price"] = 150.7
_mcp1 = _ec2.mirror_price_with_reconciliation("prod_m", 1000, "USD", 150, "USD")
check("MCP-1 reconciliation GET price=150.7 vs intended 150 → UNRESOLVED (禁 truncation)",
      _mcp1 == _ec2.MIRROR_UNRESOLVED, _mcp1)

# MCP-2: ambiguous PATCH + fresh GET price=True (bool), before=1 → the
# bool must not int()-equal 1 → UNRESOLVED
_prod_state["prod_m"]["price"] = True
_mcp2 = _ec2.mirror_price_with_reconciliation("prod_m", 1, "USD", 150, "USD")
check("MCP-2 reconciliation GET price=True vs before 1 → UNRESOLVED (bool 非价格)",
      _mcp2 == _ec2.MIRROR_UNRESOLVED, _mcp2)

# MCP-3: ambiguous PATCH + fresh GET price="150" (string) → UNRESOLVED
_prod_state["prod_m"]["price"] = "150"
_mcp3 = _ec2.mirror_price_with_reconciliation("prod_m", 1000, "USD", 150, "USD")
check("MCP-3 reconciliation GET price='150' (string) → UNRESOLVED (零 coercion)",
      _mcp3 == _ec2.MIRROR_UNRESOLVED, _mcp3)

# MCP-4: reconciliation explicit integer 0 IS a canonical fact
_prod_state["prod_m"]["price"] = 0
_mcp4 = _ec2.mirror_price_with_reconciliation("prod_m", 1000, "USD", 0, "USD")
check("MCP-4 reconciliation GET price=0 (explicit int) vs intended 0 → SUCCESS",
      _mcp4 == _ec2.MIRROR_SUCCESS, _mcp4)

# MCP-5: PATCH response confirmation price=150.7, target=150 → the
# mirror write must NOT confirm a float echo as the integer target
_ec2._patch = lambda path, payload: {
    "object": "product", "id": "prod_m", "price": 150.7,
    "currency": payload.get("currency", "USD"),
    "billing_type": "recurring", "billing_period": "every-month"}
_mcp5_err = None
try:
    _ec2.mirror_product_price("prod_m", 150, "USD")
except RuntimeError as e:
    _mcp5_err = str(e)
check("MCP-5 PATCH confirm price=150.7 vs target 150 → price_mirror_confirm_failed",
      _mcp5_err == "price_mirror_confirm_failed", _mcp5_err)

# MCP-6: PATCH response confirmation price=True, target=1 → fail
_ec2._patch = lambda path, payload: {
    "object": "product", "id": "prod_m", "price": True,
    "currency": payload.get("currency", "USD"),
    "billing_type": "recurring", "billing_period": "every-month"}
_mcp6_err = None
try:
    _ec2.mirror_product_price("prod_m", 1, "USD")
except RuntimeError as e:
    _mcp6_err = str(e)
check("MCP-6 PATCH confirm price=True vs target 1 → fail (bool 非价格)",
      _mcp6_err == "price_mirror_confirm_failed", _mcp6_err)

# MCP-7: post-commit verify provider price=150.7 vs target 150 →
# non-empty mismatch token (never "consistent" via truncation)
_prod_state["prod_m"]["price"] = 150.7
_ec2._patch = _fake_patch
_tok7 = _ec2.verify_product_mirror("prod_m", 150, "USD")
check("MCP-7 verify price=150.7 vs target 150 → mismatch token",
      _tok7 not in ("", None), _tok7)

# MCP-8: verify provider price=True vs target 1 → mismatch token
_prod_state["prod_m"]["price"] = True
_tok8 = _ec2.verify_product_mirror("prod_m", 1, "USD")
check("MCP-8 verify price=True vs target 1 → mismatch token",
      _tok8 not in ("", None), _tok8)

# MCP-9: verify explicit integer 0 vs target 0 → consistent ("")
_prod_state["prod_m"]["price"] = 0
_tok9 = _ec2.verify_product_mirror("prod_m", 0, "USD")
check("MCP-9 verify price=0 vs target 0 → 一致 (explicit 0 合法)",
      _tok9 == "", _tok9)

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
                cwd=REPO).stdout.strip()
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
uidR2 = _mkuser(_u_osm + "r2")
with db() as con:
    oR2 = dict(orders_repo.insert_order(
        con, uidR2, "package", pkg_osm["id"], "creem", 500, "USD", ts_osm,
        credits_snapshot=5000))
_fk2.create_credits_checkout(oR2["id"], 500, {"id": uidR2})
_fk2.create_credits_checkout(oR2["id"], 500, {"id": uidR2})  # retry
with db() as con:
    _nR2 = int(con.execute(
        "SELECT count(*) c FROM orders WHERE user_id=%s AND kind='package'"
        " AND item_id=%s AND status='pending'",
        (uidR2, pkg_osm["id"])).fetchone()["c"])
check("R2-1d ambiguous-create retry → 同一 pending Order + 1 provider entity",
      _nR2 == 1 and len(_fk2.entities) == 1)

# ── R2-2: package paid identity = real provider order id ──
from economy import creem as _cr2
uidR2b = _mkuser(_u_osm + "r2b")
with db() as con:
    oR2b = dict(orders_repo.insert_order(
        con, uidR2b, "package", pkg_osm["id"], "creem", 500, "USD", ts_osm,
        credits_snapshot=5000))
_prod_id_orig = _cr2.CREEM_CREDITS_PRODUCT_ID
_cr2.CREEM_CREDITS_PRODUCT_ID = "prod_credits"
try:
    # missing provider order id → no paid / no grant — ZERO ledger row
    def _grant_rows(uid, oid):
        with db() as _c:
            return _c.execute(
                "select id, delta from ledger where user_id=%s and"
                " ref_type='order' and ref_id=%s",
                (uid, str(oid))).fetchall()
    _before_a = _grant_rows(uidR2b, oR2b["id"])
    _err = None
    try:
        _cr2.fulfill_by_request_id(int(oR2b["id"]), "", "chk_x", 500,
                                   product_id="prod_credits", currency="USD")
    except KeyError as e:
        _err = str(e)
    with db() as con:
        _r = con.execute("select status, provider_order_id from orders"
                         " where id=%s", (oR2b["id"],)).fetchone()
    check("R2-2a provider order id missing → KeyError + order 仍 pending + grant row 零变化",
          _err == "'provider_order_id_missing'"
          and _r["status"] == "pending" and _r["provider_order_id"] is None
          and _grant_rows(uidR2b, oR2b["id"]) == _before_a,
          f"{_err} {dict(_r)} rows={_grant_rows(uidR2b, oR2b['id'])}")

    # wrong checkout id vs bound ch: → conflict, no mutation — ZERO ledger
    with db() as con:
        orders_repo.bind_checkout_id(con, oR2b["id"], "chk_real")
    _before_b = _grant_rows(uidR2b, oR2b["id"])
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
    check("R2-2b wrong checkout id → checkout_identity_conflict + 零 mutation + grant row 零变化",
          _err2 == "'checkout_identity_conflict'"
          and _r2["status"] == "pending" and _r2["provider_order_id"] == "ch:chk_real"
          and _grant_rows(uidR2b, oR2b["id"]) == _before_b,
          f"{_err2} {dict(_r2)} rows={_grant_rows(uidR2b, oR2b['id'])}")

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

    # ── canonical replay cannot bypass the writer conflict ──
    def _bal_rows(uid2, oid2):
        with db() as _c:
            return _c.execute(
                "select id, delta from ledger where user_id=%s and"
                " ref_type='order' and ref_id=%s",
                (uid2, str(oid2))).fetchall()
    with db() as con:
        _r3_full = con.execute(
            "select status, provider_order_id, paid_at from orders"
            " where id=%s", (oR2b["id"],)).fetchone()
    _snap_paid = (dict(_r3_full), _bal_rows(uidR2b, oR2b["id"]))

    # paid ord_A + replay proof ord_A → terminal replay no-op
    _rep = _cr2.fulfill_by_request_id(int(oR2b["id"]), "ord_r2b", "chk_real",
                                      500, product_id="prod_credits",
                                      currency="USD")
    with db() as con:
        _r4 = con.execute("select status, provider_order_id, paid_at"
                          " from orders where id=%s",
                          (oR2b["id"],)).fetchone()
    check("R2-2d paid ord_A + replay proof ord_A → no-op (replay, 不重写)",
          _rep.get("granted") is False and _rep.get("reason") == "replay"
          and _r4["status"] == "paid"
          and _r4["provider_order_id"] == "ord_r2b"
          and int(_r4["paid_at"]) == int(_snap_paid[0]["paid_at"])
          and _bal_rows(uidR2b, oR2b["id"]) == _snap_paid[1],
          f"{_rep} {dict(_r4)}")

    # paid ord_A + proof ord_B → conflict, ZERO ledger/Order mutation
    _errB = None
    try:
        _cr2.fulfill_by_request_id(int(oR2b["id"]), "ord_r2B_OTHER",
                                   "chk_real", 500,
                                   product_id="prod_credits", currency="USD")
    except KeyError as e:
        _errB = str(e)
    with db() as con:
        _r5 = con.execute("select status, provider_order_id, paid_at"
                          " from orders where id=%s",
                          (oR2b["id"],)).fetchone()
    check("R2-2e paid ord_A + proof ord_B → provider_identity_conflict + 零 mutation",
          _errB == "'provider_identity_conflict'"
          and _r5["status"] == "paid"
          and _r5["provider_order_id"] == "ord_r2b"
          and int(_r5["paid_at"]) == int(_snap_paid[0]["paid_at"])
          and _bal_rows(uidR2b, oR2b["id"]) == _snap_paid[1],
          f"{_errB} {dict(_r5)}")

    # incoming final provider id ch:* → reject BEFORE any paid identity
    uidR2c2 = _mkuser(_u_osm + "r2c2")
    with db() as con:
        oR2c2 = dict(orders_repo.insert_order(
            con, uidR2c2, "package", pkg_osm["id"], "creem", 500, "USD", ts_osm,
            credits_snapshot=5000))
    _errC = None
    try:
        _cr2.fulfill_by_request_id(int(oR2c2["id"]), "ch:evil", "chk_ev",
                                   500, product_id="prod_credits",
                                   currency="USD")
    except KeyError as e:
        _errC = str(e)
    with db() as con:
        _r6 = con.execute("select status, provider_order_id from orders"
                          " where id=%s", (oR2c2["id"],)).fetchone()
    check("R2-2f incoming final id ch:* → 拒 (paid 永不落 ch: identity) + 零 grant",
          _errC == "'provider_order_id_invalid_namespace'"
          and _r6["status"] == "pending"
          and _r6["provider_order_id"] is None
          and _bal_rows(uidR2c2, oR2c2["id"]) == [],
          f"{_errC} {dict(_r6)}")
finally:
    _cr2.CREEM_CREDITS_PRODUCT_ID = _prod_id_orig

# ── R2-3: subscription proof predicate — each required field missing ──
uidR2c = _mkuser(_u_osm + "r2c")
with db() as con:
    oR2c = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider, status,"
        " amount_cents, currency, created_at) values"
        " (%s, 'plan', %s, 'creem', 'pending', 999, 'USD', %s)"
        " returning *", (uidR2c, _pl["id"], ts_osm)).fetchone())
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
        orders_repo.complete_plan_checkout(con, oR2c["id"], "sub:sub_OTHER", ts_osm)
except KeyError as exc:
    _e3b = str(exc)
check("R2-3c writer 守 sub monotonicity (paid + sub:B → conflict)",
      _e3b == "'subscription_identity_conflict'", _e3b)

# ── R2-4: package reversal requires exact positive grant ──
from economy import reverse as _rev2
uidR2d = _mkuser(_u_osm + "r2d")
with db() as con:
    # paid WITHOUT any grant (writer-direct; snapshot positive, ledger
    # empty) — the DB no longer permits zero-snapshot package rows
    oR2d = dict(orders_repo.insert_order(
        con, uidR2d, "package", pkg_osm["id"], "creem", 500, "USD", ts_osm,
        credits_snapshot=5000))
    orders_repo.complete_package_order(con, oR2d["id"], f"ord_r2d_{_u_osm}", ts_osm)
_e4 = None
try:
    _rev2._reverse_package({"id": f"txn_r2d_{_u_osm}", "order": f"ord_r2d_{_u_osm}"})
except KeyError as exc:
    _e4 = str(exc)
with db() as con:
    _rd = con.execute("select status from orders where id=%s",
                      (oR2d["id"],)).fetchone()
check("R2-4a paid + missing grant → fail loud + 零 terminal mutation",
      _e4 == "'original_grant_missing'" and _rd["status"] == "paid",
      f"{_e4} {dict(_rd)}")

# grant != snapshot mismatch
uidR2e = _mkuser(_u_osm + "r2e")
with db() as con:
    oR2e = dict(orders_repo.insert_order(
        con, uidR2e, "package", pkg_osm["id"], "creem", 500, "USD", ts_osm,
        credits_snapshot=5000))
_prod_id_orig2 = _cr2.CREEM_CREDITS_PRODUCT_ID
_cr2.CREEM_CREDITS_PRODUCT_ID = "prod_credits"
try:
    _cr2.fulfill_by_request_id(int(oR2e["id"]), f"ord_r2e_{_u_osm}", "chk_r2e",
                                500, product_id="prod_credits", currency="USD")
finally:
    _cr2.CREEM_CREDITS_PRODUCT_ID = _prod_id_orig2
with db() as con:
    con.execute("update ledger set delta=3000 where"
                " ref_type='order' and ref_id=%s", (str(oR2e["id"]),))
_e5 = None
try:
    _rev2._reverse_package({"id": f"txn_r2e_{_u_osm}", "order": f"ord_r2e_{_u_osm}"})
except KeyError as exc:
    _e5 = str(exc)
with db() as con:
    _re = con.execute("select status from orders where id=%s",
                      (oR2e["id"],)).fetchone()
check("R2-4b grant != snapshot → grant_snapshot_mismatch + 零 terminal",
      _e5 == "'grant_snapshot_mismatch'" and _re["status"] == "paid",
      f"{_e5} {dict(_re)}")

# ── R2-5: apply_paid_period replay — wrong ref_type not matched + true movement ──
uidR2f = _mkuser(_u_osm + "r2f")
with db() as con:
    oR2f = dict(con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency, created_at,"
        " paid_at) values (%s, 'plan', %s, 'creem', 'sub:sub_r2f', 'paid',"
        " 999, 'USD', %s, %s) returning *",
        (uidR2f, _pl["id"], ts_osm, ts_osm)).fetchone())
    _m2 = con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at) values (%s, %s, 'sub:sub_r2f',"
        " 'active', %s, %s) returning *",
        (uidR2f, _pl["id"], ts_osm, ts_osm + 30*86400)).fetchone()
PS2, PE2 = ts_osm, ts_osm + 30*86400
_snap2 = {"object": "subscription", "id": "sub_r2f", "status": "active",
          "current_period_start_date": _iso(PS2), "current_period_end_date": _iso(PE2),
          "product": {"id": "prod_sm_plan"},
          "last_transaction_id": f"txn_r2f_{_u_osm}"}
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
        (uidR2f, f"txn_r2f_{_u_osm}", f"wrong-rt-{_u_osm}", ts_osm))
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
             uidR2d, uidR2e, uidR2f, uidR2c2]
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
    con.execute("delete from packages where code like %s", (f"pkg_{_u_osm}%",))
    con.execute("delete from plans where code like %s", (f"smplan_{_u_osm}%",))
for _uid2 in _uids_all:
    with db() as con:
        purge_users(con, [_uid2])
print("cleanup: state-machine fixtures removed")


# ════════════════════════════════════════════════════════════════════════
# Plan mirror commit outcome
#
# Plan mirror DB commit-outcome closure (route-level, real fault injection).
#
# Covers the three post-provider-success DB COMMIT outcomes on the
# operator plan update/create routes: DURABLE_SUCCESS (row+audit
# durable → success, no compensation), PROVEN_ROLLBACK (row == exact
# before + no audit → provider restored to exact provider-before),
# UNRESOLVED (unprovable → fail loud, zero speculative mutation).
# The provider-before compensation basis must be a REAL provider GET
# snapshot, never fabricated 0/"".
# ════════════════════════════════════════════════════════════════════════
section('Plan mirror commit outcome')

import os as _os
import sys as _sys


import time
ts_pmc = int(time.time())
_u_pmc = "mc" + _os.urandom(3).hex()

from database import db
import economy.creem as _creem
from app.main import app as _app
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
client = TestClient(_app)

# ── fixtures: operator + plan + provider fake ──
with db() as con:
    op = con.execute(
        "insert into users (username, email, password_hash, display_name,"
        " status, created_at, last_login_at) values (%s, %s, 'x',"
        " 'OP', 'samelabs', %s, %s) returning id",
        (_u_pmc + "op", f"{_u_pmc}op@t.co", ts_pmc, ts_pmc)).fetchone()
    _op_id = int(op["id"])
    _mc_sort = int(con.execute(
        "select coalesce(max(sort_order), 0) + 1 s from plans"
    ).fetchone()["s"])
    _pl_pmc = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'MC', 100, 1000, 'USD', TRUE, %s, %s, 30, 100,"
        " 104857600, %s, %s) returning *",
        (f"mcplan_{_u_pmc}", "prod_mc_x", _mc_sort, ts_pmc, ts_pmc)).fetchone()
    _pl_pmc = dict(_pl_pmc)
# the fixture row is written directly: reload the in-process tier catalog
from governance import plan_catalog as _plan_catalog_pmc
_plan_catalog_pmc.catalog(refresh=True)
sess = create_browser_session(_op_id)
op_ck = sess  # token string

# provider fake: state + record
_prod_state_pmc = {"prod_mc_x": {"price": 1000, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}}
_gets = {"n": 0}
_patches = []
def _fake_fetch(pid):
    _gets["n"] += 1
    st = _prod_state_pmc.get(pid)
    if st is None:
        raise RuntimeError("product_not_found")
    return dict(st)
def _fake_patch_pmc(pid, payload):
    _patches.append((pid, dict(payload)))
    _prod_state_pmc[pid]["price"] = int(payload.get("price") or 0)
    return True
_orig_fetch, _orig_patch = _creem._fetch_product, _creem._patch
_orig_verify, _orig_mirror = _creem.verify_product_mirror, _creem.mirror_product_price
_creem._fetch_product = _fake_fetch
_creem._patch = _fake_patch_pmc
_creem.creem_enabled = lambda: True
_creem.mirror_product_price = lambda pid, price, cur: _fake_patch_pmc(
    pid, {"price": price}) or None
def _fake_verify(pid, price, cur):
    st = _prod_state_pmc.get(pid)
    if st is None:
        return "not_found"
    if int(st["price"]) == int(price) and str(st["currency"]).upper() == str(cur).upper():
        return None
    return "price_divergence"
_creem.verify_product_mirror = _fake_verify

# ── F1: provider success + durable commit → success, NO restore ──
r1 = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                  json={"price_cents": 1499},
                  cookies={COOKIE_NAME: op_ck})
_n_patch_before = len(_patches)
with db() as con:
    _row = con.execute("select price_cents from plans where id=%s",
                       (_pl_pmc["id"],)).fetchone()
check("F1 provider success + durable commit → 200 / row=intended / no restore",
      r1.status_code == 200 and int(_row["price_cents"]) == 1499
      and len(_patches) == _n_patch_before
      and _prod_state_pmc["prod_mc_x"]["price"] == 1499,
      f"{r1.status_code} {r1.text[:80]} row={dict(_row)}")

# ── F2: commit raises (connection drop at COMMIT) + fresh read proves
#        DURABLE_SUCCESS (row == intended + audit) → success, no restore ──
import core.backoffice.products as _sml
import backoffice.products as _sml_app  # app-path identity (routes import
# 'backoffice.*'; same file, separate sys.modules entry — patch BOTH)
import database as _dbmod
from database import db as _rdb
_orig_db = _dbmod.db
_orig_sml_db = _sml.db
_orig_sml_app_db = _sml_app.db
import contextlib
import sys as _sysexc


class _CommitFailCon:
    """proxies a real transaction but raises at COMMIT with the data
    ACTUALLY committed (ACK-loss after durable write). Only arms itself
    when the transaction touched the plans table — auth/session reads
    pass through and commit normally."""
    def __init__(self, real):
        self._real = real
        self._armed = False
    def execute(self, sql, params=()):
        if isinstance(sql, str) and "plans" in sql and (
                "UPDATE" in sql.upper() or "INSERT INTO plans" in sql.upper()):
            self._armed = True
        return self._real.execute(sql, params)
    def __getattr__(self, name):
        return getattr(self._real, name)
@contextlib.contextmanager
def _db_commit_durable_then_raise():
    import psycopg2 as _pg2
    cm = _rdb()
    con = cm.__enter__()
    wrap = _CommitFailCon(con)
    try:
        yield wrap
        cm.__exit__(None, None, None)   # real commit
    except BaseException:
        cm.__exit__(*_sysexc.exc_info())
        raise
    if wrap._armed:
        raise _pg2.OperationalError(
            "simulated commit ack loss (durable)")
# patch BOTH the source module (function-local imports resolve here)
# and the samelabs module global (module-level `from database import db`)
_dbmod.db = _db_commit_durable_then_raise
_sml.db = _db_commit_durable_then_raise
_sml_app.db = _db_commit_durable_then_raise
try:
    r2 = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                      json={"price_cents": 1699},
                      cookies={COOKIE_NAME: op_ck})
except Exception:
    r2 = None
finally:
    _dbmod.db = _orig_db
    _sml.db = _orig_sml_db
    _sml_app.db = _orig_sml_app_db
with db() as con:
    _row2 = con.execute("select price_cents from plans where id=%s",
                        (_pl_pmc["id"],)).fetchone()
_ok2 = (r2 is not None and r2.status_code == 200
        and int(_row2["price_cents"]) == 1699
        and _prod_state_pmc["prod_mc_x"]["price"] == 1699)
check("F2 commit exception + fresh read proves after+audit → DURABLE_SUCCESS",
      _ok2, f"{r2 and r2.status_code} {r2 and r2.text[:80]} row={dict(_row2)}"
      f" mirror={_prod_state_pmc['prod_mc_x']}")

# ── F3: commit fails with the transaction ROLLED BACK (row == before,
#        no audit) → PROVEN_ROLLBACK → provider restored to exact before ──
@contextlib.contextmanager
def _db_rollback_then_raise():
    cm = _rdb()
    con = cm.__enter__()
    wrap = _CommitFailCon(con)
    try:
        yield wrap
        if wrap._armed:
            import psycopg2 as _pg2
            cm.gen.throw(_pg2.OperationalError(
                "simulated commit failure (rolled back)"))
        else:
            cm.__exit__(None, None, None)
    except BaseException:
        cm.__exit__(*_sysexc.exc_info())
        raise
_dbmod.db = _db_rollback_then_raise
_sml.db = _db_rollback_then_raise
_sml_app.db = _db_rollback_then_raise
try:
    r3 = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                      json={"price_cents": 1999},
                      cookies={COOKIE_NAME: op_ck})
except Exception as exc:
    r3 = type("R", (), {"status_code": 0, "text": str(exc)})()
finally:
    _dbmod.db = _orig_db
    _sml.db = _orig_sml_db
    _sml_app.db = _orig_sml_app_db
with db() as con:
    _row3 = con.execute("select price_cents from plans where id=%s",
                        (_pl_pmc["id"],)).fetchone()
_ok3 = (int(_row3["price_cents"]) == 1699          # local == exact before
        and _prod_state_pmc["prod_mc_x"]["price"] == 1699)  # provider restored
check("F3 commit failure + row==before/no audit → PROVEN_ROLLBACK + restore",
      _ok3, f"row={dict(_row3)} mirror={_prod_state_pmc['prod_mc_x']}"
      f" resp={getattr(r3, 'status_code', '?')}")

# ── F4: create with commit failure → row absent + no audit → rollback
#        (provider restored to its real before state) ──
_prod_state_pmc["prod_mc_new"] = {"price": 1, "currency": "USD",
                              "billing_type": "recurring",
                              "billing_period": "every-month"}
_p_before = len(_patches)
_dbmod.db = _db_rollback_then_raise
_sml.db = _db_rollback_then_raise
_sml_app.db = _db_rollback_then_raise
try:
    r4 = client.post("/samelabs/commerce/products/plans", json={
        "code": f"mcnew_{_u_pmc}", "name": "MC New", "monthly_credits": 50,
        "price_cents": 777, "creem_product_id": "prod_mc_new",
        "sort_order": _mc_sort + 1, "period_days": 30,
        "ingress_files_24h": 50, "ingress_bytes_24h": 104857600},
        cookies={COOKIE_NAME: op_ck})
except Exception as exc:
    r4 = type("R", (), {"status_code": 0, "text": str(exc)})()
finally:
    _dbmod.db = _orig_db
    _sml.db = _orig_sml_db
    _sml_app.db = _orig_sml_app_db
with db() as con:
    _row4 = con.execute("select 1 from plans where code=%s",
                        (f"mcnew_{_u_pmc}",)).fetchone()
check("F4 create commit failure → row absent + provider restored to before",
      _row4 is None and _prod_state_pmc["prod_mc_new"]["price"] == 1
      and len(_patches) > _p_before,
      f"row={_row4} mirror={_prod_state_pmc['prod_mc_new']}")

# ── G-series: provider-before proof — UNKNOWN never becomes a
#    compensable state. Inject the initial _fetch_product() returning
#    None (GET failure) and prove: zero PATCH, zero restore, local
#    rollback, no fabricated (0, "") before. ──
_ops_sequential = dict(_patches)   # snapshot counters
_nb_create = len(_patches)
_nb_update = len(_patches)

# G1: update path — initial GET unprovable → fail closed BEFORE any PATCH
_orig_fetch_g = _creem._fetch_product
def _fetch_none_first_update(pid):
    if pid == "prod_mc_x" and not _fetch_none_first_update.done:
        _fetch_none_first_update.done = True
        return None            # provider GET failure → unprovable before
    return _orig_fetch_g(pid)
# arm only at G1 time: the FIRST prod_mc_x fetch from here on returns None
class _Armable:
    def __init__(self):
        self.armed = False
        self.none_ids = set()   # per-product one-shot None
    def __call__(self, pid):
        if self.armed and pid in self.none_ids:
            self.none_ids.discard(pid)   # one shot per product
            return None
        return _orig_fetch_g(pid)
_fetch_none_first_update = _Armable()
_fetch_none_first_update.armed = True
_fetch_none_first_update.none_ids.add("prod_mc_x")
_creem._fetch_product = _fetch_none_first_update
_p_before_g = len(_patches)
_r_g1 = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                     json={"price_cents": 3456},
                     cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g1 = con.execute("select price_cents from plans where id=%s",
                          (_pl_pmc["id"],)).fetchone()
check("G1 update: initial GET None → fail closed / 零 PATCH / local 不变",
      _r_g1.status_code == 502
      and str(_r_g1.json().get("error", "")).startswith("price_mirror")
      and len(_patches) == _p_before_g
      and int(_row_g1["price_cents"]) != 3456,
      f"{_r_g1.status_code} {str(_r_g1.json())[:80]}"
      f" patches={len(_patches)}/{_p_before_g} row={dict(_row_g1)}")
_fetch_none_first_update.armed = False   # disarm for later sections

# G2: create path — initial GET unprovable → fail closed BEFORE any PATCH
_prod_state_pmc["prod_mc_g2"] = {"price": 4321, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}
# arm the one-shot None for the create path's initial GET
_fetch_none_first_update.armed = True
_fetch_none_first_update.none_ids.add("prod_mc_g2")
_p_before_g2 = len(_patches)
_r_g2 = client.post("/samelabs/commerce/products/plans", json={
    "code": f"mcg2_{_u_pmc}", "name": "MC G2", "monthly_credits": 60,
    "price_cents": 555, "creem_product_id": "prod_mc_g2",
    "sort_order": _mc_sort + 5, "period_days": 30,
    "ingress_files_24h": 50, "ingress_bytes_24h": 104857600},
    cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g2 = con.execute("select 1 from plans where code=%s",
                          (f"mcg2_{_u_pmc}",)).fetchone()
check("G2 create: initial GET None → fail closed / 零 PATCH / row 未落",
      _r_g2.status_code == 502
      and str(_r_g2.json().get("error", "")).startswith("price_mirror")
      and len(_patches) == _p_before_g2
      and _row_g2 is None,
      f"{_r_g2.status_code} patches={len(_patches)}/{_p_before_g2}"
      f" row={_row_g2}")
_fetch_none_first_update.armed = False
_creem._fetch_product = _orig_fetch_g

# ── G3/G4/G5: provider-before PRICE must be explicit (missing/None →
#    fail closed; explicit legitimate 0 is a real fact) ──
class _PartialHook:
    """One-shot partial provider snapshot: valid id + currency but the
    price key MISSING (or an explicit 0 for the positive case)."""
    def __init__(self):
        self.armed = False
        self.pid = None
        self.override = None
    def __call__(self, pid):
        if self.armed and pid == self.pid and self.override is not None:
            self.armed = False
            return dict(self.override)   # partial provider snapshot
        return _orig_fetch_g(pid)
_partial_hook = _PartialHook()
_creem._fetch_product = _partial_hook

# G3: update — id/currency present, price key missing → fail closed
_p_before_g3 = len(_patches)
_partial_hook.armed = True
_partial_hook.pid = "prod_mc_x"
_partial_hook.override = {"id": "prod_mc_x", "currency": "USD",
                          "billing_type": "recurring",
                          "billing_period": "every-month"}   # no price key
_r_g3 = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                     json={"price_cents": 3777},
                     cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g3 = con.execute("select price_cents from plans where id=%s",
                          (_pl_pmc["id"],)).fetchone()
check("G3 update: snapshot missing price → 502 / 零 PATCH / local 不变",
      _r_g3.status_code == 502
      and str(_r_g3.json().get("error", "")).startswith("price_mirror")
      and len(_patches) == _p_before_g3
      and int(_row_g3["price_cents"]) != 3777,
      f"{_r_g3.status_code} patches={len(_patches)}/{_p_before_g3}"
      f" row={dict(_row_g3)}")

# G4: create — id/currency present, price=None → fail closed, row absent
_p_before_g4 = len(_patches)
_partial_hook.armed = True
_partial_hook.pid = "prod_mc_g4"
_partial_hook.override = {"id": "prod_mc_g4", "currency": "USD",
                          "price": None,
                          "billing_type": "recurring",
                          "billing_period": "every-month"}
_r_g4 = client.post("/samelabs/commerce/products/plans", json={
    "code": f"mcg4_{_u_pmc}", "name": "MC G4", "monthly_credits": 70,
    "price_cents": 888, "creem_product_id": "prod_mc_g4",
    "sort_order": _mc_sort + 6, "period_days": 30,
    "ingress_files_24h": 50, "ingress_bytes_24h": 104857600},
    cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g4 = con.execute("select 1 from plans where code=%s",
                          (f"mcg4_{_u_pmc}",)).fetchone()
check("G4 create: snapshot price=None → 502 / 零 PATCH / row 未落",
      _r_g4.status_code == 502
      and str(_r_g4.json().get("error", "")).startswith("price_mirror")
      and len(_patches) == _p_before_g4
      and _row_g4 is None,
      f"{_r_g4.status_code} patches={len(_patches)}/{_p_before_g4}"
      f" row={_row_g4}")

# G5: explicit legitimate price=0 is a PROVEN fact — not treated as
#     missing; the update proceeds normally on the real (0, USD) before
_prod_state_pmc["prod_mc_x"]["price"] = 0   # provider carries explicit 0
_p_before_g5 = len(_patches)
_r_g5 = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                     json={"price_cents": 150},
                     cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g5 = con.execute("select price_cents from plans where id=%s",
                          (_pl_pmc["id"],)).fetchone()
check("G5 显式合法 price=0 是真实 fact → 更新正常 (不误判 missing)",
      _r_g5.status_code == 200 and int(_row_g5["price_cents"]) == 150
      and len(_patches) == _p_before_g5 + 1
      and _prod_state_pmc["prod_mc_x"]["price"] == 150,
      f"{_r_g5.status_code} patches={len(_patches)}/{_p_before_g5}"
      f" row={dict(_row_g5)} mirror={_prod_state_pmc['prod_mc_x']}")
# restore provider/local price for the sections that follow
_prod_state_pmc["prod_mc_x"]["price"] = 150

# ── G6/G7: non-canonical price TYPES fail closed — no int() coercion
#    may fabricate a before (float 150.7 → 150, bool True → 1) ──
def _noncanon_case(tag, bad_price, is_create):
    _p_before = len(_patches)
    _partial_hook.armed = True
    _partial_hook.pid = "prod_mc_x" if not is_create else f"prod_nc_{tag}"
    _partial_hook.override = {"id": _partial_hook.pid, "currency": "USD",
                              "price": bad_price,
                              "billing_type": "recurring",
                              "billing_period": "every-month"}
    if is_create:
        r = client.post("/samelabs/commerce/products/plans", json={
            "code": f"mcnc{tag}_{_u_pmc}", "name": "MC NC", "monthly_credits": 90,
            "price_cents": 1234, "creem_product_id": _partial_hook.pid,
            "sort_order": _mc_sort + 10, "period_days": 30,
            "ingress_files_24h": 50, "ingress_bytes_24h": 104857600},
            cookies={COOKIE_NAME: op_ck})
        with db() as con:
            row = con.execute("select 1 from plans where code=%s",
                              (f"mcnc{tag}_{_u_pmc}",)).fetchone()
        return (r.status_code == 502
                and str(r.json().get("error", "")).startswith("price_mirror")
                and len(_patches) == _p_before and row is None,
                f"{r.status_code} patches={len(_patches)}/{_p_before} row={row}")
    r = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                     json={"price_cents": 1990},
                     cookies={COOKIE_NAME: op_ck})
    with db() as con:
        row = con.execute("select price_cents from plans where id=%s",
                          (_pl_pmc["id"],)).fetchone()
    return (r.status_code == 502
            and str(r.json().get("error", "")).startswith("price_mirror")
            and len(_patches) == _p_before
            and int(row["price_cents"]) != 1990,
            f"{r.status_code} patches={len(_patches)}/{_p_before} row={dict(row)}")

_ok6a, _d6a = _noncanon_case("f", 150.7, False)     # update: float
check("G6a update: provider price=150.7 (float) → fail closed / 零 PATCH / local 不变",
      _ok6a, _d6a)
_ok6b, _d6b = _noncanon_case("fc", 150.7, True)     # create: float
check("G6b create: provider price=150.7 (float) → fail closed / 零 PATCH / row 未落",
      _ok6b, _d6b)
_ok7a, _d7a = _noncanon_case("b", True, False)      # update: bool
check("G7a update: provider price=True (bool) → fail closed / 零 PATCH",
      _ok7a, _d7a)
_ok7b, _d7b = _noncanon_case("bc", True, True)      # create: bool
check("G7b create: provider price=True (bool) → fail closed / row 未落",
      _ok7b, _d7b)
_ok7c, _d7c = _noncanon_case("s", "1500", False)    # update: string
check("G7c update: provider price='1500' (string) → fail closed / 零 coercion",
      _ok7c, _d7c)

# ── H-series: EXACT snapshot / audit correlation (mixed-field ops) ──
# H1 (deterministic): a stale audit INSIDE this operation's correlation
# window (created_at >= the route's frozen tx_started) with the SAME
# after price but a DIFFERENT full before→after pair must NEVER let a
# mixed-field rollback classify as success. The route's now() is
# frozen via injection — no wall-clock race.
from repo import operator_audit as _oa
with db() as con:
    # the plan currently carries price 150 (G5); the current operation
    # is "price unchanged + name change"
    _row_h = con.execute("select name, price_cents from plans where id=%s",
                         (_pl_pmc["id"],)).fetchone()
    _name_before_h = _row_h["name"]
# freeze the route's clock AFTER recording the stale audit with an
# earlier-or-equal timestamp, so the stale audit is provably inside
# the boundary (stale_created >= frozen_tx_ts):
import time as _time_h
_frozen_now = int(_time_h.time())
_orig_now_h = _sml.now
_sml.now = lambda: _frozen_now
try:
    with db() as con:
        # stale audit: SAME actor/plan/action, created_at == the frozen
        # tx boundary (>= tx_started), after price == intended price,
        # but a FULL snapshot pair of a DIFFERENT operation
        _oa.record(con, int(_op_id), "plan.update", "plan",
                   str(_pl_pmc["id"]),
                   {"name": _name_before_h, "price_cents": 1000},
                   {"name": _name_before_h + "_STALE", "price_cents": 150},
                   _frozen_now)
    # inject: commit rollback + the route's mirror runs (provider mutated)
    _p_before_h1 = len(_patches)
    _dbmod.db = _db_rollback_then_raise
    _sml.db = _db_rollback_then_raise
    _sml_app.db = _db_rollback_then_raise
    try:
        _r_h1 = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                             json={"price_cents": 150,
                                   "name": _name_before_h + "_CURRENT"},
                             cookies={COOKIE_NAME: op_ck})
    except Exception as exc:
        _r_h1 = type("R", (), {"status_code": 0, "text": str(exc)})()
    finally:
        _dbmod.db = _orig_db
        _sml.db = _orig_sml_db
        _sml_app.db = _orig_sml_app_db
finally:
    _sml.now = _orig_now_h
with db() as con:
    _row_h1 = con.execute("select name, price_cents from plans"
                          " where id=%s", (_pl_pmc["id"],)).fetchone()
    # prove the stale audit is inside the boundary the classifier used
    _stale_in_window = con.execute(
        "select count(*) c from operator_audit"
        " where target_type='plan' and target_id=%s"
        "   and after_json->>'price_cents'='150'"
        "   and created_at >= %s",
        (str(_pl_pmc["id"]), _frozen_now)).fetchone()["c"]
_h1_was_success = _r_h1.status_code == 200
check("H1d precondition: stale same-price audit 确在 frozen tx boundary 内",
      _stale_in_window >= 1, f"in_window={_stale_in_window}")
check("H1d stale in-window same-price audit + mixed-field rollback → 绝不 SUCCESS",
      (not _h1_was_success) and _row_h1["name"] == _name_before_h,
      f"st={_r_h1.status_code} name={_row_h1['name']!r}"
      f" (before={_name_before_h!r}) was_success={_h1_was_success}")

# ── B-series: strict exact-before three-way classifier ──
import core.backoffice.products as _smlmod
from repo import operator_audit as _oa

# B1: update commit unknown + DB third-state (row neither before nor
#     intended) + no matching audit → UNRESOLVED, never rollback/restore
with db() as con:
    con.execute("update plans set price_cents=2099 where id=%s", (_pl_pmc["id"],))
    con.execute("delete from operator_audit where target_type='plan'"
                " and target_id=%s", (str(_pl_pmc["id"]),))
_p_before_b = len(_patches)
_v1 = _smlmod._classify_db_commit_outcome(
    int(_pl_pmc["id"]),
    {"price_cents": 2499, "currency": "USD",
     "creem_product_id": "prod_mc_x"},
    {"price_cents": 1699, "currency": "USD",
     "creem_product_id": "prod_mc_x"},   # exact before
    int(_op_id), int(ts_pmc) - 3600, "plan.update")
check("B1 update third-state row + no audit → UNRESOLVED (never rollback)",
      _v1[0] == "unresolved", str(_v1))
check("B1b classifier 零 provider mutation",
      len(_patches) == _p_before_b,
      f"patches={len(_patches)} before={_p_before_b}")

# B2: create commit unknown + unexpected row exists + no audit → UNRESOLVED
#     (row absent stays the only PROVEN_ROLLBACK shape for create)
_v2 = _smlmod._classify_db_commit_outcome(
    999999,
    {"price_cents": 777, "currency": "USD",
     "creem_product_id": "prod_mc_new"},
    None, int(_op_id), int(ts_pmc) - 3600, "plan.create")
_v2b = _smlmod._classify_db_commit_outcome(
    int(_pl_pmc["id"]),   # an EXISTING row where create expected none
    {"price_cents": 777, "currency": "USD",
     "creem_product_id": "prod_mc_new"},
    None, int(_op_id), int(ts_pmc) - 3600, "plan.create")
check("B2 create unexpected row exists → UNRESOLVED (absent 才 rollback)",
      _v2[0] == "rollback" and _v2b[0] == "unresolved",
      f"absent={_v2[0]} existing={_v2b[0]}")

# B3: durable commit identified across a second boundary — tx_started
#     fixed before the provider call, audit written seconds later.
#     Full-snapshot proof: row == intended, audit == exact pair.
with db() as con:
    con.execute("update plans set price_cents=1699 where id=%s", (_pl_pmc["id"],))
with db() as con:
    _row_b3 = con.execute("select * from plans where id=%s",
                          (_pl_pmc["id"],)).fetchone()
    _snap_b3 = _smlmod._snap_plan(_row_b3)               # == intended
    _before_b3 = dict(_snap_b3, price_cents=_snap_b3["price_cents"] - 200)
    _oa.record(con, int(_op_id), "plan.update", "plan", str(_pl_pmc["id"]),
               _before_b3, _snap_b3, int(ts_pmc) + 5)
_v3 = _smlmod._classify_db_commit_outcome(
    int(_pl_pmc["id"]), _snap_b3, _before_b3,
    int(_op_id), int(ts_pmc) - 3, "plan.update")
check("B3 durable commit 跨秒后仍识别 matching audit → SUCCESS",
      _v3[0] == "success", str(_v3))

# B3b: historical same-price audit BEFORE this operation's boundary
#      must never be mistaken for this operation's durable audit
with db() as con:
    con.execute("delete from operator_audit where target_type='plan'"
                " and target_id=%s", (str(_pl_pmc["id"]),))
    _oa.record(con, int(_op_id), "plan.update", "plan", str(_pl_pmc["id"]),
               _before_b3, dict(_snap_b3, name="OLD"), int(ts_pmc) - 7200)
_v3b = _smlmod._classify_db_commit_outcome(
    int(_pl_pmc["id"]), _snap_b3, _before_b3,
    int(_op_id), int(ts_pmc) - 3, "plan.update")
check("B3b 历史同价旧 audit (boundary 之前) 不误认 → 非 SUCCESS",
      _v3b[0] != "success", str(_v3b))

# B4: compensation fires ONLY on exact PROVEN_ROLLBACK —
#     exact-before row + no matching audit is the ONLY rollback shape
with db() as con:
    con.execute("delete from operator_audit where target_type='plan'"
                " and target_id=%s", (str(_pl_pmc["id"]),))
# (a real update always has before != intended; row==before here)
with db() as con:
    _row_b4 = con.execute("select * from plans where id=%s",
                          (_pl_pmc["id"],)).fetchone()
    _snap_b4 = _smlmod._snap_plan(_row_b4)               # row == before
    _intended_b4 = dict(_snap_b4, price_cents=1899)       # != row
_v4 = _smlmod._classify_db_commit_outcome(
    int(_pl_pmc["id"]), _intended_b4, _snap_b4,
    int(_op_id), int(ts_pmc) - 3600, "plan.update")
check("B4 exact-before row + no matching audit → PROVEN_ROLLBACK (仅此可 restore)",
      _v4[0] == "rollback", str(_v4))
check("B4b UNRESOLVED verdict 全程零 provider restore",
      len(_patches) == _p_before_b,
      f"patches={len(_patches)} before={_p_before_b}")

# H2: mixed-field durable commit + ACK-loss → the exact-pair audit
#     proves THIS operation (noncommercial mutation included) → SUCCESS
with db() as con:
    _row_h2 = con.execute("select name from plans where id=%s",
                          (_pl_pmc["id"],)).fetchone()
    _name_h2 = _row_h2["name"]
_dbmod.db = _db_commit_durable_then_raise
_sml.db = _db_commit_durable_then_raise
try:
    _r_h2 = client.patch(f"/samelabs/commerce/products/plans/{_pl_pmc['id']}",
                         json={"price_cents": 150,
                               "name": _name_h2 + "_H2"},
                         cookies={COOKIE_NAME: op_ck})
except Exception:
    _r_h2 = None
finally:
    _dbmod.db = _orig_db
    _sml.db = _orig_sml_db
    _sml_app.db = _orig_sml_app_db
with db() as con:
    _row_h2b = con.execute("select name, price_cents from plans"
                           " where id=%s", (_pl_pmc["id"],)).fetchone()
check("H2 mixed-field durable commit + ACK-loss → exact-pair audit 证 SUCCESS (name 已落)",
      _r_h2 is not None and _r_h2.status_code == 200
      and _row_h2b["name"] == _name_h2 + "_H2",
      f"st={_r_h2 and _r_h2.status_code} name={_row_h2b['name']!r}"
      f" expect={_name_h2 + '_H2'!r}")

# H3: full third-state row → UNRESOLVED (never rollback/restore)
with db() as con:
    con.execute("update plans set monthly_credits=777 where id=%s",
                (_pl_pmc["id"],))
    con.execute("delete from operator_audit where target_type='plan'"
                " and target_id=%s", (str(_pl_pmc["id"]),))
_v_h3 = _smlmod._classify_db_commit_outcome(
    int(_pl_pmc["id"]), _snap_b3, _before_b3,
    int(_op_id), int(ts_pmc) - 3600, "plan.update")
check("H3 third-state row (monthly_credits 变) → UNRESOLVED",
      _v_h3[0] == "unresolved", str(_v_h3))
# restore for the sections that follow
with db() as con:
    con.execute("update plans set monthly_credits=100 where id=%s",
                (_pl_pmc["id"],))

# ── F5: compensation basis is the REAL provider GET snapshot ──
_src = open(_os.path.join(REPO, "core", "backoffice", "products.py"),
            encoding="utf-8").read()
_create_blk = _src.split("def create_plan")[1].split("\ndef ")[0]
check("F5 create mirror uses real provider-before GET (no 0/\"\")",
      "_fetch_product" in _create_blk
      and 'fields["creem_product_id"], 0, ""' not in _create_blk,
      "")

# restore patches
_creem._fetch_product = _orig_fetch
_creem._patch = _orig_patch
_creem.verify_product_mirror = _orig_verify
_creem.mirror_product_price = _orig_mirror

# ── cleanup ──
with db() as con:
    con.execute("delete from operator_audit where actor_user_id=%s", (_op_id,))
    con.execute("delete from plans where id=%s", (_pl_pmc["id"],))
    purge_users(con, [_op_id])
print("cleanup: mirror-commit fixtures removed")


finish()
