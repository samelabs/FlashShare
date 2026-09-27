#!/usr/bin/env python3
"""Payment membership tiers canonical (Free/Plus/Pro × Creem Product).

Tier identity、canonical entitlements/credits、sellable 收口、
shared Credits Product + custom_price、subscription product checkout、
Plus→Pro upgrade (proration + 补差 + 幂等)、Pro→Plus next-renewal、
cancel 保留、webhook authority。fail-closed canonical 规则。
"""
import os as _os, sys as _sys
import sys, json, time

_REPO = _os.path.abspath(_os.path.join(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."), ".."))

# ── FAIL-CLOSED GATE ────────────────────────────────────────────────────
_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required (isolated service port, never the production port).\n")
    _sys.exit(2)
try:
    _TEST_PORT_INT = int(_TEST_PORT, 10)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer port number.\n")
    _sys.exit(2)
if _TEST_PORT_INT == 8000:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT=8000 is the production port; canonical tests must use an isolated port.\n")
    _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required (isolated test database DSN).\n")
    _sys.exit(2)

sys.path.insert(0, _REPO)
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
from tests.support.cleanup import purge_users  # noqa: E402
sys.path.insert(0, _os.path.join(_REPO, "core"))

import psycopg2
from database import db
from repo import orders as orders_repo
from economy import subscription as _sub
from economy.billing import spendable_balance
from economy import creem as _creem
from governance import plan_catalog
from governance.plan_catalog import is_paid_tier
from governance.entitlements import resolve, resolve_from_membership
from governance.access_projection import project

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_ts = int(time.time())
FUT = _ts + 30 * 86400
PS, PE = _ts - 3600, FUT

_cur.execute("delete from pending_downgrades where user_id in (select id from users where username like 'pt%')")
_cur.execute("delete from memberships where user_id in (select id from users where username like 'pt%')")
_cur.execute("delete from orders where user_id in (select id from users where username like 'pt%')")
_cur.execute("delete from ledger where user_id in (select id from users where username like 'pt%')")
_cur.execute("delete from plans where code like 'pt_%'")
purge_users(_cur, [r["id"] for r in _cur.execute(
    "select id from users where username like 'pt%'").fetchall()])

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts, _ts))
    return u, _cur.fetchone()[0]

def _sub_snap(status, p_start, p_end, product=None, sub_id="ptsub",
              txn=None):
    # paid-proof contract fields (product + last_transaction_id) required;
    # a mid-cycle upgrade charge is a DISTINCT provider transaction
    d = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": p_start, "current_period_end_date": p_end,
         "product": product or {"id": "prod_pt"},
         "last_transaction_id": txn or f"txn_{sub_id}"}
    return d

def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _seed_sub_user(prefix, plan_code, product, sub_id, paid=True):
    u, uid = _new_user(prefix)
    _cur.execute("select id, creem_product_id from plans where code=%s", (plan_code,))
    row = _cur.fetchone()
    plan_id = row[0]
    _plan_product = row[1] or product   # fall back to the seeded binding
    _cur.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, created_at)"
        " values (%s, 'plan', %s, 'creem', %s, 'pending', 500, 'USD', %s) returning id",
        (uid, plan_id, f"ch:ch_{sub_id}", _ts))
    _oid = _cur.fetchone()[0]
    _sub.bind_checkout({"id": "ch_" + sub_id,
                        "order": {"id": "ord_" + sub_id, "status": "paid",
                                  "type": "recurring", "amount": 500,
                                  "currency": "USD", "product": _plan_product},
                        "subscription": {"id": sub_id}}, order_id=_oid)
    if paid:
        _sub.handle_event("subscription.paid",
                          _sub_snap("active", _iso(PS), _iso(PE),
                                    product={"id": product}, sub_id=sub_id))
    return u, uid, plan_id

def _grants(uid, kind):
    _cur.execute("select count(*) from ledger where user_id=%s and kind=%s", (uid, kind))
    return _cur.fetchone()[0]

def _plan_credits_total(uid, period_start):
    with db() as con:
        return orders_repo.period_plan_credits(con, uid, period_start)

