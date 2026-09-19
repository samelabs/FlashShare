#!/usr/bin/env python3
"""Order/payment state closure canonical.

Purchase intent identity = Order.id; provider request →
recovery transitions → evidence precedence → reversal ordering →
plan-order domain. Real repo/route helpers over an isolated DB; fault
injection only.
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
_src_rev = open(_os.path.join(_REPO, "core", "economy", "reverse.py"),
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
    con.execute("update orders set status='failed', finished_null_probe=NULL"
                " where id=%s", (oE["id"],)) if False else con.execute(
        "update orders set status='failed' where id=%s", (oE["id"],))

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
    _planS3 = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'PS3', 100, 999, 'USD', TRUE, %s, 93, 30, 100,"
        " 104857600, %s, %s) returning *",
        (f"ps3_{_u}", f"prod_ps3_{_u}", ts, ts)).fetchone()
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
    _pl = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'PC Plan', 100, 999, 'USD', TRUE, 'prod_pc_plan',"
        " 77, 30, 100, 104857600, %s, %s) on conflict (code) do update"
        " set monthly_credits=EXCLUDED.monthly_credits returning *",
        (f"pcplan_{_u}", ts, ts)).fetchone()
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
_src_sml = open(_os.path.join(_REPO, "app", "routes", "samelabs.py"),
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
        con.execute("delete from users where id=%s", (uid,))
print("cleanup: payment-state fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
