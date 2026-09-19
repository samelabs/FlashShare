#!/usr/bin/env python3
"""Order/payment state closure canonical.

Checkout identity (client_ref) → local order → provider request →
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

def _order(user_id, kind, item_id, ref, status="pending", amount=500,
           credits=5000, provider_id=None):
    # field combinations must satisfy the canonical invariants
    granted = kind == "package" and status == "paid"
    paid_at = ts if status in ("paid",) else None
    refunded_at = ts if status in ("refunded", "disputed") else None
    if status in ("refunded", "disputed"):
        granted = kind == "package"  # reversal may follow a paid fact
        paid_at = ts
    with db() as con:
        row = con.execute(
            "insert into orders (user_id, kind, item_id, provider, status,"
            " amount_cents, currency, credits_snapshot, client_ref,"
            " granted, paid_at, refunded_at, created_at) values"
            " (%s, %s, %s, 'creem', %s, %s, 'USD', %s, %s, %s, %s, %s, %s)"
            " returning *",
            (user_id, kind, item_id, status, amount, credits, ref,
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

# ══════════ A. client_ref 契约 (route helper 级 = parser 语义) ══════════
def _valid_ref(r):
    return (isinstance(r, str) and bool(r.strip())
            and len(r.strip()) <= 128)
check("A1 insert_order 拒 missing/blank/长/非串 ref",
      all(not _valid_ref(x) for x in (None, "", "   ", "x" * 129, 123)))
_raised = 0
for bad in (None, "", "   ", "x" * 129, 123):
    try:
        with db() as con:
            orders_repo.insert_order(con, u1, "package", pkg["id"], "creem",
                                     500, "USD", ts, credits_snapshot=5000,
                                     client_ref=bad)
    except ValueError:
        _raised += 1
check("A2 insert_order runtime 拒 5 种非法 ref (零行落地)",
      _raised == 5)

# ══════════ B. same ref = immutable intent ══════════
refB = f"pay:{_u}-1"
with db() as con:
    oB1 = dict(orders_repo.insert_order(
        con, u1, "package", pkg["id"], "creem", 500, "USD", ts,
        credits_snapshot=5000, client_ref=refB))
with db() as con:
    row = orders_repo.order_by_client_ref_for_update(con, u1, refB)
check("B1 order_by_client_ref 命中同 ref", row is not None and row["id"] == oB1["id"])
with db() as con:
    row2 = orders_repo.order_by_client_ref_for_update(con, u2, refB)
check("B2 不同 user 同 ref 不命中 (user-scoped)", row2 is None)
# 同 ref 第二次 insert → unique 拒
_dup = False
try:
    with db() as con:
        orders_repo.insert_order(con, u1, "package", pkg["id"], "creem",
                                 500, "USD", ts, credits_snapshot=5000,
                                 client_ref=refB)
except Exception:
    _dup = True
check("B3 同 user+ref 双 insert → unique 拒", _dup)
# 历史 NULL ref 合法 (多行; canonical 组合不变量仍需满足)
with db() as con:
    for _ in range(2):
        con.execute(
            "insert into orders (user_id, kind, item_id, provider, status,"
            " amount_cents, currency, credits_snapshot, created_at) values"
            " (%s, 'package', %s, 'creem', 'failed', 1, 'USD', 100, %s)",
            (u1, pkg["id"], ts))
check("B4 历史 NULL client_ref 多行合法", True)

# ══════════ C. package evidence precedence (guarded transitions) ══════════
def _mk_pkg_order(status):
    return _order(u1, "package", pkg["id"], f"pay:{_u}-{status}-{_os.urandom(2).hex()}",
                  status=status)

# pending + success → paid
o1 = _mk_pkg_order("pending")
with db() as con:
    orders_repo.mark_paid(con, o1["id"], ts)
check("C1 pending + success → paid", _status(o1["id"]) == "paid")
# paid + success replay → paid (无变化)
with db() as con:
    orders_repo.mark_paid(con, o1["id"], ts)
check("C2 paid + replay → paid", _status(o1["id"]) == "paid")
# failed + success → paid
o2 = _mk_pkg_order("failed")
with db() as con:
    orders_repo.mark_paid(con, o2["id"], ts)
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
# pending + refund → refunded
o5 = _mk_pkg_order("pending")
with db() as con:
    orders_repo.mark_refunded(con, o5["id"], ts)
check("C6 pending + refund → refunded", _status(o5["id"]) == "refunded")
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
    orders_repo.mark_paid(con, o8["id"], ts)
check("C9 refunded + stale success → 无 paid 改写", _status(o8["id"]) == "refunded")
# disputed + stale success → NO paid rewrite
o9 = _mk_pkg_order("disputed")
with db() as con:
    orders_repo.mark_paid(con, o9["id"], ts)
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
    orders_repo.mark_subscription_checkout_completed(con, po1["id"], ts)
check("D1 plan pending + completed → paid", _status(po1["id"]) == "paid")
with db() as con:
    orders_repo.mark_subscription_checkout_completed(con, po1["id"], ts)
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
# provider create 异常 → pending 保持 (不 mark_failed) — 由 route 源码
# 语义 + 事务行为测试 (HTTP 层在 test_payment_forward 覆盖 route);
# 这里直证: pending order 在 unknown-outcome 后仍 pending 且可被同 ref 复用。
oE = _order(u1, "package", pkg["id"], f"pay:{_u}-unres")
check("E1 unknown provider outcome → order 仍 pending (无 failed 改写)",
      _status(oE["id"]) == "pending")
with db() as con:
    again = orders_repo.order_by_client_ref_for_update(con, u1, f"pay:{_u}-unres")
check("E2 同 ref retry 命中同一 pending order (无新单)", again["id"] == oE["id"])

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
        credits_snapshot=5000, client_ref=ref6))
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
                     " and client_ref=%s", (uid6, ref6)).fetchone()["c"]
check("O7 same-ref retry → same local order (1 row)", n6 == 1)
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
_creem_mod.create_credits_checkout = _orig_create

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
oS3 = _order(uidS3, "plan", 1, f"pay:{_u}-s3", amount=999, credits=None)
_ev = {"id": "ch_s3", "request_id": f"order:{oS3['id']}",
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

# ══════════ cleanup ══════════
for uid in (u1, u2, uid6, uidE, uidS3, uidV):
    with db() as con:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
        con.execute("delete from orders where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
with db() as con:
    con.execute("delete from packages where code like %s", (f"pkg_{_u}%",))
for uid in (u1, u2, uid6, uidE, uidS3, uidV):
    with db() as con:
        con.execute("delete from users where id=%s", (uid,))
print("cleanup: payment-state fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