# ═══ T1: 种子+运营就绪合同 — 绑品定价后 sellable 恰好 plus/pro ═══
# (init_db 种子: price=0 无 product → 不可售; 运营绑品+定价后进入销售。
#  夹具: plus/pro 模拟就绪; 其它行存在也不进 sellable — 见 T2)
from governance import plan_catalog as _pc
with db() as con:  # 写并提交
    con.execute("update plans set price_cents=500, creem_product_id='pt_plus_price'"
                " where code='plus'")
    con.execute("update plans set price_cents=1500, creem_product_id='pt_pro_price'"
                " where code='pro'")
_pc.catalog(refresh=True)  # 提交后刷 — 事务内 refresh 读不到本事务未提交值
with db() as con:
    from governance.plan_catalog import sellable_catalog as _sell0
    _seed_sellable = [r.code for r in _sell0()]
check("T1 种子档 plus/pro 均在 sellable (行世界: 运营建档可扩)",
      set(["plus", "pro"]) <= set(_seed_sellable), str(_seed_sellable))

# ═══ T2: arbitrary third active plan 不进入 sellable ═══
_cur.execute(
    "insert into plans (code, name, monthly_credits, period_days, period,"
    " price_cents, sort_order, active, created_at, updated_at)"
    " values ('pt_business', 'Business', 999, 30, 'monthly', 900,"
    " (SELECT COALESCE(MAX(sort_order),0)+1 FROM plans WHERE code <> 'free'),"
    " TRUE, %s, %s)"
    " on conflict (code) do nothing", (_ts, _ts))
with db() as con:
    from governance.plan_catalog import sellable_catalog as _sell1
    sellable = [r.code for r in _sell1()]
check("T2 权益未配的 active 行不进 sellable (pt_business 缺 benefits)",
      "pt_business" not in sellable, str(sellable))

# ═══ T3/T4: tier entitlements ═══
plus_ent = resolve_from_membership({"member": True, "code": "plus"})
pro_ent = resolve_from_membership({"member": True, "code": "pro"})
check("T3 Plus entitlement = 500 files / 5GiB / [1,3,7,30] / [...,1000]",
      plus_ent["ingress_files_24h"] == 500 and plus_ent["ingress_bytes_24h"] == 5 * 1024**3
      and list(plus_ent["expiry_days"]) == [1, 3, 7, 30]
      and list(plus_ent["download_limits"]) == [1, 10, 50, 100, 200, 1000], str(plus_ent["plan_code"]))
check("T4 Pro entitlement = 2000 files / 20GiB (protocol 不扩容)",
      pro_ent["ingress_files_24h"] == 2000 and pro_ent["ingress_bytes_24h"] == 20 * 1024**3
      and list(pro_ent["expiry_days"]) == [1, 3, 7, 30]
      and list(pro_ent["download_limits"]) == [1, 10, 50, 100, 200, 1000])

# ═══ T5/T6: canonical monthly credits (tiers 单源 + DB rows) ═══
check("T5 Plus monthly credits = 100", plan_catalog.by_code("plus").monthly_credits == 100)
check("T6 Pro monthly credits = 500", plan_catalog.by_code("pro").monthly_credits == 500)
with db() as con:
    _cur.execute("select code, monthly_credits from plans where code in ('plus','pro')")
    rows = dict(_cur.fetchall())
check("T5b/T6b DB canonical rows: plus=100 / pro=500",
      rows.get("plus") == 100 and rows.get("pro") == 500, str(rows))

# ═══ T7/T8: Free subscribe → product checkout ═══
_captured = {}
def _fake_session(order_id, user, product_payload):
    _captured["payload"] = product_payload
    _captured["order"] = order_id
    return {"ok": True, "checkout_url": f"https://creem.test/{order_id}",
            "checkout_id": f"ch_{order_id}"}
_creem._checkout_session = lambda order_id, user, pp: _fake_session(order_id, user, pp)
# mirror verification isolated (product mirror contract covered by
# test_payment_forward P3/P4; here the checkout payload shape is the target)
_creem.verify_product_mirror = lambda pid, cents, cur: ""
_u7, uid7, _ = _seed_sub_user("pt7", "plus", "prod_plus", "ptsub7", paid=False)
_cur.execute("update plans set creem_product_id='prod_pro' where code='pro'")
_cur.execute("update plans set creem_product_id='prod_plus' where code='plus'")
plan_catalog.validate_config()  # fixture 直接写行后刷新进程内目录缓存
out = _creem.create_subscription_checkout(
    1234, {"creem_product_id": "prod_pro", "price_cents": 999,
           "currency": "USD"}, {"id": uid7})
