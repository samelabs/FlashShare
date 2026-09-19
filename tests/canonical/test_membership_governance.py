#!/usr/bin/env python3
"""Membership governance canonical invariants — 整链单 suite.

多档 Membership 作为 quota/entitlement 唯一稳定事实源:
memberships 单行权威、provider subscription 绑定、plan→entitlement
显式 profile、首购/复购/tier-change、paid/cancel/expire/reconcile、
migration fail-closed。遵循 canonical fail-closed 规则。
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
sys.path.insert(0, _os.path.join(_REPO, "core"))

import psycopg2
from database import db
from repo import orders as orders_repo
from economy import subscription as _sub
from economy import reconcile as _rec
from economy.billing import spendable_balance
from governance.entitlements import (resolve, resolve_from_membership, entitlement_profile_exists, EntitlementConfigurationError)


def _row_views():
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    from governance import plan_catalog as _pc
    _plans = {t.code: t.benefits.as_dict(t.code) for t in _pc.catalog() if not t.is_free}
    return _plans.get("free"), _plans


_PLANS = _row_views()
from governance import plan_catalog as _pc

def _refresh_catalog():
    _pc.validate_config()
from governance.access_projection import project

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
# migration 子进程用文件传 DSN (与 runner 的 TEST_DB_URL 同源):
_DSN_FILE = "/tmp/mg_gov_dsn.txt"
with open(_DSN_FILE, "w") as _f:
    _f.write(TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ["FLASHSHARE_TEST_DSN_FILE"] = _DSN_FILE
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_ts = int(time.time())
FUT = _ts + 30 * 86400
# 幂等重跑: 清理本 suite 之前的半途残留 (mg 前缀用户及其行)
_cur.execute("delete from memberships where user_id in (select id from users where username like 'mg%')")
_cur.execute("delete from orders where user_id in (select id from users where username like 'mg%')")
_cur.execute("delete from ledger where user_id in (select id from users where username like 'mg%')")
_cur.execute("delete from plans where code like 'mg_%'")
_cur.execute("delete from users where username like 'mg%'")

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts, _ts))
    return u, _cur.fetchone()[0]

def _seed_plan(code, credits=100, price_id=None):
    # 幂等 seed: plus 为 init_db 种子行, 其余测试自建
    _cur.execute(
        "insert into plans (code, name, monthly_credits, price_cents, creem_product_id, sort_order, created_at, updated_at)"
        " values (%s, %s, %s, 500, %s,"
        " (SELECT COALESCE(MAX(p.sort_order),0)+1 FROM plans p WHERE p.code <> 'free'), %s, %s)"
        " on conflict (code) do update set monthly_credits=excluded.monthly_credits,"
        " creem_product_id=coalesce(plans.creem_product_id, excluded.creem_product_id),"
        " price_cents=excluded.price_cents"
        " returning id",
        (code, code.upper(), credits, price_id, _ts, _ts))
    _refresh_catalog()
    return _cur.fetchone()[0]

def _seed_order(uid, plan_id, sub_id):
    _cur.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, created_at, paid_at)"
        " values (%s, 'plan', %s, 'creem', %s, 'paid', 500, %s, %s)"
        " on conflict (provider_order_id) do update set item_id=excluded.item_id"
        " returning id",
        (uid, plan_id, f"sub:{sub_id}", _ts, _ts))
    return _cur.fetchone()[0]

def _sub_snap(status, p_start, p_end, product=None, sub_id="mgsub1"):
    d = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": p_start, "current_period_end_date": p_end,
         # paid-proof contract: product + last_transaction_id must be present
         # AND the product must match the locally bound plan product
         "product": product or {"id": "price_plus"},
         "last_transaction_id": f"txn_{sub_id}"}
    return d

def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _m_row(uid):
    _cur.execute("select plan_id, provider_subscription_id, status, renews_at,"
                 " last_grant_period_start, cancel_scheduled_at from memberships where user_id=%s", (uid,))
    return _cur.fetchone()

def _grants(uid):
    _cur.execute("select count(*) from ledger where user_id=%s and kind='grant_plan'", (uid,))
    return _cur.fetchone()[0]

PS, PE = _ts - 3600, FUT

# ═══ 链1: Free → Free quota ═══
uF, uidF = _new_user("mgF")
p = project(uidF)
check("G1 Free: membership None + Free entitlements + usage",
      p["membership"] is None and p["entitlements"]["plan_code"] == "free"
      and resolve(uidF)["plan_code"] == "free")

# ═══ 链2: Plus current → Plus quota (首购 → paid) ═══
uP, uidP = _new_user("mgP")
plan_plus = _seed_plan("plus", 100, "price_plus")
_cur.execute("select id from plans where code='plus' and id != %s", (plan_plus,))
oid = _seed_order(uidP, plan_plus, "mgsub1")
# 首购建档 (bind_checkout 的 membership 建立):
_sub.bind_checkout({"id": "ch1", "order": {"id": "ord_mg1", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_plus"}, "subscription": {"id": "mgsub1"}})
# paid → grant + membership active:
res = _sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE)))
m = _m_row(uidP)
check("G2 首购+paid: exactly one grant + membership active (sub 绑定)",
      _grants(uidP) == 1 and spendable_balance(uidP) == 100
      and m[2] == "active" and m[1] == "sub:mgsub1" and m[3] == PE, str(res))
pp = project(uidP)
check("G2b Plus current → Plus entitlements (整链跟随)",
      pp["membership"]["member"] is True and pp["entitlements"]["plan_code"] == "plus"
      and resolve(uidP)["plan_code"] == "plus")

# ═══ 链3: 不同 live sub → 拒绝换绑 ═══
from repo.orders import MembershipBindingError as _MBE
try:
    with db() as con:
        orders_repo.upsert_membership_state(con, "mgsub2", "active",
                                            started_at=_ts, renews_at=PE)
    check("G3 不同 live sub state update → 拒绝换绑 (原行不变)", False, "no raise")
except _MBE:
    m = _m_row(uidP)
    _cur.execute("select count(*) from memberships where user_id=%s", (uidP,))
    check("G3 不同 live sub state update → 拒绝换绑 (原行不变)",
          _cur.fetchone()[0] == 1 and m[1] == "sub:mgsub1"
          and m[3] == PE and m[2] == "active", str(m))
except Exception as exc:
    check("G3 不同 live sub state update → 拒绝换绑", False, str(exc))
try:
    with db() as con:
        orders_repo.bind_membership(con, uidP, plan_plus, "mgsub2", "active",
                                    started_at=_ts, renews_at=PE)
    check("G3b bind 在 live Membership 上 → 拒绝", False, "no raise")
except _MBE:
    _cur.execute("select count(*) from memberships where user_id=%s", (uidP,))
    check("G3b bind 在 live Membership 上 → 拒绝 (sub 仍 mgsub1)",
          _cur.fetchone()[0] == 1 and _m_row(uidP)[1] == "sub:mgsub1")

# ═══ 链4: 第二付费 tier → 自己的 entitlement (非 Free) ═══
# 第二付费 tier 使用 canonical pro (provider product 只映射 plus/pro):
uT, uidT = _new_user("mgT")
_cur.execute("select id from plans where code='pro'")
plan_pro = _cur.fetchone()[0]
_cur.execute("update plans set creem_product_id='price_pro' where code='pro'")
_refresh_catalog()
PRO_FILES, PRO_BYTES = 2000, 20 * 1024 ** 3
_seed_order(uidT, plan_pro, "mgsubT")
_sub.bind_checkout({"id": "chT", "order": {"id": "ord_mgT", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_pro"}, "subscription": {"id": "mgsubT"}})
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), sub_id="mgsubT"))
r = resolve(uidT)
check("G4 第二 tier (pro) → 自己的 entitlement (2000 files/20GiB, 绝不 Free)",
      r["plan_code"] == "pro" and r["ingress_files_24h"] == PRO_FILES
      and r["ingress_bytes_24h"] == PRO_BYTES, str(r["plan_code"]))
check("G4b entitlement_profile_exists 单点",
      entitlement_profile_exists("plus") and entitlement_profile_exists("pro")
      and not entitlement_profile_exists("nope"))

# ═══ 链5: subscription.update 换 tier (同一 sub) ═══
res = _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "price_pro"}, sub_id="mgsubT"))
m = _m_row(uidT)
pp = project(uidT)
check("G5 update plan A→B: 同一 provider sub, membership/projection/entitlement 全变 B",
      m[0] == plan_pro and m[1] == "sub:mgsubT"
      and pp["entitlements"]["plan_code"] == "pro"
      and resolve(uidT)["ingress_files_24h"] == PRO_FILES, str(res))
# product 单点提取 (Creem 当前两种合法形态; product_id-only 不再解释):
check("G5c extract_product_id dict/string/其它→空",
      _sub.extract_product_id({"product": {"id": "prod_x"}}) == "prod_x"
      and _sub.extract_product_id({"product": "price_y"}) == "price_y"
      and _sub.extract_product_id({"product_id": "price_z"}) == ""
      and _sub.extract_product_id({"product": 123}) == ""
      and _sub.extract_product_id({}) == "")
# update 无映射 → fail loud:
try:
    _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "price_unknown"}, sub_id="mgsubT"))
    check("G5b product 无本地唯一映射 → KeyError fail loud", False, "no raise")
except KeyError:
    check("G5b product 无本地唯一映射 → KeyError fail loud", True)

# ═══ 链6: paid renewal → idem once ═══
PS2, PE2 = _ts + 86400, _ts + 31 * 86400
_bal = spendable_balance(uidP); _g = _grants(uidP)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2)))
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2)))  # replay
check("G6 renewal grant once + replay 幂等 (idem plan:{uid}:{period})",
      _grants(uidP) == _g + 1 and spendable_balance(uidP) == _bal + 100
      and _m_row(uidP)[4] == PS2)

# ═══ 链7: scheduled_cancel → 当期权益保留 ═══
_sub.handle_event("subscription.scheduled_cancel", _sub_snap("scheduled_cancel", _iso(PS2), _iso(PE2)))
m = _m_row(uidP)
pp = project(uidP)
check("G7 scheduled_cancel: 当期 member 有效 + marker 非空",
      m[5] is not None and pp["membership"]["member"] is True
      and pp["entitlements"]["plan_code"] == "plus", str(m))

# ═══ 链8: period end → Free ═══
_sub.handle_event("subscription.expired", _sub_snap("expired", _iso(PS2), _iso(_ts - 10)))
pp = project(uidP)
check("G8 period end → Free (membership fact 保留, entitlements free)",
      pp["membership"]["member"] is False and pp["entitlements"]["plan_code"] == "free")

# ═══ 链9: reconcile 跟 current Membership sub ═══
uR, uidR = _new_user("mgR")
plan_r = _seed_plan("plus", 100, "price_plus")
_seed_order(uidR, plan_r, "mgsubR")
_sub.bind_checkout({"id": "chR", "order": {"id": "ord_mgR", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_plus"}, "subscription": {"id": "mgsubR"}})
# 历史 plan order (非当前 sub) — reconcile 不得遍历它:
_old_plan = _seed_plan("mg_old", 777)
_cur.execute(
    "insert into orders (user_id, kind, item_id, provider, provider_order_id, status, amount_cents, created_at, paid_at)"
    " values (%s, 'plan', %s, 'creem', 'sub:mg_old_sub', 'paid', 500, %s, %s)",
    (uidR, _old_plan, _ts, _ts))
_snap_g9 = _sub_snap("active", _iso(PS), _iso(PE), sub_id="mgsubR")
_rec.fetch_subscription = lambda sid: _snap_g9 if sid == "mgsubR" else None
_rec.fetch_transaction = lambda t: {"object": "transaction", "id": t,
                                   "status": "paid", "type": "invoice",
                                   "subscription": "mgsubR",
                                   "period_start": PS, "period_end": PE}
r = _rec.reconcile_user(uidR)
check("G9 reconcile 只跟 current Membership sub (不遍历历史 orders)",
      r["checked"] == 1 and r["granted"] == 1 and spendable_balance(uidR) == 100
      and _m_row(uidR)[0] == plan_r, f"r={r}")

# ═══ 链10: unknown profile tier — 收费前拒绝 + 异常防线 ═══
# 新逻辑: 无 profile 的付费档在 /subscribe 收费前拒绝 (plan_not_ready),
# 不产生 order/checkout。此处直测 gate 消费的同一 profile 单点:
uU, uidU = _new_user("mgU")
plan_x = _seed_plan("mg_unknown_tier", 100, "price_x")
_cur.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidU,))
check("G10pre unknown-profile plan 收费前拒绝 (gate 单点 + gate 后零 order)",
      not entitlement_profile_exists("mg_unknown_tier")
      and _cur.fetchone()[0] == 0)
# 防御测试: 若 DB/外部异常导致未知 active Membership 已存在 (绕过 gate),
# read 面 fail loud、authorization fail-safe — 异常防线, 非销售流程:
_seed_order(uidU, plan_x, "mgsubU")
_sub.bind_checkout({"id": "chU", "order": {"id": "ord_mgU", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_x"}, "subscription": {"id": "mgsubU"}})
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), sub_id="mgsubU"))
try:
    resolve_from_membership({"member": True, "code": "mg_unknown_tier"})
    check("G10 unknown active tier: read 面 EntitlementConfigurationError fail loud", False, "no raise")
except EntitlementConfigurationError:
    check("G10 unknown active tier: read 面EntitlementConfigurationError fail loud", True)
r = resolve(uidU)  # authorization fail-safe
check("G10b authorization → Free fail-safe (付费用户不静默, 但授权安全)",
      r["plan_code"] == "free")
try:
    project(uidU)
    check("G10c projection 传播配置错误 (不谎称 tier)", False, "no raise")
except EntitlementConfigurationError:
    check("G10c projection 传播配置错误 (不谎称 tier)", True)

# ═══ 链12: /subscribe 序列化 — pending checkout reuse / race / profile gate ═══
import asyncio
from app.routes import topup as _topup
_orig_auth = _topup._require_auth
_topup._require_auth = lambda request: ({"id": uidP, "email": "t@t.co",
                                        "username": uP}, None)
_creem_calls = {"create": 0, "retrieve": {}}
def _fake_create(order_id, package, user):
    _creem_calls["create"] += 1
    return {"ok": True, "checkout_url": f"https://creem.test/c/{order_id}",
            "checkout_id": f"ch_fake_{order_id}"}
_RT = {"ch_fake_EXPIRED": {"status": "expired", "checkout_url": "https://creem.test/e"},
       "ch_fake_PEND": {"status": "pending", "checkout_url": "https://creem.test/p"},
       "ch_fake_DONE": {"status": "completed", "checkout_url": "https://creem.test/d"}}
def _fake_retrieve(cid):
    _creem_calls["retrieve"][cid] = _creem_calls["retrieve"].get(cid, 0) + 1
    if cid not in _RT:
        raise RuntimeError("provider down")
    return _RT[cid]
from economy import creem as _creem_mod
def _fake_sub_checkout(order_id, plan, user):
    return _fake_create(order_id, plan, user)
_orig_create, _orig_retrieve, _orig_enabled = (_creem_mod.create_subscription_checkout,
    _creem_mod.retrieve_checkout, _creem_mod.creem_enabled)
_creem_mod.create_subscription_checkout = _fake_sub_checkout
_creem_mod.retrieve_checkout = _fake_retrieve
_creem_mod.creem_enabled = lambda: True

class _FakeReq:
    def __init__(self, body):
        self._b = json.dumps(body).encode()
    async def json(self):
        return json.loads(self._b)

async def _do_sub(uid_body):
    return await _topup.subscribe_checkout(_FakeReq(uid_body))

def _resp(jr):
    if isinstance(jr, dict):
        return 200, jr
    return jr.status_code, json.loads(jr.body)

# live member (fresh, paid) → plan_change_required, 零 create:
uL, uidL = _new_user("mgL")
_seed_order(uidL, plan_plus, "mgsubL")
_sub.bind_checkout({"id": "chL", "order": {"id": "ord_mgL", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_plus"}, "subscription": {"id": "mgsubL"}})
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), sub_id="mgsubL"))
_topup._require_auth = lambda request: ({"id": uidL, "email": "t@t.co",
                                        "username": uL}, None)
_creem_calls["create"] = 0
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
check("G12 live member 再 subscribe → plan_change_required / 零 checkout",
      st == 409 and d.get("error") == "plan_change_required"
      and _creem_calls["create"] == 0, str(d))

# 新用户首次 + pending reuse 序列:
uS, uidS = _new_user("mgS")
_topup._require_auth = lambda request: ({"id": uidS, "email": "t@t.co",
                                        "username": uS}, None)
_cur.execute("update plans set creem_product_id='price_plus' where id=%s", (plan_plus,))
_refresh_catalog()
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur.execute("select id from orders where user_id=%s and kind='plan' order by id desc limit 1", (uidS,))
_oi1 = _cur.fetchone()[0]
check("G12b 首次 subscribe → 1 order + 1 create_checkout",
      st == 200 and d.get("reused") is None and _creem_calls["create"] == 1
      and _oi1 > 0, f"st={st} d={d}")
# 第二次 (webhook 前) → retrieve pending → reuse URL, 不建第二 order/create:
_RT[f"ch_fake_{_oi1}"] = {"status": "pending", "checkout_url": f"https://creem.test/c/{_oi1}"}
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidS,))
_n_orders = _cur.fetchone()[0]
check("G12c webhook 前再 subscribe → reuse 同一 checkout URL / 仍 1 order / 零新 create",
      st == 200 and d.get("reused") is True and d.get("checkout_url") == f"https://creem.test/c/{_oi1}"
      and _n_orders == 1 and _creem_calls["create"] == 1, f"st={st} d={d} n={_n_orders}")
# expired → 终结旧 order, 重新创建:
_RT[f"ch_fake_{_oi1}"] = {"status": "expired", "checkout_url": f"https://creem.test/c/{_oi1}"}
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur.execute("select count(*) from orders where user_id=%s and kind='plan' and status='failed'", (uidS,))
_nf = _cur.fetchone()[0]
check("G12d expired checkout → 旧 order failed + 新 create",
      st == 200 and _nf == 1 and _creem_calls["create"] == 2, f"st={st} failed={_nf}")
# bind 后 paid 前 (membership active renews NULL) → 拒绝:
_cur.execute("select id from orders where user_id=%s and kind='plan' and status='pending' order by id desc limit 1", (uidS,))
_oi2 = _cur.fetchone()[0]
with db() as con:
    orders_repo.bind_membership(con, uidS, plan_plus, f"chfake{_oi2}", "active",
                                started_at=_ts, renews_at=None)
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
check("G12e bind 后 paid 前 → plan_change_required (不建第二 subscription)",
      st == 409 and d.get("error") == "plan_change_required"
      and _creem_calls["create"] == 2, f"st={st} d={d}")
# unknown-profile plan → 收费前拒绝 (plan_not_ready, 零 order 零 create):
uQ, uidQ = _new_user("mgQ")
_cur.execute("update plans set creem_product_id='price_q' where code='mg_unknown_tier'")
_refresh_catalog()
_cur.execute("select id from plans where code='mg_unknown_tier'")
_pq = _cur.fetchone()[0]
_topup._require_auth = lambda request: ({"id": uidQ, "email": "t@t.co",
                                        "username": uQ}, None)
st, d = _resp(asyncio.run(_do_sub({"plan_id": _pq})))
_cur.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidQ,))
check("G12f unknown-profile plan → plan_not_ready / 零 order / 零 checkout",
      st == 422 and d.get("error") == "plan_not_ready"
      and _cur.fetchone()[0] == 0 and _creem_calls["create"] == 2, f"st={st} d={d}")
_topup._require_auth = _orig_auth
# 夹具清场: mg_unknown_tier 回到"权益未配的缺口行" (可售性归零), 不污染后续套件的
# sellable 目录断言
_cur.execute("update plans set creem_product_id=NULL, price_cents=0"
             " where code='mg_unknown_tier'")
_refresh_catalog()

# ═══ 链13: occupied/rebind 七态 (membership_is_occupied 单一语义) ═══
_FUT2, _PAST2 = _ts + 15 * 86400, _ts - 15 * 86400
_states = [
    # (name, status, renews_at, expect_occupied)
    ("active+None", "active", None, True),
    ("active+future", "active", _FUT2, True),
    ("canceled+future", "canceled", _FUT2, True),
    ("past_due+future", "past_due", _FUT2, True),
    ("expired", "expired", _FUT2, False),
    ("canceled+past", "canceled", _PAST2, False),
    ("active+past", "active", _PAST2, False),
]
for _nm, _st, _rn, _exp in _states:
    check(f"G13 {_nm} → occupied={_exp}",
          _sub.membership_is_occupied(_st, _rn, _ts) is _exp)
# bind 路径吃同一语义: 七态行 × 不同 sub bind:
for _nm, _st, _rn, _exp in _states:
    uX, uidX = _new_user("mgX")
    _cur.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id, status,"
        " started_at, renews_at, updated_at) values (%s, %s, %s, %s, %s, %s, %s)",
        (uidX, plan_plus, f"sub:old_{uidX}", _st, _ts - 86400, _rn, _ts))
    try:
        with db() as con:
            orders_repo.bind_membership(con, uidX, plan_plus, f"rx{uidX}",
                                        "active", started_at=_ts, renews_at=None)
        check(f"G13b bind 不同 sub @ {_nm} → {'BLOCK' if _exp else 'rebind ok'}",
              not _exp, "unexpectedly allowed")
    except _MBE:
        check(f"G13b bind 不同 sub @ {_nm} → {'BLOCK' if _exp else 'rebind ok'}",
              _exp, "unexpectedly blocked")

# ═══ 链14: 真实并发 /subscribe — threading.Barrier 同起跑 ═══
import threading
uR2, uidR2 = _new_user("mgR2")
_topup._require_auth = lambda request: ({"id": uidR2, "email": "t@t.co",
                                        "username": uR2}, None)
_creem_calls["create"] = 0
_barrier = threading.Barrier(2)
_results = []
def _worker():
    _barrier.wait()
    _results.append(_resp(asyncio.run(_do_sub({"plan_id": plan_plus}))))
_t1 = threading.Thread(target=_worker)
_t2 = threading.Thread(target=_worker)
_t1.start(); _t2.start(); _t1.join(); _t2.join()
_cur.execute("select count(*) from orders where user_id=%s and kind='plan' and status='pending'", (uidR2,))
_np = _cur.fetchone()[0]
_ok200 = sum(1 for st, _ in _results if st == 200)
_inprog = sum(1 for st, d in _results if d.get("error") == "subscription_in_progress")
_chk_ids = {d.get("checkout_id") for st, d in _results if st == 200}
check("G14 同 user 两线程并发 /subscribe → 恰 1 pending Order / 同一 checkout entity",
      _np == 1 and _ok200 == 2 and len(_chk_ids) == 1,
      f"orders={_np} creates={_creem_calls['create']} ch={_chk_ids} results={_results}")
_topup._require_auth = _orig_auth
# retrieve URL proof: 真 retrieve_checkout + 捕获 Request.get_full_url:
import io as _io
class _FakeResp:
    def read(self):
        return json.dumps({"status": "pending", "checkout_url": "https://creem.test/x"}).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
_captured = {}
import urllib.request as _ureq
_orig_open = _ureq.urlopen
def _spy_open(req, *a, **kw):
    _captured["url"] = req.get_full_url()
    _captured["method"] = req.get_method()
    return _FakeResp()
_ureq.urlopen = _spy_open
try:
    out = _orig_retrieve("ch/test with space")  # 真实现 (fake 已被 _spy 顶替 urlopen)
finally:
    _ureq.urlopen = _orig_open
check("G14b retrieve URL == /v1/checkouts?checkout_id=<quoted> (GET)",
      _captured.get("url", "").endswith(
          "/v1/checkouts?checkout_id=ch%2Ftest+with+space")
      and "/v1/checkouts?" in _captured.get("url", "")
      and _captured.get("method") == "GET" and out.get("status") == "pending",
      str(_captured))
# 还原 creem patch (后续 suite 段不吃 fake):
_creem_mod.create_subscription_checkout = _orig_create
_creem_mod.retrieve_checkout = _orig_retrieve
_creem_mod.creem_enabled = _orig_enabled

# ═══ 链15: subscription checkout 两事实分离 (order paid vs cycle credits) ═══
uV, uidV = _new_user("mgV")
plan_v = plan_plus
_seed_order(uidV, plan_v, "mgsubV")  # pending plan order
_cur.execute("select id, status, granted from orders where user_id=%s and kind='plan' order by id desc limit 1", (uidV,))
_ov_id, _ov_status, _ov_granted = _cur.fetchone()
# checkout.completed (bind) — payment 完成, credits 未授:
_res15 = _sub.bind_checkout({"id": "chV", "order": {"id": "ord_mgV", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_plus"}, "subscription": {"id": "mgsubV"}})
_cur.execute("select status, granted from orders where id=%s", (_ov_id,))
_st15, _gr15 = _cur.fetchone()
check("G15 checkout.completed 后 order: status=paid / granted=FALSE (cycle credits 未授)",
      _st15 == "paid" and _gr15 is False, f"st={_st15} granted={_gr15}")
_bal15 = spendable_balance(uidV)
# subscription.paid — credits 此刻授予:
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), sub_id="mgsubV"))
_cur.execute("select count(*) from ledger where user_id=%s and kind='grant_plan'", (uidV,))
_ng15 = _cur.fetchone()[0]
check("G15b subscription.paid 后: grant_plan 恰一笔 / credits +100 / membership marker 更新",
      _ng15 == 1 and spendable_balance(uidV) == _bal15 + 100
      and _m_row(uidV)[4] == PS, f"grants={_ng15} bal={spendable_balance(uidV)}")
# package/top-up mark_paid 语义保持 (窄 DB proof):
uW, uidW = _new_user("mgW")
_cur.execute(
    "insert into orders (user_id, kind, item_id, provider, provider_order_id, status, amount_cents, currency, credits_snapshot, created_at)"
    " values (%s, 'package', 1, 'creem', 'ch:mgW', 'pending', 500, 'USD', 100, %s) returning id",
    (uidW, _ts))
_ow = _cur.fetchone()[0]
with db() as con:
    orders_repo.complete_package_order(con, _ow, "ord_mgW", _ts)
_cur.execute("select status, granted from orders where id=%s", (_ow,))
check("G15c package mark_paid → status=paid / granted=TRUE (语义保持)",
      _cur.fetchone() == ("paid", True))

# ═══ Source guards ═══
_sub_src = open(_os.path.join(_REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
_repo_src = open(_os.path.join(_REPO, "core", "repo", "orders.py"), encoding="utf-8").read()
_schema_src = open(_os.path.join(_REPO, "core", "database.py"), encoding="utf-8").read()
_topup_src = open(_os.path.join(_REPO, "app", "routes", "topup.py"), encoding="utf-8").read()
check("SG1 memberships PK = user_id (单行权威 SQL)",
      "PRIMARY KEY (user_id)" in open(_os.path.join(_REPO, "core", "database.py"), encoding="utf-8").read()
      and "provider_subscription_id TEXT UNIQUE" in open(_os.path.join(_REPO, "core", "database.py"), encoding="utf-8").read())
_po = _sub_src.split("def plan_of")[1].split("\ndef ")[0]
check("SG2 plan_of 无多行选择逻辑 (单行 read + live 判定)",
      "membership_row(" in _po and "ORDER BY" not in _po)
check("SG3 subscription.update handler 存在且走 product 映射",
      "def _ev_update" in _sub_src and "plan_by_product_id" in _sub_src)
check("SG4 事件定位走 membership_by_sub (provider 绑定权威)",
      "membership_by_sub" in _sub_src)
check("SG5 reconcile 消费 membership_row (不遍历 orders)",
      "membership_row(" in open(_os.path.join(_REPO, "core", "economy", "reconcile.py"), encoding="utf-8").read())
check("SG6 /subscribe 有 plan_change_required 拦截",
      "plan_change_required" in _topup_src)
check("SG7 entitlement profile 单点 (_require_profile + 未知 raise)",
      "_require_profile" in open(_os.path.join(_REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read())
_tmpl = open(_os.path.join(_REPO, "app", "templates", "account", "credits.html"), encoding="utf-8").read()
check("SG8 Account badge 展示 access.membership.name (当前 tier)",
      "{{ m.name }}" in _tmpl)
check("SG9 旧 upsert_membership 单体 writer 已拆 (bind/state 语义分离)",
      "def upsert_membership(" not in _repo_src
      and "def bind_membership(" in _repo_src
      and "def upsert_membership_state(" in _repo_src
      and "MembershipBindingError" in _repo_src)
_state_blk = _repo_src.split("def upsert_membership_state")[1].split("\ndef ")[0]
check("SG10 state update 禁止换绑 (UPDATE 定位 sub, 无 sub 写列)",
      "provider_subscription_id = " not in _state_blk.replace(
          "WHERE provider_subscription_id = %s", "").replace(
          "WHERE provider_subscription_id = %s FOR UPDATE", ""))
_legacy_writers = ("def upsert_user_plan(", "def user_plan_row(",
                   "def set_cancel_scheduled(", "def user_subscriptions(")
check("SG11 legacy user_plans production writers 零残留",
      all(w not in _repo_src for w in _legacy_writers)
      and all(w not in _sub_src for w in _legacy_writers))
check("SG12 runtime 全仓 user_plans 零引用",
      "user_plans" not in _repo_src and "user_plans" not in _sub_src
      and "user_plans" not in _schema_src)
check("SG13 retrieve_checkout 存在 (provider 收敛单点)",
      "def retrieve_checkout" in open(_os.path.join(_REPO, "core", "economy", "creem.py"), encoding="utf-8").read())
check("SG14 extract_product_id canonical dict 形态",
      'isinstance(product, dict)' in _sub_src
      and 'product.get("id")' in _sub_src)
_topup_g = open(_os.path.join(_REPO, "app", "routes", "topup.py"), encoding="utf-8").read()
check("SG15 /subscribe sellable gate (单点) + open order 检查 + FOR UPDATE",
      "_tier.sellable" in _topup_g
      and "membership_is_effective" in _topup_g
      and "lock_user_for_checkout" in _topup_g
      and "open_plan_order" in _topup_g
      and "retrieve_checkout" in _topup_g)
_pages_g = open(_os.path.join(_REPO, "app", "routes", "pages.py"), encoding="utf-8").read()
check("SG17 occupied 语义单一纯函数 (route+bind 共用, 无第二套 predicate)",
      "def membership_is_occupied(" in _sub_src
      and "membership_is_occupied" in _topup_g
      and "membership_is_occupied" in _repo_src
      and "renews_at or 0" not in _repo_src.split("def bind_membership")[1].split("\ndef ")[0])
_checkouts_url = open(_os.path.join(_REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
check("SG18 retrieve query-encoded 合同 (无 path 形式)",
      '"/v1/checkouts?"' in _checkouts_url
      and "urlencode" in _checkouts_url
      and "/v1/checkouts/" not in _checkouts_url.split("def retrieve_checkout")[1].split("\ndef ")[0])
import ast as _ast
_sub_fn = [n for n in _ast.parse(_topup_g).body
           if isinstance(n, _ast.AsyncFunctionDef) and n.name == "subscribe_checkout"][0]
_all_withs = [n for n in _ast.walk(_sub_fn) if isinstance(n, _ast.With)]
_locked_bodies = [_ast.dump(n) for n in _all_withs
                  if "lock_user_for_checkout" in _ast.dump(n)]
check("SG19 INSERT 在每个锁事务内 (AST; 两处 lock = 首建+expired-recreate)",
      len(_locked_bodies) == 2
      and all("insert_order" in b for b in _locked_bodies))
_bind_blk = _sub_src.split("def bind_checkout")[1].split("\ndef ")[0]
check("SG20 bind_checkout 不调用 mark_paid (complete_plan_checkout 域 writer)",
      "mark_paid(" not in _bind_blk
      and "complete_plan_checkout(" in _bind_blk)
_subw_blk = _repo_src.split("\ndef complete_plan_checkout")[1].split("\ndef ")[0]
_upd_i = _subw_blk.index("UPDATE orders")
_subw_sql = _subw_blk[_upd_i:_subw_blk.index("RETURNING", _upd_i)]
check("SG21 plan checkout writer: guarded paid, never granted",
      "granted" not in _subw_sql
      and "status='paid'" in _subw_sql and "paid_at=COALESCE" in _subw_sql
      and "status IN ('pending','failed')" in _subw_sql,
      _subw_sql[:120])
_orig_paid_blk = _repo_src.split("def mark_paid")[1].split("\ndef ")[0]
check("SG22 package finalize writer 保持 granted=TRUE (complete_package_order)",
      "granted=TRUE" in _repo_src.split("def complete_package_order")[1].split("\ndef ")[0])
check("SG19b HTTP 全在锁事务外 (AST: 锁 with 块内无 retrieve/create)",
      bool(_locked_bodies)
      and all("retrieve_checkout" not in b and "create_checkout" not in b
              for b in _locked_bodies))
check("SG16 Account 可售列表消费同一 sellable 单点",
      "t.sellable" in _pages_g.split("def account_credits")[1].split("async def ")[0]
      and "plan_catalog.catalog()" in _pages_g.split("def account_credits")[1].split("async def ")[0])

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