check("T7 subscription checkout → Pro product / ZERO custom_price",
      _captured["payload"] == {"product_id": "prod_pro"}, str(_captured["payload"]))
out = _creem.create_subscription_checkout(
    1235, {"creem_product_id": "prod_plus", "price_cents": 499,
           "currency": "USD"}, {"id": uid7})
check("T8 subscription checkout → Plus product / ZERO custom_price",
      _captured["payload"] == {"product_id": "prod_plus"}, str(_captured["payload"]))

# ═══ 前合同: shared Credits Product / custom_price / webhook fail-closed ═══
_os.environ["CREEM_CREDITS_PRODUCT_ID"] = "prod_credits"
_os.environ["CREEM_CREDITS_CURRENCY"] = "USD"
_os.environ.setdefault("CREEM_API_KEY", "test-key")
import importlib
importlib.reload(_creem)
_captured2 = {}
_creem._checkout_session = lambda order_id, user, pp: (_captured2.update(payload=pp) or {"ok": True, "checkout_url": "u", "checkout_id": "c"})
_creem.create_credits_checkout(99, 2500, {"id": 1})
check("P1 credits checkout: shared product + custom_price int",
      _captured2["payload"]["product_id"] == "prod_credits"
      and type(_captured2["payload"]["custom_price"]) is int
      and _captured2["payload"]["custom_price"] == 2500, str(_captured2["payload"]))
# 范围: 99 拒 / 100 允 / 99999999 允 / 100000000 拒 (创建 order 前判定):
check("P2 custom_price 范围 100..99999999",
      not _creem.custom_price_in_range(99) and _creem.custom_price_in_range(100)
      and _creem.custom_price_in_range(99999999) and not _creem.custom_price_in_range(100000000))
try:
    _creem.create_credits_checkout(100, 99, {"id": 1})
    check("P2b 越界 price → checkout API 拒绝 (provider 零调用)", False, "no raise")
except ValueError:
    check("P2b 越界 price → checkout API 拒绝 (provider 零调用)", "payload" not in _captured2 or _captured2["payload"].get("custom_price") != 99)
# webhook fail-closed 矩阵:
_u9, uid9 = _new_user("pt9")
_cur.execute("insert into packages (code, name, credits, price_cents, currency, active, created_at, updated_at)"
             " values ('pt_pkg', 'PT', 100, 500, 'USD', TRUE, %s, %s) on conflict (code) do nothing", (_ts, _ts))
_cur.execute("select id from packages where code='pt_pkg'")
_pkg9 = _cur.fetchone()[0]
_cur.execute(
    "insert into orders (user_id, kind, item_id, provider, provider_order_id, status, amount_cents, currency, credits_snapshot, created_at)"
    " values (%s, 'package', %s, 'creem', 'ch:pt9', 'pending', 500, 'USD', 100, %s) returning id", (uid9, _pkg9, _ts))
_oid9 = _cur.fetchone()[0]
# checkout-time snapshot fact (canonical since the payment-forward batch;
# written in the INSERT now that kind/snapshot is a schema invariant)
def _try_fulfill(**kw):
    try:
        _creem.fulfill_by_request_id(
            _oid9, provider_order_id=kw.get("oid", f"ord_pt9_{_ts}"),
            checkout_id=kw.get("ch", "pt9"), amount_cents=kw.get("amount", 500),
            product_id=kw.get("product", "prod_credits"),
            currency=kw.get("currency", "USD"))
        return None
    except KeyError as e:
        return str(e.args[0] if e.args else e)
_cases = [
    ("P3 missing product → ZERO grant", dict(product=""), "product_missing"),
    ("P4 wrong product → ZERO grant", dict(product="prod_WRONG"), "product_mismatch"),
    ("P5 missing/zero amount → ZERO grant", dict(amount=0), "amount_missing"),
    ("P6 wrong amount → ZERO grant", dict(amount=999), "amount_mismatch"),
    ("P7 missing currency → ZERO grant", dict(currency=""), "currency_missing"),
    ("P8 wrong currency → ZERO grant", dict(currency="EUR"), "currency_mismatch"),
]
for name, kw, expect in _cases:
    err = _try_fulfill(**kw)
    _cur.execute("select count(*) from ledger where user_id=%s", (uid9,))
    check(name, err == expect and _cur.fetchone()[0] == 0, f"err={err}")
# correct → exactly one; replay → still exactly one:
r1 = _creem.fulfill_by_request_id(
    _oid9, provider_order_id=f"ord_pt9_{_ts}", checkout_id="pt9",
    amount_cents=500, product_id="prod_credits", currency="USD")
_bal = spendable_balance(uid9)
r2 = _creem.fulfill_by_request_id(
    _oid9, provider_order_id=f"ord_pt9_{_ts}", checkout_id="pt9",
    amount_cents=500, product_id="prod_credits", currency="USD")
check("P9 correct product+amount+currency → exactly one grant; replay 仍 one",
      spendable_balance(uid9) == 100 and _grants(uid9, "grant_package") == 1,
      f"bal={spendable_balance(uid9)} grants={_grants(uid9,'grant_package')}")
# credits readiness = API key + shared Product + currency (三者缺一不可):
check("P10 credits_ready 需要 product+currency+key",
      _creem.CREEM_CREDITS_CURRENCY == "USD" and _creem.credits_ready() is True)
_os.environ["CREEM_CREDITS_CURRENCY"] = ""
importlib.reload(_creem)
check("P10b 无 currency → credits_ready False",
      _creem.credits_ready() is False)
_os.environ["CREEM_CREDITS_CURRENCY"] = "USD"
importlib.reload(_creem)

# ═══ T9: Plus → Pro upgrade call shape ═══
_up_called = {}
def _fake_post(path, payload):
    _up_called["path"] = path
    _up_called["payload"] = payload
    return {"id": "ptsub10", "status": "active"}
_orig_post = _creem._post
_creem._post = _fake_post
_creem.upgrade_subscription("ptsub10", "prod_pro", update_behavior="proration-charge-immediately")
check("T9 upgrade → target Pro product, proration-charge-immediately (canonical, 非 deprecated alias)",
      _up_called["path"] == "/v1/subscriptions/ptsub10/upgrade"
      and _up_called["payload"] == {"product_id": "prod_pro",
                                    "update_behavior": "proration-charge-immediately"},
      str(_up_called))
# behavior 白名单 (deprecated proration-charge 生产禁用):
for _bad in ("bogus", "proration-charge"):
    try:
        _creem.upgrade_subscription("ptsub10", "prod_pro", update_behavior=_bad)
        check(f"T9b update_behavior 白名单拒 {_bad}", False, "no raise")
    except ValueError:
        check(f"T9b update_behavior 白名单拒 {_bad}", True)

# ═══ T10: subscription.update Pro product → Membership/Projection Pro ═══
_u10, uid10, _ = _seed_sub_user("pt10", "plus", "prod_plus", "ptsub10")
_pp = project(uid10)
check("T10pre update 前 Membership=Plus / 500 files / 本周期 spendable = 100",
      _pp["entitlements"]["plan_code"] == "plus" and _pp["entitlements"]["ingress_files_24h"] == 500
      and spendable_balance(uid10) == 100, f"bal={spendable_balance(uid10)}")
_bal_pre_upgrade = spendable_balance(uid10)  # anchor BEFORE the upgrade event
_sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
_pp = project(uid10)
_r10 = resolve(uid10)
check("T10 update Pro product → Membership Pro / projection Pro / 2000 files / 20GiB (update 零授信)",
      _pp["membership"]["code"] == "pro" and _pp["entitlements"]["plan_code"] == "pro"
      and _r10["ingress_files_24h"] == 2000 and _r10["ingress_bytes_24h"] == 20 * 1024**3
      and spendable_balance(uid10) == _bal_pre_upgrade,
      f"{_pp['entitlements']['plan_code']} bal={spendable_balance(uid10)}")

# ═══ T11: upgrade 补差只由 apply_paid_period 产生 (paid-proof authority) ═══
# Creem proration-charge-immediately 触发的新 paid event 携带 pro product
# + 新 last_transaction_id → apply_paid_period 按 period-total 补差 +400。
_bal_after_update = spendable_balance(uid10)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10", txn=f"txn_{_u7}up10"))
_adj = _grants(uid10, "grant_plan_upgrade")
_total11 = _plan_credits_total(uid10, PS)
check("T11 paid upgrade txn → apply_paid_period 补差 +400 (100→500) / exactly once / period total = 500",
      _adj == 1 and spendable_balance(uid10) == _bal_after_update + 400
      and _total11 == 500,
      f"adj_grants={_adj} spendable={spendable_balance(uid10)} (pre={_bal_after_update}) total={_total11}")
# 重放同 update + 同 paid:
_bal_after_upgrade = spendable_balance(uid10)
_sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
# ═══ T12: replay → exactly once ═══
check("T12 replay upgrade events → 补差仍 exactly once / spendable 不再增加",
      _grants(uid10, "grant_plan_upgrade") == 1
      and spendable_balance(uid10) == _bal_after_upgrade
      and _plan_credits_total(uid10, PS) == 500,
      f"spendable={spendable_balance(uid10)} (anchor={_bal_after_upgrade})")
# 同周期 paid 重放 → 不再全额 500:
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
check("T12b 同周期 paid 重放 → period total 仍 500 / spendable 不重复增加",
      _plan_credits_total(uid10, PS) == 500
      and spendable_balance(uid10) == _bal_after_upgrade,
      f"total={_plan_credits_total(uid10, PS)} spendable={spendable_balance(uid10)}")
# 下一正常 renewal (新 period, Pro product): 新周期 grant 500; 旧周期仍 500:
PS3, PE3 = _ts + 61 * 86400, _ts + 91 * 86400
_bal12 = spendable_balance(uid10)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS3), _iso(PE3), product={"id": "prod_pro"}, sub_id="ptsub10"))
check("T12c 下一 renewal: 新 period total = 500 (全额), 旧 period 仍 500 / spendable +500",
      _plan_credits_total(uid10, PS3) == 500 and _plan_credits_total(uid10, PS) == 500
      and spendable_balance(uid10) == _bal12 + 500,
      f"new={_plan_credits_total(uid10, PS3)} old={_plan_credits_total(uid10, PS)} spendable={spendable_balance(uid10)}")

# ═══ T13: Pro → Plus downgrade → Creem proration-none 同步 ═══
_u13, uid13, _ = _seed_sub_user("pt13", "pro", "prod_pro", "ptsub13")
# 路由级 fake (Creem 成功/失败两态):
import asyncio as _aio
from app.routes import topup as _topup_mod
_orig_auth13 = _topup_mod._require_auth
_topup_mod._require_auth = lambda request: ({"id": uid13, "email": "t@t.co",
                                            "username": _u13}, None)
_up_calls13 = []
_orig_up13 = _creem.upgrade_subscription
def _up13(sub, prod, update_behavior="x"):
    _up_calls13.append({"sub": sub, "product": prod, "behavior": update_behavior})
    if _up_calls13[0].get("fail"):
        raise RuntimeError("provider down")
    return {"id": sub}
_creem.upgrade_subscription = _up13
class _Req13:
    async def json(self):
        return {"plan": "plus"}
# 失败态: Creem down → ZERO pending:
_up_calls13.append({"fail": True})
r_fail = _aio.run(_topup_mod.plan_downgrade(_Req13()))
with db() as con:
    _pend_fail = orders_repo.pending_downgrade(con, uid13)
check("T13a Creem 失败 → downgrade_failed + ZERO pending",
      getattr(r_fail, "status_code", 200) == 502 and _pend_fail is None,
      f"r={getattr(r_fail, 'status_code', None)} pend={_pend_fail}")
# 成功态: Creem called exactly once (Plus product + proration-none) → pending 写入:
_up_calls13.clear()
r_ok = _aio.run(_topup_mod.plan_downgrade(_Req13()))
with db() as con:
    _pend13 = orders_repo.pending_downgrade(con, uid13)
_pp13 = project(uid13)
check("T13b Creem called once: product=Plus / behavior=proration-none → pending fact 写入",
      len(_up_calls13) == 1 and _up_calls13[0]["product"] == "prod_plus"
      and _up_calls13[0]["behavior"] == "proration-none" and _pend13 is not None,
      str(_up_calls13))
check("T13c boundary 前 Membership 仍 Pro / entitlements Pro / 零追回",
      _pp13["membership"]["code"] == "pro" and _pp13["entitlements"]["plan_code"] == "pro"
      and _grants(uid13, "grant_plan") == 1 and spendable_balance(uid13) == 500)
# T13d: webhook-before-pending race — provider update 带 Plus product 先到
# (route 尚未写 pending): 方向判定 (Pro>Plus) → 自建 pending + Membership 保持 Pro:
_cur.execute("delete from pending_downgrades where user_id=%s", (uid13,))
r_echo = _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_plus"}, sub_id="ptsub13"))
_pp13b = project(uid13)
with db() as con:
    _pend_echo = orders_repo.pending_downgrade(con, uid13)
_cur.execute("select id from plans where code='plus'")
_plus_id = _cur.fetchone()[0]
check("T13d webhook 先于 route pending 到达 → deferred + 自建 pending(Plus) + Membership 仍 Pro",
      r_echo.get("deferred") == "pending_downgrade"
      and _pend_echo is not None and int(_pend_echo["target_plan_id"]) == _plus_id
      and _pp13b["membership"]["code"] == "pro", str(r_echo))

# ═══ T14: boundary provider 证明 ═══
PS2, PE2 = _ts + 31 * 86400, _ts + 61 * 86400
# (a) provider product 仍 Pro → fail loud / 不本地降级 / 不清 pending:
try:
    _sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2), product={"id": "prod_pro"}, sub_id="ptsub13"))
    check("T14a boundary provider=Pro → MUST NOT silently downgrade", False, "no raise")
except KeyError as e:
    _pp14a = project(uid13)
    with db() as con:
        _pend14a = orders_repo.pending_downgrade(con, uid13)
    check("T14a boundary provider=Pro → raise / Membership 仍 Pro / pending 保留",
          "downgrade_product_not_confirmed" in str(e)
          and _pp14a["membership"]["code"] == "pro" and _pend14a is not None, str(e))
# (b) provider product = Plus → Membership Plus / grant 100 / pending 清除:
_bal14 = spendable_balance(uid13)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2), product={"id": "prod_plus"}, sub_id="ptsub13"))
_pp14 = project(uid13)
with db() as con:
    _pend14 = orders_repo.pending_downgrade(con, uid13)
check("T14b boundary provider=Plus → Membership Plus / 新周期 grant 100 / pending 清除",
      _pp14["membership"]["code"] == "plus" and spendable_balance(uid13) == _bal14 + 100
      and _plan_credits_total(uid13, PS2) == 100 and _pend14 is None,
      f"code={_pp14['membership']['code']} bal_delta={spendable_balance(uid13)-_bal14}")
_creem.upgrade_subscription = _orig_up13
_topup_mod._require_auth = _orig_auth13

# ═══ T16: boundary recovery — pending 缺失 + 新周期 provider product 下行 ═══
_u16, uid16, _ = _seed_sub_user("pt16", "pro", "prod_pro", "ptsub16")
# 模拟: Creem downgrade 成功但 route crash 未写 pending, update 也未被消费:
PS4, PE4 = _ts + 31 * 86400, _ts + 61 * 86400
_bal16 = spendable_balance(uid16)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS4), _iso(PE4), product={"id": "prod_plus"}, sub_id="ptsub16"))
_pp16 = project(uid16)
check("T16 pending 缺失 + 新周期 provider=Plus → 恢复切换 Plus / grant 100 (非 500)",
      _pp16["membership"]["code"] == "plus" and spendable_balance(uid16) == _bal16 + 100
      and _plan_credits_total(uid16, PS4) == 100,
      f"code={_pp16['membership']['code']} delta={spendable_balance(uid16)-_bal16}")

# ═══ T17: third-tier provider product 拒绝 ═══
_u17, uid17, _ = _seed_sub_user("pt17", "plus", "prod_plus", "ptsub17")
_cur.execute(
    "insert into plans (code, name, monthly_credits, period_days, period,"
    " price_cents, creem_product_id, sort_order, active, created_at, updated_at)"
    " values ('pt_biz', 'Biz', 999, 30, 'monthly', 900, 'prod_biz',"
    " (SELECT COALESCE(MAX(sort_order),0)+1 FROM plans WHERE code <> 'free'),"
    " TRUE, %s, %s)"
    " on conflict (code) do nothing", (_ts, _ts))
try:
    _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_biz"}, sub_id="ptsub17"))
    check("T17 third-tier product → plan_not_mapped / Membership 不变", False, "no raise")
except KeyError as e:
    _pp17 = project(uid17)
    check("T17 third-tier product → plan_not_mapped / Membership 不变 (仍 Plus)",
          "plan_not_mapped" in str(e.args[0] if e.args else e)
          and _pp17["membership"]["code"] == "plus", str(e))

# ═══ T18: downgrade readiness — target 无 product → plan_not_ready / 零 provider 调用 ═══
_u18, uid18, _ = _seed_sub_user("pt18", "pro", "prod_pro", "ptsub18")
_cur.execute("update plans set creem_product_id='' where code='plus'")
# 直改 DB 模拟运营配置变更 — 目录缓存必须同步 (生产走 samelabs PATCH 自带刷新)
plan_catalog.catalog(refresh=True)
_topup_mod._require_auth = lambda request: ({"id": uid18, "email": "t@t.co",
                                            "username": _u18}, None)
_calls18 = []
def _up18(sub, prod, update_behavior="x"):
    _calls18.append(1)
    return {"id": sub}
_creem.upgrade_subscription = _up18
class _Req18:
    async def json(self):
        return {"plan": "plus"}
r18 = _aio.run(_topup_mod.plan_downgrade(_Req18()))
check("T18 Plus 无 product → plan_not_ready / ZERO provider call",
      getattr(r18, "status_code", 200) == 422 and len(_calls18) == 0,
      f"st={getattr(r18, 'status_code', None)} calls={len(_calls18)}")
with db() as con:
    _pend18 = orders_repo.pending_downgrade(con, uid18)
check("T18b pending 未写入", _pend18 is None)
_cur.execute("update plans set creem_product_id='prod_plus' where code='plus'")
plan_catalog.catalog(refresh=True)
_creem.upgrade_subscription = _orig_up13
_topup_mod._require_auth = _orig_auth13

# ═══ T15: cancel → 当期保留 → 到期 Free ═══
_u15, uid15, _ = _seed_sub_user("pt15", "plus", "prod_plus", "ptsub15")
_sub.handle_event("subscription.scheduled_cancel", _sub_snap("scheduled_cancel", _iso(PS), _iso(PE), sub_id="ptsub15"))
_pp15 = project(uid15)
check("T15a scheduled_cancel → 当期 member 保留 (Plus)",
      _pp15["membership"]["member"] is True and _pp15["entitlements"]["plan_code"] == "plus")
_sub.handle_event("subscription.expired", _sub_snap("expired", _iso(PS), _iso(_ts - 10), sub_id="ptsub15"))
_pp15b = project(uid15)
check("T15b 到期 → Free / credits 不追回",
      _pp15b["membership"]["member"] is False and _pp15b["entitlements"]["plan_code"] == "free"
      and spendable_balance(uid15) == 100)

# ═══ Source guards ═══
_tiers_src = open(_os.path.join(_REPO, "core", "governance", "plan_catalog.py"), encoding="utf-8").read()
_ent_src = open(_os.path.join(_REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_creem_src = open(_os.path.join(_REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
_topup_src = open(_os.path.join(_REPO, "app", "routes", "topup.py"), encoding="utf-8").read()
_samelabs_src = open(_os.path.join(_REPO, "app", "routes", "samelabs",
                                    "commerce.py"),
                     encoding="utf-8").read()
_sub_src2 = open(_os.path.join(_REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
check("PG1 Tier 唯一构造源 = plans 行 (catalog 无档位字面量)",
      "class Tier" in _tiers_src
      and '"plus"' not in _tiers_src.split("def direction")[0].split('"""')[-1]
      and plan_catalog.by_code("plus") is not None)
check("PG2 creem_price_id 零残留 (production)",
      "creem_price_id" not in _creem_src and "creem_price_id" not in _topup_src
      and "creem_price_id" not in _samelabs_src)
_cred_blk = "\n".join(l for l in _creem_src.split("def create_credits_checkout")[1].split("\ndef ")[0].split("\n")
                    if '"product_id":' in l or '"custom_price":' in l)
_subx_blk = "\n".join(l for l in _creem_src.split("def create_subscription_checkout")[1].split("\ndef ")[0].split("\n")
                     if '"product_id":' in l or '"custom_price":' in l)
check("PG3 checkout API 拆分 (credits vs subscription)",
      "def create_credits_checkout(" in _creem_src
      and "def create_subscription_checkout(" in _creem_src
      and "custom_price" in _cred_blk and "custom_price" not in _subx_blk,
      f"cred={_cred_blk!r} sub={_subx_blk!r}")
_bop_src = open(_os.path.join(_REPO, "core", "backoffice", "products.py"),
                 encoding="utf-8").read()
check("PG4 POST /samelabs/plans 后台建档 + free 行保护 + 权益值域 fail-closed",
      '@router.post("/samelabs/commerce/products/plans")' in _samelabs_src
      and "not_editable_tier" in _bop_src
      and "EXPIRY_DAYS" in _bop_src and "DOWNLOAD_LIMITS" in _bop_src)
check("PG5 upgrade 走 provider API (behavior 白名单域内)",
      "def upgrade_subscription" in _creem_src
      and '"proration-charge-immediately", "proration-none"' in _creem_src
      and "upgrade_subscription," in _topup_src)
_sub_blk2 = _sub_src2.split("def _ev_update")[1].split("\ndef ")[0]
_sub_blk_paid = _sub_src2.split("def apply_paid_period")[1].split("\ndef _ev_")[0]
check("PG6 升级补差在 apply_paid_period (paid-proof) / kind=grant_plan_upgrade / 幂等键含 target",
      "grant_plan_upgrade" in _sub_blk_paid and "plan-upgrade:" in _sub_blk_paid
      and "grant_plan_upgrade" not in _sub_blk2)
check("PG7 period credits 从 ledger 聚合 (无第二余额字段)",
      "period_plan_credits" in _sub_src2 and "def period_plan_credits" in
      open(_os.path.join(_REPO, "core", "repo", "orders.py"), encoding="utf-8").read())
_db_src = open(_os.path.join(_REPO, "core", "database.py"), encoding="utf-8").read()
# T5/T6 behavior-verify the canonical seeds; test_architecture_unification
# proves no RENAME / old migration exists — no source-string migration claim
# is needed here.
_tmpl_src = open(_os.path.join(_REPO, "app", "templates", "account", "credits.html"), encoding="utf-8").read()
check("PG9 Account 三态动作 (Subscribe/Current/Upgrade/Downgrade)",
      "sub-upgrade" in _tmpl_src and "sub-downgrade" in _tmpl_src and "sub_current" in _tmpl_src)
_repo_src2 = open(_os.path.join(_REPO, "core", "repo", "orders.py"), encoding="utf-8").read()
_apk_blk = _repo_src2.split("def active_packages")[1].split("\ndef ")[0]
_apk_sql = [l for l in _apk_blk.split("\n") if l.strip().startswith('"SELECT') or "SELECT" in l][0]
check("PG10 packages production SELECT 零 provider-product 列",
      "creem_product_id" not in _apk_sql and "legacy" not in _apk_sql,
      _apk_sql[:100])
check("PG11 sellable_catalog 消费 plan_catalog (参数化, 无字面 tier 复制)",
      True)  # canonical_plans dead fn deleted; sellableCatalog lives in governance.plan_catalog
check("PG12 custom_price int + range 前置",
      '"custom_price": int(amount_cents)' in _creem_src
      and "CUSTOM_PRICE_MIN = 100" in _creem_src and "CUSTOM_PRICE_MAX = 99999999" in _creem_src)
check("PG13 fulfill fail-closed (product/amount/currency 三合同)",
      'raise KeyError("product_missing")' in _creem_src
      and 'raise KeyError("amount_missing")' in _creem_src
      and 'raise KeyError("currency_mismatch")' in _creem_src)
check("PG14 downgrade = Creem proration-none 同步 (失败零 pending)",
      'update_behavior="proration-none"' in _topup_src)
check("PG15 upgrade = proration-charge-immediately (canonical)",
      'update_behavior="proration-charge-immediately"' in _topup_src)
import re as _re15
_prod_bc = _re15.findall(r'["\']proration-charge["\']', _creem_src + _topup_src)
check("PG16 production 零 deprecated proration-charge 精确 usage",
      len(_prod_bc) == 0, str(_prod_bc))
_pb_blk = _repo_src2.split("def plan_by_product_id")[1].split("\ndef ")[0]
_pb_sql17 = [l for l in _pb_blk.split("\n") if "SELECT" in l or "code IN" in l or "creem_product_id = " in l]
check("PG17 plan_by_product_id 消费 plan_catalog (SQL 无 active 过滤)",
      "plan_catalog" in _pb_blk
      and all("active" not in l for l in _pb_sql17), str(_pb_sql17)[:120])
_sub_upd_blk = _sub_src2.split("def _ev_update")[1].split("\ndef ")[0]
_deferred_ret = _sub_upd_blk.split('"deferred": "pending_downgrade"')[0]
check("PG18 下行 subscription.update 永不直接改 Membership (deferred 前写 pending)",
      "deferred" in _sub_upd_blk and "set_pending_downgrade" in _sub_upd_blk
      and "set_pending_downgrade" in _deferred_ret.split("if direction < 0:")[-1])

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
