#!/usr/bin/env python3
"""Membership lifecycle — single-row membership authority, reconcile
boundaries, and the economy/membership closure (re-subscribe,
downgrade, upgrade refunds, platform income serialization).
"""
from _harness import PG, REPO, TEST_DB, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Membership reconcile
#
# Membership reconcile canonical invariants — 四事实边界合同.
#
# 锁住: membership_row 显式 read model、唯一 paid-period 实现、
# 补偿 eligibility (active/scheduled_cancel only)、scheduled_cancel
# marker 保留、last_grant 判据、ledger 幂等防线、provider failure
# best-effort。遵循 canonical fail-closed 规则。
# ════════════════════════════════════════════════════════════════════════
section('Membership reconcile')

import os as _os, sys as _sys
import sys
import json, time


import psycopg2
from database import db
from repo import orders as orders_repo
from economy import reconcile as _rec
from economy.billing import spendable_balance


_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_ts = int(time.time())

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts, _ts))
    return u, _cur.fetchone()[0]

def _seed_plan_order(uid, monthly_credits=100):
    _cur.execute(
        "insert into plans (code, name, monthly_credits, price_cents, sort_order,"
        " created_at, updated_at)"
        " values (%s, %s, %s, 500,"
        " (SELECT COALESCE(MAX(sort_order),0)+1 FROM plans WHERE code <> 'free'),"
        " %s, %s) returning id",
        (f"plus{uid}", f"PLUS{uid}", monthly_credits, _ts, _ts))
    plan_id = _cur.fetchone()[0]
    _cur.execute("update plans set creem_product_id=%s, price_cents=500 where id=%s",
                 (f"prod_rc_{uid}", plan_id))
    _cur.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, created_at, paid_at)"
        " values (%s, 'plan', %s, 'creem', %s, 'paid', 500, %s, %s) returning id",
        (uid, plan_id, f"sub:test{uid}", _ts, _ts))
    oid = _cur.fetchone()[0]
    # (sub id 返回纯 id; orders/memberships 落库带 sub: 前缀)
    # membership 建档 (新单行模型; fixture 直建, sub 唯一)
    _cur.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, NULL, %s)"
        " on conflict (user_id) do update set plan_id=excluded.plan_id,"
        " provider_subscription_id=excluded.provider_subscription_id, status=excluded.status",
        (uid, plan_id, f"sub:test{uid}", "active", _ts, _ts))
    return plan_id, oid, f"test{uid}"

def _sub_snapshot(status, p_start, p_end, sub_id="txn-sub", product=None):
    return {"object": "subscription", "id": sub_id, "status": status,
            "current_period_start_date": p_start, "current_period_end_date": p_end,
            "product": {"id": product or f"prod_rc_default"},
            "last_transaction_id": f"txn-{sub_id}"}

# paid-transaction proof fake: a paid invoice matching the snapshot's own
# (sub id, period) — the proof channel reconcile now requires
def _txn_fetch_for(snap):
    def _f(txn_id):
        return {"object": "transaction", "id": txn_id, "status": "paid",
                "type": "invoice", "subscription": snap["id"],
                "period_start": PS, "period_end": PE}
    return _f

def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _plan_row(uid):
    _cur.execute(
        "select status, renews_at, last_grant_period_start, cancel_scheduled_at"
        " from memberships where user_id=%s", (uid,))
    return _cur.fetchone()

def _ledger_grants(uid):
    _cur.execute(
        "select count(*) from ledger where user_id=%s and kind='grant_plan'", (uid,))
    return _cur.fetchone()[0]

# ═══ R1 — read contract (单行 membership 模型) ═══
u1, uid1 = _new_user("rc1")
pid1, oid1, sub1 = _seed_plan_order(uid1)
with db() as con:
    m = orders_repo.membership_row(con, uid1)
    o = orders_repo.order_by_id_for_update(con, oid1)
check("R1a membership_row 显式字段 (membership+plan join)",
      m is not None and {"plan_id", "provider_subscription_id", "status",
                         "renews_at", "last_grant_period_start",
                         "cancel_scheduled_at", "code", "name"} <= set(dict(m)),
      str(sorted(dict(m or {}).keys())))
check("R1b order fact 正确 (paid, item=plan)",
      dict(o)["status"] == "paid" and dict(o)["item_id"] == pid1)
check("R1c fixture 建档后 membership 绑定 sub",
      dict(m)["provider_subscription_id"] == f"sub:{sub1}")

# ═══ R2 — active missing grant → exactly one grant ═══
PS, PE = _ts - 3600, _ts + 2592000
def _fake_fetch(snap):
    def _f(sub_id):
        return snap
    return _f

_bal0 = spendable_balance(uid1)
_gr0 = _ledger_grants(uid1)
_snap = _sub_snapshot("active", _iso(PS), _iso(PE), sub_id=sub1, product=f"prod_rc_{uid1}")
_rec.fetch_subscription = _fake_fetch(_snap)
_rec.fetch_transaction = _txn_fetch_for(_snap)
r = _rec.reconcile_user(uid1)
_bal1 = spendable_balance(uid1)
pr = _plan_row(uid1)
check("R2a active 缺授 → exactly one grant (credits=100)",
      r["granted"] == 1 and _bal1 - _bal0 == 100 and _ledger_grants(uid1) - _gr0 == 1,
      f"r={r} Δbal={_bal1 - _bal0}")
check("R2b membership active + renews_at=period_end + marker=period_start",
      pr[0] == "active" and pr[1] == PE and pr[2] == PS, str(pr))

# ═══ R3 — second reconcile same period ═══
_bal2 = spendable_balance(uid1)
_gr2 = _ledger_grants(uid1)
r = _rec.reconcile_user(uid1)
check("R3 同 period 二次 reconcile: credits 零增 / ledger 零新行",
      _bal2 == spendable_balance(uid1) and _gr2 == _ledger_grants(uid1) and r["granted"] == 0,
      f"r={r}")

# ═══ R4 — ledger idempotency (marker 丢失) ═══
_cur.execute("update memberships set last_grant_period_start=NULL where user_id=%s", (uid1,))
_bal4 = spendable_balance(uid1)
_gr4 = _ledger_grants(uid1)
r = _rec.reconcile_user(uid1)
check("R4 marker 丢失但同 idem ledger 已在 → 不双授 (billing 幂等防线)",
      spendable_balance(uid1) == _bal4 and _ledger_grants(uid1) == _gr4,
      f"r={r}")
# marker 被 helper 重新落回 (grant no-op 后 upsert 仍写 marker — 状态机自愈):
pr = _plan_row(uid1)
check("R4b marker 自愈回 period_start (无第二笔账)", pr[2] == PS and _ledger_grants(uid1) == _gr4 + 0, str(pr))

# ═══ R5 — triaging ═══
u2, uid2 = _new_user("rc2")
_seed_plan_order(uid2)
_cur.execute("delete from memberships where user_id=%s", (uid2,))  # R5 前提: 无 current membership
_bal5 = spendable_balance(uid2)
_snap = _sub_snapshot("trialing", _iso(PS), _iso(PE))
_rec.fetch_subscription = _fake_fetch(_snap)
_rec.fetch_transaction = _txn_fetch_for(_snap)
r = _rec.reconcile_user(uid2)
check("R5 trialing: credits 零变 / membership 不被写成 active",
      spendable_balance(uid2) == _bal5 and _plan_row(uid2) is None, f"r={r}")

# ═══ R6 — past_due ═══
u3, uid3 = _new_user("rc3")
pid3, _, sub3 = _seed_plan_order(uid3)
with db() as con:
    orders_repo.upsert_membership_state(con, sub3, "past_due",
                                        renews_at=PE)
_bal6 = spendable_balance(uid3)
_snap = _sub_snapshot("past_due", _iso(PS), _iso(PE), sub_id=sub3, product=f"prod_rc_{uid3}")
_rec.fetch_subscription = _fake_fetch(_snap)
_rec.fetch_transaction = _txn_fetch_for(_snap)
r = _rec.reconcile_user(uid3)
pr = _plan_row(uid3)
check("R6 past_due: 不 grant / membership 仍 past_due (reconcile 不强行 active)",
      spendable_balance(uid3) == _bal6 and pr[0] == "past_due", f"r={r} plan={pr}")

# ═══ R7 — scheduled_cancel ═══
u4, uid4 = _new_user("rc7")
pid4, _, sub4 = _seed_plan_order(uid4)
_snap = _sub_snapshot("scheduled_cancel", _iso(PS), _iso(PE), sub_id=sub4, product=f"prod_rc_{uid4}")
_rec.fetch_subscription = _fake_fetch(_snap)
_rec.fetch_transaction = _txn_fetch_for(_snap)
_bal7 = spendable_balance(uid4)
r = _rec.reconcile_user(uid4)
pr = _plan_row(uid4)
check("R7a scheduled_cancel 缺授 → grant once (当期权益保留)",
      r["granted"] == 1 and spendable_balance(uid4) - _bal7 == 100, f"r={r}")
check("R7b membership 有效 + cancel_scheduled_at 非空 + marker 正确",
      pr[0] == "active" and pr[3] is not None and pr[2] == PS and pr[1] == PE, str(pr))
# 再 reconcile: 一次补授即可, marker 防重 + marker 不被清:
_bal7b = spendable_balance(uid4)
r = _rec.reconcile_user(uid4)
pr = _plan_row(uid4)
check("R7c 二次 reconcile 零新 grant 且 cancellation marker 仍在",
      spendable_balance(uid4) == _bal7b and r["granted"] == 0 and pr[3] is not None, f"r={r}")

# ═══ R8 — expired/canceled/unknown ═══
for st in ("canceled", "expired", "weird_state"):
    u5, uid5 = _new_user("rc8")
    _seed_plan_order(uid5)
    _cur.execute("delete from memberships where user_id=%s", (uid5,))  # 前提: 无 current membership
    _bal8 = spendable_balance(uid5)
    _snap = _sub_snapshot(st, _iso(PS), _iso(PE))
    _rec.fetch_subscription = _fake_fetch(_snap)
    _rec.fetch_transaction = _txn_fetch_for(_snap)
    r = _rec.reconcile_user(uid5)
    check(f"R8 {st}: 不 grant / 无 membership 写入",
          spendable_balance(uid5) == _bal8 and _plan_row(uid5) is None, f"r={r}")

# ═══ R9 — provider failure ═══
u6, uid6 = _new_user("rc9")
_seed_plan_order(uid6)
_cur.execute("delete from memberships where user_id=%s", (uid6,))  # 前提: 无 current membership
_bal9 = spendable_balance(uid6)
_rec.fetch_subscription = lambda sub_id: None
r = _rec.reconcile_user(uid6)  # 不抛
check("R9 provider None: 正常返回 / 零 grant / 零 membership mutation",
      isinstance(r, dict) and spendable_balance(uid6) == _bal9 and _plan_row(uid6) is None, f"r={r}")
# R9b: fetch_subscription 的真实网络失败路径 = 内部 try/except 吞掉后
# 返回 None (reconcile 对其零感知)。monkeypatch 其内部 urlopen 复现:
import urllib.request as _ur
def _net_down(url, *a, **kw):
    raise ConnectionError("network down")
_orig_urlopen = _ur.urlopen
_ur.urlopen = _net_down
try:
    r = _rec.fetch_subscription("sub:test" + str(uid6))
    check("R9b 真实网络异常 → fetch_subscription 返回 None (best-effort 吞)",
          r is None)
finally:
    _ur.urlopen = _orig_urlopen
r = _rec.reconcile_user(uid6)
check("R9b2 网络失败后 reconcile_user 正常返回 / 零 mutation",
      isinstance(r, dict) and spendable_balance(uid6) == _bal9 and _plan_row(uid6) is None, f"r={r}")

# ═══ Source guards ═══
_rec_src = open(_os.path.join(REPO, "core", "economy", "reconcile.py"), encoding="utf-8").read()
_rec_code = "\n".join(l for l in _rec_src.split("\n")
                      if not l.strip().startswith(("#", '"', "'")))
check("SG1 reconcile 不 import/call billing.grant", "billing import grant" not in _rec_src
      and "grant(" not in _rec_code.replace("apply_paid_period(", "").replace("granted", ""))
check("SG2 reconcile 不直接调用 membership writers (upsert_*)",
      "upsert_membership(" not in _rec_code and "upsert_user_plan" not in _rec_code)
check("SG3 reconcile 调用共享 paid-period helper", "apply_paid_period" in _rec_code)
check("SG4 reconcile 无裸 row[\"status\"] 读取 (注释外)",
      'row["status"]' not in _rec_code and 'row.get("status")' not in _rec_code)
check("SG5 reconcile 读取 last_grant_period_start 判据",
      "last_grant_period_start" in _rec_code)
check("SG6 eligibility 无 trialing/past_due (仅 active/scheduled_cancel)",
      "trialing" not in _rec_code.replace("trialing /", "")
      and '"active", "scheduled_cancel"' in _rec_code)
_sub_src = open(_os.path.join(REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
_paid_blk = _sub_src.split("def apply_paid_period")[1].split("\ndef ")[0]
_ev_blk = _sub_src.split("def _ev_paid")[1].split("\ndef ")[0]
check("SG7 _ev_paid 与 reconcile 共用同一 helper",
      "apply_paid_period" in _ev_blk)
check("SG8 idem key 仍为 plan:{uid}:{period_start}",
      'f"plan:{order[\'user_id\']}:{period_start}"' in _paid_blk)
check("SG9 grant 在 upsert 前 (顺序保持)",
      _paid_blk.index("grant(") < _paid_blk.index("upsert_membership"))
_auth_src = open(_os.path.join(REPO, "app", "routes", "auth_route.py"), encoding="utf-8").read()
check("SG10 _login_maintenance 仍调用 reconcile_user",
      "reconcile_user" in _auth_src)


# ════════════════════════════════════════════════════════════════════════
# Membership governance
#
# Membership governance canonical invariants — 整链单 suite.
#
# 多档 Membership 作为 quota/entitlement 唯一稳定事实源:
# memberships 单行权威、provider subscription 绑定、plan→entitlement
# 显式 profile、首购/复购/tier-change、paid/cancel/expire/reconcile、
# migration fail-closed。遵循 canonical fail-closed 规则。
# ════════════════════════════════════════════════════════════════════════
section('Membership governance')

import os as _os, sys as _sys
import sys, json, time


import psycopg2
from database import db
from repo import orders as orders_repo
from economy import subscription as _sub
from economy import reconcile as _rec
from economy.billing import spendable_balance
from governance.entitlements import (resolve, resolve_from_membership, EntitlementConfigurationError)

def _prof_exists(code: str) -> bool:
    """entitlement_profile_exists retired (dead fn): a profile exists iff
    the tier table carries a configured row for the code (unknown codes
    resolve to the free profile, never raising)."""
    from governance.entitlements import resolve_from_membership
    try:
        return resolve_from_membership({"member": True, "code": code}).get(
            "plan_code") == code
    except Exception:
        return False


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


# migration 子进程用文件传 DSN (与 runner 的 TEST_DB_URL 同源):
_DSN_FILE = "/tmp/mg_gov_dsn.txt"
with open(_DSN_FILE, "w") as _f:
    _f.write(TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ["FLASHSHARE_TEST_DSN_FILE"] = _DSN_FILE
_con = psycopg2.connect(PG); _con.autocommit = True
_cur_mgv = _con.cursor()
_ts_mgv = int(time.time())
FUT = _ts_mgv + 30 * 86400
# 幂等重跑: 清理本 suite 之前的半途残留 (mg 前缀用户及其行)
_cur_mgv.execute("delete from memberships where user_id in (select id from users where username like 'mg%')")
_cur_mgv.execute("delete from orders where user_id in (select id from users where username like 'mg%')")
_cur_mgv.execute("delete from ledger where user_id in (select id from users where username like 'mg%')")
_cur_mgv.execute("delete from plans where code like 'mg_%'")
_cur_mgv.execute(
    "select id from users where username like 'mg%'")
purge_users(_cur_mgv, [r[0] for r in _cur_mgv.fetchall()])

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur_mgv.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts_mgv, _ts_mgv))
    return u, _cur_mgv.fetchone()[0]

def _seed_plan(code, credits=100, price_id=None):
    # 幂等 seed: plus 为 init_db 种子行, 其余测试自建
    _cur_mgv.execute(
        "insert into plans (code, name, monthly_credits, price_cents, creem_product_id, sort_order, created_at, updated_at)"
        " values (%s, %s, %s, 500, %s,"
        " (SELECT COALESCE(MAX(p.sort_order),0)+1 FROM plans p WHERE p.code <> 'free'), %s, %s)"
        " on conflict (code) do update set monthly_credits=excluded.monthly_credits,"
        " creem_product_id=coalesce(plans.creem_product_id, excluded.creem_product_id),"
        " price_cents=excluded.price_cents"
        " returning id",
        (code, code.upper(), credits, price_id, _ts_mgv, _ts_mgv))
    _refresh_catalog()
    return _cur_mgv.fetchone()[0]

def _seed_order(uid, plan_id, sub_id, ch_id=None):
    """canonical first-checkout fixture: pending plan Order, provider
    identity = the webhook checkout binding ch:<id>, paid_at NULL.
    Completion happens through bind_checkout(order_id=...)'s guarded
    complete_plan_checkout writer — never a pre-paid historical row.
    ch_id MUST be the checkout.completed obj.id the test will deliver
    (bind_checkout validates the ch: identity match)."""
    _cur_mgv.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, currency, created_at)"
        " values (%s, 'plan', %s, 'creem', %s, 'pending', 500, 'USD', %s)"
        " on conflict (provider_order_id) do update set item_id=excluded.item_id"
        " returning id",
        (uid, plan_id, f"ch:{ch_id or sub_id}", _ts_mgv))
    return _cur_mgv.fetchone()[0]

def _sub_snap(status, p_start, p_end, product=None, sub_id="mgsub1", txn=None):
    d = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": p_start, "current_period_end_date": p_end,
         # paid-proof contract: product + last_transaction_id must be present
         # AND the product must match the locally bound plan product.
         # Transaction ids are unique per payment (Creem contract; the
         # replay gate matches them exactly) — a renewal is a NEW payment
         # and must carry its own id, never the base cycle's.
         "product": product or {"id": "price_plus"},
         "last_transaction_id": txn or f"txn_{sub_id}"}
    return d

def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _m_row(uid):
    _cur_mgv.execute("select plan_id, provider_subscription_id, status, renews_at,"
                 " last_grant_period_start, cancel_scheduled_at from memberships where user_id=%s", (uid,))
    return _cur_mgv.fetchone()

def _grants(uid):
    _cur_mgv.execute("select count(*) from ledger where user_id=%s and kind='grant_plan'", (uid,))
    return _cur_mgv.fetchone()[0]

PS_mgv, PE_mgv = _ts_mgv - 3600, FUT

# ═══ 链1: Free → Free quota ═══
uF, uidF = _new_user("mgF")
p = project(uidF)
check("G1 Free: membership None + Free entitlements + usage",
      p["membership"] is None and p["entitlements"]["plan_code"] == "free"
      and resolve(uidF)["plan_code"] == "free")

# ═══ 链2: Plus current → Plus quota (首购 → paid) ═══
uP, uidP = _new_user("mgP")
plan_plus = _seed_plan("plus", 100, "price_plus")
_cur_mgv.execute("select id from plans where code='plus' and id != %s", (plan_plus,))
oid = _seed_order(uidP, plan_plus, "mgsub1", ch_id="ch1")
# 首购建档 (bind_checkout 的 membership 建立):
_sub.bind_checkout({"id": "ch1", "order": {"id": "ord_mg1", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_plus"}, "subscription": {"id": "mgsub1"}}, order_id=oid)
# paid → grant + membership active:
res = _sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS_mgv), _iso(PE_mgv)))
m = _m_row(uidP)
check("G2 首购+paid: exactly one grant + membership active (sub 绑定)",
      _grants(uidP) == 1 and spendable_balance(uidP) == 100
      and m[2] == "active" and m[1] == "sub:mgsub1" and m[3] == PE_mgv, str(res))
pp = project(uidP)
check("G2b Plus current → Plus entitlements (整链跟随)",
      pp["membership"]["member"] is True and pp["entitlements"]["plan_code"] == "plus"
      and resolve(uidP)["plan_code"] == "plus")

# ═══ 链3: 不同 live sub → 拒绝换绑 ═══
from repo.orders import MembershipBindingError as _MBE
try:
    with db() as con:
        orders_repo.upsert_membership_state(con, "mgsub2", "active",
                                            renews_at=PE_mgv)
    check("G3 不同 live sub state update → 拒绝换绑 (原行不变)", False, "no raise")
except _MBE:
    m = _m_row(uidP)
    _cur_mgv.execute("select count(*) from memberships where user_id=%s", (uidP,))
    check("G3 不同 live sub state update → 拒绝换绑 (原行不变)",
          _cur_mgv.fetchone()[0] == 1 and m[1] == "sub:mgsub1"
          and m[3] == PE_mgv and m[2] == "active", str(m))
except Exception as exc:
    check("G3 不同 live sub state update → 拒绝换绑", False, str(exc))
try:
    with db() as con:
        orders_repo.bind_membership(con, uidP, plan_plus, "mgsub2", "active",
                                    started_at=_ts_mgv, renews_at=PE_mgv)
    check("G3b bind 在 live Membership 上 → 拒绝", False, "no raise")
except _MBE:
    _cur_mgv.execute("select count(*) from memberships where user_id=%s", (uidP,))
    check("G3b bind 在 live Membership 上 → 拒绝 (sub 仍 mgsub1)",
          _cur_mgv.fetchone()[0] == 1 and _m_row(uidP)[1] == "sub:mgsub1")

# ═══ 链4: 第二付费 tier → 自己的 entitlement (非 Free) ═══
# 第二付费 tier 使用 canonical pro (provider product 只映射 plus/pro):
uT, uidT = _new_user("mgT")
_cur_mgv.execute("select id from plans where code='pro'")
plan_pro = _cur_mgv.fetchone()[0]
_cur_mgv.execute("update plans set creem_product_id='price_pro' where code='pro'")
_refresh_catalog()
PRO_FILES, PRO_BYTES = 2000, 20 * 1024 ** 3
_oidT = _seed_order(uidT, plan_pro, "mgsubT", ch_id="chT")
_sub.bind_checkout({"id": "chT", "order": {"id": "ord_mgT", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_pro"}, "subscription": {"id": "mgsubT"}}, order_id=_oidT)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS_mgv), _iso(PE_mgv), sub_id="mgsubT"))
r = resolve(uidT)
check("G4 第二 tier (pro) → 自己的 entitlement (2000 files/20GiB, 绝不 Free)",
      r["plan_code"] == "pro" and r["ingress_files_24h"] == PRO_FILES
      and r["ingress_bytes_24h"] == PRO_BYTES, str(r["plan_code"]))
check("G4b entitlement_profile_exists 单点",
      _prof_exists("plus") and _prof_exists("pro")
      and not _prof_exists("nope"))

# ═══ 链5: subscription.update 换 tier (同一 sub) ═══
res = _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS_mgv), _iso(PE_mgv), product={"id": "price_pro"}, sub_id="mgsubT"))
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
    _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS_mgv), _iso(PE_mgv), product={"id": "price_unknown"}, sub_id="mgsubT"))
    check("G5b product 无本地唯一映射 → KeyError fail loud", False, "no raise")
except KeyError:
    check("G5b product 无本地唯一映射 → KeyError fail loud", True)

# ═══ 链6: paid renewal → idem once ═══
PS2, PE2 = _ts_mgv + 86400, _ts_mgv + 31 * 86400
_bal = spendable_balance(uidP); _g = _grants(uidP)
# renewal = distinct payment → distinct transaction id; the second,
# identical event is the replay and keeps the same id
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2), txn="txn_mgsub1_renew"))
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2), txn="txn_mgsub1_renew"))  # replay
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
# 同一周期内 end 更早的 expired (当前周期被提前终止) → 照常生效
_sub.handle_event("subscription.expired", _sub_snap("expired", _iso(PS2), _iso(_ts_mgv - 10)))
pp = project(uidP)
check("G8 period end → Free (membership fact 保留, entitlements free)",
      pp["membership"]["member"] is False and pp["entitlements"]["plan_code"] == "free")

# ═══ 链9: reconcile 跟 current Membership sub ═══
uR, uidR = _new_user("mgR")
plan_r = _seed_plan("plus", 100, "price_plus")
_oidR = _seed_order(uidR, plan_r, "mgsubR", ch_id="chR")
_sub.bind_checkout({"id": "chR", "order": {"id": "ord_mgR", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_plus"}, "subscription": {"id": "mgsubR"}}, order_id=_oidR)
# 历史 plan order (非当前 sub) — reconcile 不得遍历它:
_old_plan = _seed_plan("mg_old", 777)
_cur_mgv.execute(
    "insert into orders (user_id, kind, item_id, provider, provider_order_id, status, amount_cents, created_at, paid_at)"
    " values (%s, 'plan', %s, 'creem', 'sub:mg_old_sub', 'paid', 500, %s, %s)",
    (uidR, _old_plan, _ts_mgv, _ts_mgv))
_snap_g9 = _sub_snap("active", _iso(PS_mgv), _iso(PE_mgv), sub_id="mgsubR")
_rec.fetch_subscription = lambda sid: _snap_g9 if sid == "mgsubR" else None
_rec.fetch_transaction = lambda t: {"object": "transaction", "id": t,
                                   "status": "paid", "type": "invoice",
                                   "subscription": "mgsubR",
                                   "period_start": PS_mgv, "period_end": PE_mgv}
r = _rec.reconcile_user(uidR)
check("G9 reconcile 只跟 current Membership sub (不遍历历史 orders)",
      r["checked"] == 1 and r["granted"] == 1 and spendable_balance(uidR) == 100
      and _m_row(uidR)[0] == plan_r, f"r={r}")

# ═══ 链10: unknown profile tier — 收费前拒绝 + 异常防线 ═══
# 新逻辑: 无 profile 的付费档在 /subscribe 收费前拒绝 (plan_not_ready),
# 不产生 order/checkout。此处直测 gate 消费的同一 profile 单点:
uU, uidU = _new_user("mgU")
plan_x = _seed_plan("mg_unknown_tier", 100, "price_x")
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidU,))
check("G10pre unknown-profile plan 收费前拒绝 (gate 单点 + gate 后零 order)",
      not _prof_exists("mg_unknown_tier")
      and _cur_mgv.fetchone()[0] == 0)
# 防御测试: 若 DB/外部异常导致未知 active Membership 已存在 (绕过 gate),
# read 面 fail loud、authorization fail-safe — 异常防线, 非销售流程:
_oidU = _seed_order(uidU, plan_x, "mgsubU", ch_id="chU")
_sub.bind_checkout({"id": "chU", "order": {"id": "ord_mgU", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_x"}, "subscription": {"id": "mgsubU"}}, order_id=_oidU)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS_mgv), _iso(PE_mgv), sub_id="mgsubU"))
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
    cid = f"ch_fake_{order_id}"
    url = f"https://creem.test/c/{order_id}"
    # A successful provider create makes that checkout immediately
    # retrievable by the same provider identity. Concurrency tests must
    # model this contract; otherwise a loser that observes stored ch:*
    # hits an artificial "provider down" path unrelated to Order
    # convergence.
    _RT[cid] = {"status": "pending", "checkout_url": url}
    return {"ok": True, "checkout_url": url, "checkout_id": cid}
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
_oidL = _seed_order(uidL, plan_plus, "mgsubL", ch_id="chL")
_sub.bind_checkout({"id": "chL", "order": {"id": "ord_mgL", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_plus"}, "subscription": {"id": "mgsubL"}}, order_id=_oidL)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS_mgv), _iso(PE_mgv), sub_id="mgsubL"))
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
_cur_mgv.execute("update plans set creem_product_id='price_plus' where id=%s", (plan_plus,))
_refresh_catalog()
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur_mgv.execute("select id from orders where user_id=%s and kind='plan' order by id desc limit 1", (uidS,))
_oi1 = _cur_mgv.fetchone()[0]
check("G12b 首次 subscribe → 1 order + 1 create_checkout",
      st == 200 and d.get("reused") is None and _creem_calls["create"] == 1
      and _oi1 > 0, f"st={st} d={d}")
# 第二次 (webhook 前) → retrieve pending → reuse URL, 不建第二 order/create:
_RT[f"ch_fake_{_oi1}"] = {"status": "pending", "checkout_url": f"https://creem.test/c/{_oi1}"}
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidS,))
_n_orders = _cur_mgv.fetchone()[0]
check("G12c webhook 前再 subscribe → reuse 同一 checkout URL / 仍 1 order / 零新 create",
      st == 200 and d.get("reused") is True and d.get("checkout_url") == f"https://creem.test/c/{_oi1}"
      and _n_orders == 1 and _creem_calls["create"] == 1, f"st={st} d={d} n={_n_orders}")
# expired → 终结旧 order, 重新创建:
_RT[f"ch_fake_{_oi1}"] = {"status": "expired", "checkout_url": f"https://creem.test/c/{_oi1}"}
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan' and status='failed'", (uidS,))
_nf = _cur_mgv.fetchone()[0]
check("G12d expired checkout → 旧 order failed + 新 create",
      st == 200 and _nf == 1 and _creem_calls["create"] == 2, f"st={st} failed={_nf}")
# ── G12f 系列: nonterminal / unknown retrieve status 不得当 terminal proof ──
# 场景: 用户重新走到 pending+ch 状态 (G12d 建了新 order+create; 复用该 order)
_cur_mgv.execute("select id, provider_order_id from orders where user_id=%s"
             " and kind='plan' and status='pending' order by id desc limit 1", (uidS,))
_orow = _cur_mgv.fetchone()
_oi_f, _ch_f = _orow[0], _orow[1]
_RT[_ch_f[3:]] = {"status": "pending"}   # pending NO checkout_url
_nc = _creem_calls["create"]
st_f1, d_f1 = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidS,))
_n1 = _cur_mgv.fetchone()[0]
_cur_mgv.execute("select status from orders where id=%s", (_oi_f,))
_s1 = _cur_mgv.fetchone()[0]
check("G12f-1 pending 无 checkout_url → 原 Order 仍 pending / 零新 create / 零新 Order",
      _s1 == "pending" and _n1 == 2 and _creem_calls["create"] == _nc
      and st_f1 in (200, 409, 502) and not (st_f1 == 200 and d_f1.get("checkout_id") not in (None, _ch_f[3:])),
      f"st={st_f1} d={d_f1} status={_s1} n={_n1} creates={_creem_calls['create']}/{_nc}")
_RT[_ch_f[3:]] = {"status": "processing"}   # processing NO url
st_f2, d_f2 = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur_mgv.execute("select status from orders where id=%s", (_oi_f,))
_s2 = _cur_mgv.fetchone()[0]
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidS,))
_n2 = _cur_mgv.fetchone()[0]
check("G12f-2 processing 无 checkout_url → 同上 (intent 不释放)",
      _s2 == "pending" and _n2 == 2 and _creem_calls["create"] == _nc, f"st={st_f2} status={_s2} n={_n2}")
_RT[_ch_f[3:]] = {"status": "mystery_future_state", "checkout_url": "https://creem.test/x"}
st_f3, d_f3 = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur_mgv.execute("select status from orders where id=%s", (_oi_f,))
_s3 = _cur_mgv.fetchone()[0]
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidS,))
_n3 = _cur_mgv.fetchone()[0]
check("G12f-3 unknown status (mystery) → 不 mark_failed / 不建第二 Order/checkout",
      _s3 == "pending" and _n3 == 2 and _creem_calls["create"] == _nc, f"st={st_f3} status={_s3} n={_n3}")
_RT[_ch_f[3:]] = {"status": "", "checkout_url": "https://creem.test/x"}
st_f4, d_f4 = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur_mgv.execute("select status from orders where id=%s", (_oi_f,))
_s4 = _cur_mgv.fetchone()[0]
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidS,))
_n4 = _cur_mgv.fetchone()[0]
check("G12f-4 blank/missing status → 同上 fail-closed",
      _s4 == "pending" and _n4 == 2 and _creem_calls["create"] == _nc, f"st={st_f4} status={_s4} n={_n4}")
# failed (terminal whitelist 成员) 确认仍 terminalize:
_RT[_ch_f[3:]] = {"status": "failed", "checkout_url": "https://creem.test/x"}
_nc5 = _creem_calls["create"]
st_f5, d_f5 = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
_cur_mgv.execute("select status from orders where id=%s", (_oi_f,))
_s5 = _cur_mgv.fetchone()[0]
check("G12f-5 failed (terminal) → 仍 terminalize + 新 create",
      _s5 == "failed" and _creem_calls["create"] == _nc5 + 1, f"st={st_f5} status={_s5}")
# 重新抓当前 pending order 供后续段落:
_cur_mgv.execute("select id from orders where user_id=%s and kind='plan' and status='pending' order by id desc limit 1", (uidS,))
_oi2 = _cur_mgv.fetchone()[0]
with db() as con:
    orders_repo.bind_membership(con, uidS, plan_plus, f"chfake{_oi2}", "active",
                                started_at=_ts_mgv, renews_at=None)
st, d = _resp(asyncio.run(_do_sub({"plan_id": plan_plus})))
check("G12e bind 后 paid 前 → plan_change_required (不建第二 subscription)",
      st == 409 and d.get("error") == "plan_change_required"
      and _creem_calls["create"] == 3, f"st={st} d={d}")
# unknown-profile plan → 收费前拒绝 (plan_not_ready, 零 order 零 create):
uQ, uidQ = _new_user("mgQ")
_cur_mgv.execute("update plans set creem_product_id='price_q' where code='mg_unknown_tier'")
_refresh_catalog()
_cur_mgv.execute("select id from plans where code='mg_unknown_tier'")
_pq = _cur_mgv.fetchone()[0]
_topup._require_auth = lambda request: ({"id": uidQ, "email": "t@t.co",
                                        "username": uQ}, None)
st, d = _resp(asyncio.run(_do_sub({"plan_id": _pq})))
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan'", (uidQ,))
check("G12f unknown-profile plan → plan_not_ready / 零 order / 零 checkout",
      st == 422 and d.get("error") == "plan_not_ready"
      and _cur_mgv.fetchone()[0] == 0 and _creem_calls["create"] == 3, f"st={st} d={d}")
_topup._require_auth = _orig_auth
# 夹具清场: mg_unknown_tier 回到"权益未配的缺口行" (可售性归零), 不污染后续套件的
# sellable 目录断言
_cur_mgv.execute("update plans set creem_product_id=NULL, price_cents=0"
             " where code='mg_unknown_tier'")
_refresh_catalog()

# ═══ 链13: occupied/rebind 七态 (membership_is_occupied 单一语义) ═══
_FUT2, _PAST2 = _ts_mgv + 15 * 86400, _ts_mgv - 15 * 86400
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
          _sub.membership_is_occupied(_st, _rn, _ts_mgv) is _exp)
# bind 路径吃同一语义: 七态行 × 不同 sub bind:
for _nm, _st, _rn, _exp in _states:
    uX, uidX = _new_user("mgX")
    _cur_mgv.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id, status,"
        " started_at, renews_at, updated_at) values (%s, %s, %s, %s, %s, %s, %s)",
        (uidX, plan_plus, f"sub:old_{uidX}", _st, _ts_mgv - 86400, _rn, _ts_mgv))
    try:
        with db() as con:
            orders_repo.bind_membership(con, uidX, plan_plus, f"rx{uidX}",
                                        "active", started_at=_ts_mgv, renews_at=None)
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
_cur_mgv.execute("select count(*) from orders where user_id=%s and kind='plan' and status='pending'", (uidR2,))
_np = _cur_mgv.fetchone()[0]
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
_seed_order(uidV, plan_v, "mgsubV", ch_id="chV")  # pending plan order
_cur_mgv.execute("select id, status, granted from orders where user_id=%s and kind='plan' order by id desc limit 1", (uidV,))
_ov_id, _ov_status, _ov_granted = _cur_mgv.fetchone()
# checkout.completed (bind) — payment 完成, credits 未授:
_res15 = _sub.bind_checkout({"id": "chV", "order": {"id": "ord_mgV", "status": "paid", "type": "recurring", "amount": 500, "currency": "USD", "product": "price_plus"}, "subscription": {"id": "mgsubV"}}, order_id=_ov_id)
_cur_mgv.execute("select status, granted from orders where id=%s", (_ov_id,))
_st15, _gr15 = _cur_mgv.fetchone()
check("G15 checkout.completed 后 order: status=paid / granted=FALSE (cycle credits 未授)",
      _st15 == "paid" and _gr15 is False, f"st={_st15} granted={_gr15}")
_bal15 = spendable_balance(uidV)
# subscription.paid — credits 此刻授予:
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS_mgv), _iso(PE_mgv), sub_id="mgsubV"))
_cur_mgv.execute("select count(*) from ledger where user_id=%s and kind='grant_plan'", (uidV,))
_ng15 = _cur_mgv.fetchone()[0]
check("G15b subscription.paid 后: grant_plan 恰一笔 / credits +100 / membership marker 更新",
      _ng15 == 1 and spendable_balance(uidV) == _bal15 + 100
      and _m_row(uidV)[4] == PS_mgv, f"grants={_ng15} bal={spendable_balance(uidV)}")
# package/top-up canonical finalize 语义保持 (窄 DB proof):
uW, uidW = _new_user("mgW")
_cur_mgv.execute(
    "insert into orders (user_id, kind, item_id, provider, provider_order_id, status, amount_cents, currency, credits_snapshot, created_at)"
    " values (%s, 'package', 1, 'creem', 'ch:mgW', 'pending', 500, 'USD', 100, %s) returning id",
    (uidW, _ts_mgv))
_ow = _cur_mgv.fetchone()[0]
with db() as con:
    orders_repo.complete_package_order(con, _ow, "ord_mgW", _ts_mgv)
_cur_mgv.execute("select status, granted from orders where id=%s", (_ow,))
check("G15c package complete_package_order → status=paid / granted=TRUE (语义保持)",
      _cur_mgv.fetchone() == ("paid", True))

# ═══ Source guards ═══
_sub_src_mgv = open(_os.path.join(REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
_repo_src = open(_os.path.join(REPO, "core", "repo", "orders.py"), encoding="utf-8").read()
_topup_src = open(_os.path.join(REPO, "app", "routes", "topup.py"), encoding="utf-8").read()
check("SG1 memberships PK = user_id (单行权威 SQL)",
      "PRIMARY KEY (user_id)" in open(_os.path.join(REPO, "core", "database.py"), encoding="utf-8").read()
      and "provider_subscription_id TEXT UNIQUE" in open(_os.path.join(REPO, "core", "database.py"), encoding="utf-8").read())
_po = _sub_src_mgv.split("def plan_of")[1].split("\ndef ")[0]
check("SG2 plan_of 无多行选择逻辑 (单行 read + live 判定)",
      "membership_row(" in _po and "ORDER BY" not in _po)
check("SG3 subscription.update handler 存在且走 product 映射",
      "def _ev_update" in _sub_src_mgv and "plan_by_product_id" in _sub_src_mgv)
check("SG4 事件定位走 membership_by_sub (provider 绑定权威)",
      "membership_by_sub" in _sub_src_mgv)
check("SG5 reconcile 消费 membership_row (不遍历 orders)",
      "membership_row(" in open(_os.path.join(REPO, "core", "economy", "reconcile.py"), encoding="utf-8").read())
check("SG6 /subscribe 有 plan_change_required 拦截",
      "plan_change_required" in _topup_src)
check("SG7 entitlement profile 单点 (_require_profile + 未知 raise)",
      "_require_profile" in open(_os.path.join(REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read())
_tmpl = open(_os.path.join(REPO, "app", "templates", "account", "credits.html"), encoding="utf-8").read()
check("SG8 Account badge 展示 access.membership.name (当前 tier; Free 回退)",
      "{{ m.name if m and m.member else texts.pricing_free_name }}" in _tmpl)
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
check("SG13 retrieve_checkout 存在 (provider 收敛单点)",
      "def retrieve_checkout" in open(_os.path.join(REPO, "core", "economy", "creem.py"), encoding="utf-8").read())
check("SG14 extract_product_id canonical dict 形态",
      'isinstance(product, dict)' in _sub_src_mgv
      and 'product.get("id")' in _sub_src_mgv)
_topup_g = open(_os.path.join(REPO, "app", "routes", "topup.py"), encoding="utf-8").read()
check("SG15 /subscribe sellable gate (单点) + open order 检查 + FOR UPDATE",
      "_tier.sellable" in _topup_g
      and "membership_is_effective" in _topup_g
      and "lock_user_for_checkout" in _topup_g
      and "open_plan_order" in _topup_g
      and "retrieve_checkout" in _topup_g)
_pages_g = open(_os.path.join(REPO, "app", "routes", "pages.py"), encoding="utf-8").read()
check("SG17 occupied 语义单一纯函数 (route+bind 共用, 无第二套 predicate)",
      "def membership_is_occupied(" in _sub_src_mgv
      and "membership_is_occupied" in _topup_g
      and "membership_is_occupied" in _repo_src
      and "renews_at or 0" not in _repo_src.split("def bind_membership")[1].split("\ndef ")[0])
_checkouts_url = open(_os.path.join(REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
check("SG18 retrieve query-encoded 合同 (无 path 形式)",
      '"/v1/checkouts?"' in _checkouts_url
      and "urlencode" in _checkouts_url
      and "/v1/checkouts/" not in _checkouts_url.split("def retrieve_checkout")[1].split("\ndef ")[0])
import ast as _ast
# the sync business block lives in _subscribe_checkout_impl (the async
# shell offloads it to the default executor — same code, new home)
_sub_fn = [n for n in _ast.parse(_topup_g).body
           if isinstance(n, _ast.FunctionDef) and n.name == "_subscribe_checkout_impl"][0]
_all_withs = [n for n in _ast.walk(_sub_fn) if isinstance(n, _ast.With)]
_locked_bodies = [_ast.dump(n) for n in _all_withs
                  if "lock_user_for_checkout" in _ast.dump(n)]
check("SG19 INSERT 在每个锁事务内 (AST; 两处 lock = 首建+expired-recreate)",
      len(_locked_bodies) == 2
      and all("insert_order" in b for b in _locked_bodies))
_bind_blk = _sub_src_mgv.split("def bind_checkout")[1].split("\ndef ")[0]
check("SG20 bind_checkout 只经 canonical complete_plan_checkout 域 writer",
      "complete_plan_checkout(" in _bind_blk)
_subw_blk = _repo_src.split("\ndef complete_plan_checkout")[1].split("\ndef ")[0]
_upd_i = _subw_blk.index("UPDATE orders")
_subw_sql = _subw_blk[_upd_i:_subw_blk.index("RETURNING", _upd_i)]
check("SG21 plan checkout writer: guarded paid, never granted",
      "granted" not in _subw_sql
      and "status='paid'" in _subw_sql and "paid_at=COALESCE" in _subw_sql
      and "status IN ('pending','failed')" in _subw_sql,
      _subw_sql[:120])
check("SG22 package finalize writer 保持 granted=TRUE (complete_package_order)",
      "granted=TRUE" in _repo_src.split("def complete_package_order")[1].split("\ndef ")[0])
check("SG19b HTTP 全在锁事务外 (AST: 锁 with 块内无 retrieve/create)",
      bool(_locked_bodies)
      and all("retrieve_checkout" not in b and "create_checkout" not in b
              for b in _locked_bodies))
_shop_g = _pages_g.split("def _shop_offers")[1].split("\n@router")[0]
check("SG16 /pricing sellable list consumes the same sellable authority",
      "t.sellable" in _shop_g and "plan_catalog.catalog()" in _shop_g)

# ═══ MC: /samelabs/plans member_count == canonical effective predicate ═══
# membership_counts_by_plan(con, ts) 必须与 membership_is_effective 同义:
# status live + renews_at 非空 + > ts。七格行为矩阵, raw 行直接铺。
_cur_mgv.execute(
    "insert into plans (code, name, monthly_credits, price_cents,"
    " creem_product_id, created_at, updated_at)"
    " values ('mcmat', 'MC Matrix', 1, 500, 'price_mcmat', %s, %s)"
    " on conflict (code) do update set updated_at=excluded.updated_at"
    " returning id", (_ts_mgv, _ts_mgv))
_mc_plan = _cur_mgv.fetchone()[0]
_cur_mgv.execute("delete from memberships where provider_subscription_id like 'mcmat%%'")
_NOW = int(time.time())
_FUT_MC, _PAST_MC = _NOW + 86400, _NOW - 86400
_MC_ROWS = [
    ("mcmat_a1", "active",   _FUT_MC, True),   # active + future → counted
    ("mcmat_c1", "canceled", _FUT_MC, True),   # canceled + future → counted
    ("mcmat_p1", "past_due", _FUT_MC, True),   # past_due + future → counted
    ("mcmat_a2", "active",   None,    False),  # active + NULL renews → not
    ("mcmat_a3", "active",   _PAST_MC, False), # active + past renews → not
    ("mcmat_c2", "canceled", _PAST_MC, False),
    ("mcmat_p2", "past_due", _PAST_MC, False),
    ("mcmat_e1", "expired",  _FUT_MC, False),  # expired + future → not
]
for _i, (_sid, _st, _rn, _) in enumerate(_MC_ROWS):
    _cur_mgv.execute(
        "insert into users (username, display_name, status, created_at,"
        " last_login_at) values (%s, 'MC', 'active', %s, %s) returning id",
        (f"mcmat_u{_i}_{_NOW}", _NOW, _NOW))
    _mc_uid = _cur_mgv.fetchone()[0]
    _cur_mgv.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, %s, %s)",
        (_mc_uid, _mc_plan, _sid, _st, _NOW - 7200, _rn, _NOW))
from database import db as _mc_db
with _mc_db() as _mc_con:
    _counts = orders_repo.membership_counts_by_plan(_mc_con, _NOW)
_got = int(_counts.get(_mc_plan, 0))
check("MC1 member_count == canonical effective (3 counted of 8 rows)",
      _got == 3, f"got {_got} of {len(_MC_ROWS)}")
from economy.subscription import membership_is_effective as _mie
for _sid, _st, _rn, _want in _MC_ROWS:
    _cur_mgv.execute("select user_id from memberships where provider_subscription_id=%s", (_sid,))
    _uid = _cur_mgv.fetchone()[0]
    check(f"MC2 {_sid} ({_st}, renews={_rn and 'future' if _rn and _rn > _NOW else (_rn and 'past' or 'NULL')}) repo==predicate",
          _mie(_st, _rn, _NOW) == _want)
_cur_mgv.execute("delete from memberships where provider_subscription_id like 'mcmat%%'")
_cur_mgv.execute("delete from plans where code='mcmat'")
_cur_mgv.execute(
    "select id from users where username like 'mcmat_u%%'")
purge_users(_cur_mgv, [r[0] for r in _cur_mgv.fetchall()])


# ════════════════════════════════════════════════════════════════════════
# Economy / membership closure
#
# Economy / membership closure canonical.
#
# Proves over real routes/functions (Creem isolated by module fakes, DB
# isolated, every row this suite creates is removed at the end):
#   M1  re-subscribe after lapse: subscribe → checkout.completed →
#       subscription.paid on a NEW subscription rebinds a dead Membership
#       (expired / lapsed past renews_at); credits granted exactly once;
#       a live Membership bound to another subscription still fails closed.
#   M2  a pending downgrade is cleared when subscription.update confirms
#       the current plan; the next renewal then succeeds.
#   M3  refunding ONLY the upgrade (proration) charge reverses exactly the
#       upgrade top-up and keeps the Membership; refunding the base period
#       payment still ends the current period's paid validity.
#   M4  platform income writer serializes on the platform users row:
#       concurrent settlements get distinct consecutive balance_after.
#   M5  a non-InsufficientCredits consume failure resolves the claim
#       (no charge → failed; committed charge → exact refund → failed;
#       unresolvable → running for startup recovery); retry is not stuck.
#   M6  provider call log API: explicit {at, status, duration_s,
#       cost_snapshot} shape (no failure text); drawer JS renders local
#       time + i18n status keys present in all locales.
#   M7  upgrade-credit documentation matches code (paid-only).
#   M8  an already-applied period's paid replay never regresses
#       canceled / past_due / newer period boundary.
#   M9  authenticated access runs the idempotent reconcile for a lapsed
#       Membership, throttled per user, in the background.
# ════════════════════════════════════════════════════════════════════════
section('Economy / membership closure')

import os as _os
import sys as _sys


import asyncio
import datetime
import hashlib as _hashlib
import hmac as _hmac
import json as _json
import threading
import time as _time

ts = int(_time.time())
_u = "mc" + _os.urandom(4).hex()

from database import db

_USERS = []
_PLANS_emc = []
_FIRST_PARTY_TOOLS = []
_PLATFORM_IDEMS = []

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    _USERS.append(int(row["id"]))
    return int(row["id"])

def _iso_emc(v):
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).isoformat()

def _cleanup():
    with db() as con:
        from governance.platform import ensure_platform_account
        pid = ensure_platform_account(con)
        for idem in _PLATFORM_IDEMS:
            con.execute("delete from ledger where user_id=%s and idem_key=%s",
                        (pid, idem))
        for tk in _FIRST_PARTY_TOOLS:
            con.execute("delete from pricing where tool_id=%s", (tk,))
            con.execute("delete from platform_tools where tool_key=%s", (tk,))
        for uid in _USERS:
            # uploads/ledger/orders/memberships/pending_downgrades/
            # tool_calls/sessions cascade with the users row
            con.execute("delete from pending_downgrades where user_id=%s", (uid,))
            con.execute("delete from memberships where user_id=%s", (uid,))
            purge_users(con, [uid])
        for pid_ in _PLANS_emc:
            con.execute("delete from plans where id=%s", (pid_,))
    from governance import plan_catalog
    plan_catalog.catalog(refresh=True)

try:
    # ═══ Creem isolation (module fakes; no network) ═══
    from economy import creem as _creem
    from economy import reconcile as _rec
    _creem.CREEM_WEBHOOK_SECRET = "mc-test-secret"
    _creem.creem_enabled = lambda: True
    def _fake_sub_checkout(order_id, plan, user):
        return {"ok": True, "checkout_url": f"https://creem.test/s/{order_id}",
                "checkout_id": f"ch_{_u}_{order_id}"}
    _creem.create_subscription_checkout = _fake_sub_checkout

    _TXNS = {}
    _rec.fetch_transaction = lambda txn_id: (dict(_TXNS[txn_id])
                                             if txn_id in _TXNS else None)

    def _signed(event):
        raw = _json.dumps(event).encode()
        sig = _hmac.new(b"mc-test-secret", raw, _hashlib.sha256).hexdigest()
        return raw, sig

    from fastapi.testclient import TestClient
    from app.main import app as _app
    from app.web_auth import create_browser_session, COOKIE_NAME
    client = TestClient(_app)

    def _hook(event_type, obj, evt_id=None):
        event = {"id": evt_id or f"evt_{_os.urandom(6).hex()}",
                 "eventType": event_type, "object": obj}
        raw, sig = _signed(event)
        r = client.post("/webhooks/creem", content=raw,
                        headers={"creem-signature": sig,
                                 "Content-Type": "application/json"})
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}

    def _sub_emc(sub_id, ps, pe, product, txn=None, status="active"):
        s = {"object": "subscription", "id": sub_id, "status": status,
             "current_period_start_date": _iso_emc(ps),
             "current_period_end_date": _iso_emc(pe),
             "product": {"id": product}}
        if txn:
            s["last_transaction_id"] = txn
        return s

    def _completed(order_id, sub_id, amount, product, checkout_id=None):
        return {"id": checkout_id or f"ch_{_u}_{order_id}",
                "object": "checkout",
                "request_id": f"order:{order_id}",
                "subscription": {"id": sub_id},
                "order": {"id": f"ord_{sub_id}", "status": "paid",
                          "type": "recurring", "amount": amount,
                          "currency": "USD", "product": product}}

    # ═══ plans: PLUS (100 credits) < PRO (500 credits) ═══
    from governance import plan_catalog
    def _mkplan(code, product, price, monthly, so):
        with db() as con:
            row = con.execute(
                "insert into plans (code, name, monthly_credits, price_cents,"
                " currency, creem_product_id, sort_order, active,"
                " ingress_files_24h, ingress_bytes_24h, expiry_days,"
                " download_limits, created_at, updated_at) values"
                " (%s, %s, %s, %s, 'USD', %s, %s, TRUE, 500, 524288000,"
                " ARRAY[7], ARRAY[50], %s, %s) returning id",
                (code, code.upper(), monthly, price, product, so, ts, ts)
            ).fetchone()
        _PLANS_emc.append(int(row["id"]))
        return int(row["id"])
    with db() as con:
        _so = int(con.execute(
            "select coalesce(max(sort_order), 0) + 1 s from plans").fetchone()["s"])
    P_PLUS = f"prod_{_u}_plus"
    P_PRO = f"prod_{_u}_pro"
    plus_id = _mkplan(f"plus_{_u}", P_PLUS, 999, 100, _so)
    pro_id = _mkplan(f"pro_{_u}", P_PRO, 2999, 500, _so + 1)
    plan_catalog.catalog(refresh=True)

    from economy.billing import spendable_balance, grant as _grant
    from economy.subscription import handle_event as _hev

    def _membership(uid):
        with db() as con:
            r = con.execute(
                "select m.*, p.code from memberships m join plans p"
                " on p.id=m.plan_id where m.user_id=%s", (uid,)).fetchone()
        return dict(r) if r else None

    def _pending(uid):
        with db() as con:
            r = con.execute("select * from pending_downgrades where user_id=%s",
                            (uid,)).fetchone()
        return dict(r) if r else None

    def _subscribe(cookie, plan_id):
        r = client.post("/api/topup/subscribe", json={"plan_id": plan_id},
                        cookies={COOKIE_NAME: cookie})
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}

    def _open_plan_order(uid):
        with db() as con:
            r = con.execute(
                "select * from orders where user_id=%s and kind='plan'"
                " and status='pending' order by id desc limit 1",
                (uid,)).fetchone()
        return dict(r) if r else None

    PS1, PE1 = ts - 5 * 86400, ts + 25 * 86400

    # ══════════ M1. re-subscribe after lapse ══════════
    def _first_subscription(uid, cookie, plan_id, product, price, sub_id,
                            txn, ps=PS1, pe=PE1):
        sc, d = _subscribe(cookie, plan_id)
        o = _open_plan_order(uid)
        sc2, d2 = _hook("checkout.completed",
                        _completed(o["id"], sub_id, price, product))
        sc3, d3 = _hook("subscription.paid", _sub_emc(sub_id, ps, pe, product, txn))
        return (sc, d), (sc2, d2), (sc3, d3), o

    for variant in ("expired", "lapsed"):
        ua = _mkuser(f"{_u}r{variant[:2]}")
        ck = create_browser_session(ua)
        sa, sb = f"sub_{_u}_{variant}_A", f"sub_{_u}_{variant}_B"
        (s1, d1), (s2, d2), (s3, d3), oa = _first_subscription(
            ua, ck, plus_id, P_PLUS, 999, sa, f"txn_{_u}_{variant}_A1")
        m = _membership(ua)
        check(f"M1.{variant}.0 first subscription live (plus, +100)",
              s1 == 200 and s2 == 200 and s3 == 200 and m
              and m["status"] == "active" and m["code"] == f"plus_{_u}"
              and spendable_balance(ua) == 100,
              f"{s1}/{d1} {s2}/{d2} {s3}/{d3} {m} bal={spendable_balance(ua)}")
        # a still-live membership blocks a second checkout
        sc, d = _subscribe(ck, pro_id)
        check(f"M1.{variant}.1 live membership → subscribe 409 plan_change_required",
              sc == 409 and d.get("error") == "plan_change_required", f"{sc} {d}")
        # leave a stale scheduled downgrade on the old subscription
        with db() as con:
            con.execute(
                "insert into pending_downgrades (user_id, target_plan_id,"
                " effective_at) values (%s, %s, %s)", (ua, plus_id, PE1))
        if variant == "expired":
            # 同一周期内 end 更早 (当前周期被提前终止) → 照常生效
            sc, d = _hook("subscription.expired",
                          _sub_emc(sa, PS1, ts - 60, P_PLUS, status="expired"))
            ok_lapse = sc == 200 and _membership(ua)["status"] == "expired"
        else:
            # canceled, and the paid-through date has passed (no event)
            sc, d = _hook("subscription.canceled",
                          _sub_emc(sa, PS1, PE1, P_PLUS, status="canceled"))
            with db() as con:
                con.execute("update memberships set renews_at=%s"
                            " where user_id=%s", (ts - 60, ua))
            m = _membership(ua)
            ok_lapse = sc == 200 and m["status"] == "canceled" \
                and int(m["renews_at"]) < ts
        check(f"M1.{variant}.2 old subscription {variant}", ok_lapse, f"{sc} {d}")
        bal0 = spendable_balance(ua)
        sc, d = _subscribe(ck, pro_id)
        ob = _open_plan_order(ua)
        check(f"M1.{variant}.3 dead membership → new checkout allowed",
              sc == 200 and d.get("checkout_url") and ob
              and int(ob["item_id"]) == pro_id, f"{sc} {d}")
        sc, d = _hook("checkout.completed",
                      _completed(ob["id"], sb, 2999, P_PRO))
        m = _membership(ua)
        with db() as con:
            obr = dict(con.execute("select * from orders where id=%s",
                                   (ob["id"],)).fetchone())
        check(f"M1.{variant}.4 checkout.completed rebinds the dead slot (200,"
              " order paid sub:B, membership sub:B pro, no credits yet)",
              sc == 200 and obr["status"] == "paid"
              and obr["provider_order_id"] == f"sub:{sb}"
              and m["provider_subscription_id"] == f"sub:{sb}"
              and m["code"] == f"pro_{_u}" and m["status"] == "active"
              and m["renews_at"] is None
              and spendable_balance(ua) == bal0,
              f"{sc} {d} {obr.get('status')} {m}")
        check(f"M1.{variant}.5 stale pending downgrade of the dead sub cleared",
              _pending(ua) is None, str(_pending(ua)))
        PS2, PE2 = ts - 60, ts + 30 * 86400
        sc, d = _hook("subscription.paid",
                      _sub_emc(sb, PS2, PE2, P_PRO, f"txn_{_u}_{variant}_B1"))
        m = _membership(ua)
        check(f"M1.{variant}.6 subscription.paid → active pro, +500",
              sc == 200 and m["status"] == "active" and m["code"] == f"pro_{_u}"
              and int(m["renews_at"]) == PE2
              and spendable_balance(ua) == bal0 + 500,
              f"{sc} {d} {m} bal={spendable_balance(ua)}")
        sc, d = _hook("subscription.paid",
                      _sub_emc(sb, PS2, PE2, P_PRO, f"txn_{_u}_{variant}_B1"))
        sc_c, d_c = _hook("checkout.completed",
                          _completed(ob["id"], sb, 2999, P_PRO))
        check(f"M1.{variant}.7 paid + checkout replays → credits exactly once",
              sc == 200 and sc_c == 200 and spendable_balance(ua) == bal0 + 500,
              f"{sc} {d} {sc_c} {d_c} bal={spendable_balance(ua)}")
        # replay of the OLD subscription's checkout never moves the slot back
        sc, d = _hook("checkout.completed",
                      _completed(oa["id"], sa, 999, P_PLUS))
        m = _membership(ua)
        check(f"M1.{variant}.8 old checkout replay → 500 membership_occupied,"
              " slot stays on sub:B",
              sc == 500 and d.get("error") == "membership_occupied"
              and m["provider_subscription_id"] == f"sub:{sb}",
              f"{sc} {d} {m}")

    # negative: live membership bound to a different subscription
    un = _mkuser(f"{_u}neg")
    ckn = create_browser_session(un)
    sc_, sn = f"sub_{_u}_negC", f"sub_{_u}_negD"
    _first_subscription(un, ckn, plus_id, P_PLUS, 999, sc_, f"txn_{_u}_negC1")
    from repo import orders as orders_repo
    with db() as con:
        od = orders_repo.insert_order(con, un, "plan", pro_id, "creem", 2999,
                                      "USD", ts)
    bal_n = spendable_balance(un)
    sc, d = _hook("checkout.completed", _completed(od["id"], sn, 2999, P_PRO))
    m = _membership(un)
    with db() as con:
        odr = dict(con.execute("select status, provider_order_id from orders"
                               " where id=%s", (od["id"],)).fetchone())
    check("M1.neg live membership on another sub → 500 membership_occupied,"
          " zero mutation",
          sc == 500 and d.get("error") == "membership_occupied"
          and m["provider_subscription_id"] == f"sub:{sc_}"
          and m["code"] == f"plus_{_u}" and m["status"] == "active"
          and odr["status"] == "pending" and odr["provider_order_id"] is None
          and spendable_balance(un) == bal_n,
          f"{sc} {d} {m} {odr}")
    # bound-but-unpaid (active + renews None) is occupied too
    with db() as con:
        con.execute("update memberships set renews_at=NULL where user_id=%s",
                    (un,))
    sc, d = _hook("checkout.completed", _completed(od["id"], sn, 2999, P_PRO))
    check("M1.neg2 bound-but-unpaid membership → still membership_occupied",
          sc == 500 and d.get("error") == "membership_occupied"
          and _membership(un)["provider_subscription_id"] == f"sub:{sc_}",
          f"{sc} {d}")

    # ══════════ M2. pending downgrade cleared by current-plan update ══════════
    def _seed_member(tag, plan_id, product, sub_id, txn, ps=PS1, pe=PE1):
        uid = _mkuser(f"{_u}{tag}")
        with db() as con:
            con.execute(
                "insert into orders (user_id, kind, item_id, provider,"
                " provider_order_id, status, amount_cents, currency, paid_at,"
                " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid',"
                " 999, 'USD', %s, %s)", (uid, plan_id, "sub:" + sub_id, ts, ts))
            con.execute(
                "insert into memberships (user_id, plan_id,"
                " provider_subscription_id, status, started_at, renews_at,"
                " updated_at) values (%s, %s, %s, 'active', %s, NULL, %s)",
                (uid, plan_id, "sub:" + sub_id, ts, ts))
        _hev("subscription.paid", _sub_emc(sub_id, ps, pe, product, txn))
        return uid

    s2 = f"sub_{_u}_dg"
    u2 = _seed_member("dg", pro_id, P_PRO, s2, f"txn_{_u}_dg1")
    r = _hev("subscription.update", _sub_emc(s2, PS1, PE1, P_PLUS))
    p = _pending(u2)
    check("M2.1 downgrade update → pending downgrade to plus, membership stays pro",
          r.get("deferred") == "pending_downgrade" and p
          and int(p["target_plan_id"]) == plus_id
          and _membership(u2)["code"] == f"pro_{_u}", f"{r} {p}")
    r = _hev("subscription.update", _sub_emc(s2, PS1, PE1, P_PRO))
    check("M2.2 update confirming current plan → pending downgrade cleared",
          r.get("pending_downgrade_cleared") is True and _pending(u2) is None
          and _membership(u2)["code"] == f"pro_{_u}", str(r))
    r = _hev("subscription.update", _sub_emc(s2, PS1, PE1, P_PRO))
    check("M2.3 replay is idempotent (no pending, no error)",
          r.get("updated") is True and "pending_downgrade_cleared" not in r
          and _pending(u2) is None, str(r))
    b2 = spendable_balance(u2)
    PSn, PEn = PE1, PE1 + 30 * 86400
    try:
        r = _hev("subscription.paid", _sub_emc(s2, PSn, PEn, P_PRO, f"txn_{_u}_dg2"))
        err = ""
    except KeyError as exc:
        err = str(exc)
    m = _membership(u2)
    check("M2.4 renewal on current plan succeeds (+500 pro, renews advanced)",
          not err and m["code"] == f"pro_{_u}" and int(m["renews_at"]) == PEn
          and spendable_balance(u2) == b2 + 500,
          f"{err} {m} bal={spendable_balance(u2)}")

    # ══════════ M3. partial upgrade refund ══════════
    def _upgrade(uid, sub_id, txn_up):
        _hev("subscription.update", _sub_emc(sub_id, PS1, PE1, P_PRO))
        _hev("subscription.paid", _sub_emc(sub_id, PS1, PE1, P_PRO, txn_up))

    def _refund_evt(txn_id, sub_id, ps=PS1, pe=PE1):
        _TXNS[txn_id] = {"object": "transaction", "id": txn_id,
                         "status": "refunded", "type": "invoice",
                         "subscription": sub_id,
                         "period_start": _iso_emc(ps), "period_end": _iso_emc(pe)}
        return _hook("refund.created", {"id": f"ref_{txn_id}",
                                        "object": "refund",
                                        "transaction": {"id": txn_id}})

    s3 = f"sub_{_u}_up"
    u3 = _seed_member("up", plus_id, P_PLUS, s3, f"txn_{_u}_up_base")
    _upgrade(u3, s3, f"txn_{_u}_up_charge")
    m0 = _membership(u3)
    check("M3.0 base +100 then upgrade top-up +400 (pro)",
          spendable_balance(u3) == 500 and m0["code"] == f"pro_{_u}",
          f"bal={spendable_balance(u3)} {m0}")
    sc, d = _refund_evt(f"txn_{_u}_up_charge", s3)
    m = _membership(u3)
    check("M3.1 upgrade-charge refund → exactly -400",
          sc == 200 and d.get("credits") == -400 and spendable_balance(u3) == 100,
          f"{sc} {d} bal={spendable_balance(u3)}")
    check("M3.2 upgrade-charge refund does NOT expire the membership",
          m["status"] == "active" and int(m["renews_at"]) == PE1
          and int(m["last_grant_period_start"]) == PS1, str(m))
    sc, d = _refund_evt(f"txn_{_u}_up_charge", s3)
    check("M3.3 upgrade refund replay → no second revoke, still active",
          sc == 200 and spendable_balance(u3) == 100
          and _membership(u3)["status"] == "active",
          f"{sc} {d} bal={spendable_balance(u3)}")
    sc, d = _refund_evt(f"txn_{_u}_up_base", s3)
    m = _membership(u3)
    check("M3.4 base-period refund → -100 and current period expired (unchanged)",
          sc == 200 and d.get("credits") == -100 and spendable_balance(u3) == 0
          and m["status"] == "expired" and int(m["renews_at"]) <= int(_time.time()) + 2,
          f"{sc} {d} {m}")
    s3b = f"sub_{_u}_upb"
    u3b = _seed_member("upb", plus_id, P_PLUS, s3b, f"txn_{_u}_upb_base")
    _upgrade(u3b, s3b, f"txn_{_u}_upb_charge")
    sc, d = _refund_evt(f"txn_{_u}_upb_base", s3b)
    m = _membership(u3b)
    check("M3.5 base refund with an un-refunded upgrade → -100 only, expired",
          sc == 200 and d.get("credits") == -100
          and spendable_balance(u3b) == 400 and m["status"] == "expired",
          f"{sc} {d} {m} bal={spendable_balance(u3b)}")

    # ══════════ M4. platform income serialization ══════════
    from governance.platform import record_platform_income, ensure_platform_account
    _pid = ensure_platform_account()
    _ids = [f"{_u}m4a", f"{_u}m4b"]
    _amt = {_ids[0]: 7, _ids[1]: 11}
    for cid in _ids:
        _PLATFORM_IDEMS.append(f"platform_revenue:{cid}")
    _barrier_emc = threading.Barrier(2)
    _m4err = []
    def _settle(cid):
        try:
            _barrier_emc.wait(timeout=10)
            with db() as con:
                record_platform_income(con, cid, "platform_revenue", _amt[cid],
                                       "m4 concurrency", ts)
                _time.sleep(0.5)   # hold the settlement tx open
        except Exception as exc:
            _m4err.append(repr(exc))
    _th = [threading.Thread(target=_settle, args=(c,)) for c in _ids]
    for t in _th:
        t.start()
    for t in _th:
        t.join(timeout=30)
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "select id, ref_id, delta, balance_after from ledger"
            " where user_id=%s and idem_key = any(%s) order by id",
            (_pid, [f"platform_revenue:{c}" for c in _ids])).fetchall()]
        total = int(con.execute(
            "select coalesce(sum(delta),0) s from ledger where user_id=%s"
            " and kind in ('platform_commission','platform_revenue')",
            (_pid,)).fetchone()["s"])
    ok4 = (not _m4err and len(rows) == 2
           and rows[1]["balance_after"] == rows[0]["balance_after"] + rows[1]["delta"]
           and rows[1]["balance_after"] == total)
    check("M4 two parallel settlements → distinct consecutive balance_after",
          ok4, f"{_m4err} {rows} total={total}")

    # ══════════ M5. consume failure resolves the claim ══════════
    from execution import gate as _gate
    from execution.invocation import ToolInvocation, SourceFile
    from execution.results import StagedItems, StagedFileItem
    from repo.ledger import by_idem_key
    tool5 = f"mctool_{_u}"
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled,"
            " created_at, updated_at, accept_json, format_family)"
            " values (%s, NULL, %s, 'local', %s,"
            " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
            (tool5, tool5, _json.dumps({"en": "MC"}), ts, ts))
        con.execute("insert into pricing (tool_id, cost, enabled, updated_at)"
                    " values (%s, 25, TRUE, %s)", (tool5, ts))
    _FIRST_PARTY_TOOLS.append(tool5)
    u5_emc = _mkuser(f"{_u}c5")
    _grant(u5_emc, 1000, "manual_topup", note="mc")

    def _inv(ref):
        return ToolInvocation(tool_id=tool5, files=[SourceFile("p", "t")],
                              params={}, delivery={"expiry_days": 7,
                                                   "allowed_downloads": 5},
                              client_ref=ref)
    _stage_n = [0]
    async def _stage_ok():
        _stage_n[0] += 1
        return StagedItems(
            order=["file"], inline=[],
            files=[StagedFileItem(
                gokapi_id=f"gk_{_u}_{_stage_n[0]}", filename="o.bin",
                content_type="application/octet-stream", size=10,
                expiry_days=7, allowed_downloads=5,
                expire_at=ts + 86400, url_hotlink="")],
            ip="127.0.0.1")
    def _run(ref):
        return asyncio.run(_gate.run(
            {"id": u5_emc}, {"id": tool5, "tool_key": tool5,
                         "owner_id": None}, _inv(ref), _stage_ok))
    def _calls(ref):
        with db() as con:
            return [dict(r) for r in con.execute(
                "select id, status, error from tool_calls where user_id=%s"
                " and client_ref=%s order by id", (u5_emc, ref)).fetchall()]

    _real_consume = _gate.consume
    # a) consume fails WITHOUT committing
    def _consume_boom(*a, **kw):
        raise RuntimeError("db connection lost")
    _gate.consume = _consume_boom
    try:
        ref_a = f"m5a:{_u}"
        b5 = spendable_balance(u5_emc)
        res, err, st = _run(ref_a)
    finally:
        _gate.consume = _real_consume
    ca = _calls(ref_a)
    with db() as con:
        chg_a = by_idem_key(con, u5_emc, f"call:{ca[0]['id']}") if ca else None
    check("M5a consume error (not committed) → 500 err_metering_failed, call"
          " failed, zero charge",
          st == 500 and err.get("error") == "err_metering_failed"
          and len(ca) == 1 and ca[0]["status"] == "failed"
          and chg_a is None and spendable_balance(u5_emc) == b5,
          f"{st} {err} {ca} bal={spendable_balance(u5_emc)}")
    res, err, st = _run(ref_a)
    ca = _calls(ref_a)
    check("M5a2 retry with the same client_ref is not stuck (runs, charged once)",
          res and res.get("ok") and err is None
          and [c["status"] for c in ca] == ["failed", "ok"]
          and spendable_balance(u5_emc) == b5 - 25,
          f"{res} {err} {ca} bal={spendable_balance(u5_emc)}")
    # b) consume COMMITS, then the call raises (ACK loss)
    def _consume_commit_then_boom(*a, **kw):
        _real_consume(*a, **kw)
        raise RuntimeError("ack lost")
    _gate.consume = _consume_commit_then_boom
    try:
        ref_b = f"m5b:{_u}"
        b5b = spendable_balance(u5_emc)
        res, err, st = _run(ref_b)
    finally:
        _gate.consume = _real_consume
    cb = _calls(ref_b)
    with db() as con:
        chg = by_idem_key(con, u5_emc, f"call:{cb[0]['id']}") if cb else None
        rf = by_idem_key(con, u5_emc, f"refund:ledger:{chg['id']}") if chg else None
    check("M5b committed charge + exception → exact refund, call failed",
          st == 500 and err.get("error") == "err_metering_failed"
          and len(cb) == 1 and cb[0]["status"] == "failed"
          and chg is not None and rf is not None
          and int(rf["delta"]) == -int(chg["delta"])
          and spendable_balance(u5_emc) == b5b,
          f"{st} {err} {cb} chg={chg and chg['delta']} rf={rf and rf['delta']}")
    # c) resolution cannot finalize → stays running → startup recovery
    _real_ffail = _gate.finalize_call_failed
    def _ffail_boom(*a, **kw):
        raise RuntimeError("db down")
    _gate.consume = _consume_boom
    _gate.finalize_call_failed = _ffail_boom
    try:
        ref_c = f"m5c:{_u}"
        res, err, st = _run(ref_c)
    finally:
        _gate.consume = _real_consume
        _gate.finalize_call_failed = _real_ffail
    cc = _calls(ref_c)
    rec = _gate.recover_abandoned_calls()
    cc2 = _calls(ref_c)
    check("M5c unresolvable → stays running (500), startup recovery settles it",
          st == 500 and cc and cc[0]["status"] == "running"
          and cc2[0]["status"] == "failed", f"{st} {cc} {cc2} {rec}")

    # ══════════ M6. provider call log API + drawer ══════════
    owner = _mkuser(f"{_u}ow")
    caller = _mkuser(f"{_u}ca")
    with db() as con:
        oname = con.execute("select username from users where id=%s",
                            (owner,)).fetchone()["username"]
    tid6 = f"t6{_u}"
    tkey6 = f"{oname}_{tid6}"
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json,"
            " visibility, enabled, endpoint_json, created_at, updated_at, accept_json, format_family)"
            " values (%s, %s, %s, 'http', %s,"
            " 'unlisted', TRUE, %s, %s, %s, '[\".png\"]', 'image')",
            (tkey6, owner, tid6, _json.dumps({"en": "T6"}),
             _json.dumps({"url": "https://example.com/x", "timeout_s": 10}),
             ts, ts))
        h = "a" * 64
        con.execute(
            "insert into tool_calls (client_ref, user_id, tool_id, status,"
            " cost_snapshot, error, started_at, finished_at,"
            " commission_rate_snapshot, invocation_hash) values"
            " ('r1', %s, %s, 'failed', 25, 'balance:12345', %s, %s, 20, %s),"
            " ('r2', %s, %s, 'running', 25, NULL, %s, NULL, 20, %s)",
            (caller, tkey6, ts - 100, ts - 97, h, caller, tkey6, ts - 50, h))
    ck6 = create_browser_session(owner)
    r = client.get(f"/api/usertools/{tkey6}/calls", cookies={COOKIE_NAME: ck6})
    d = r.json()
    calls6 = d.get("calls") or []
    shape_ok = all(set(c) == {"at", "status", "duration_s", "cost_snapshot"}
                   for c in calls6)
    by_st = {c["status"]: c for c in calls6}
    check("M6.1 call log shape {at,status,duration_s,cost_snapshot}, no failure text",
          r.status_code == 200 and len(calls6) == 2 and shape_ok
          and "balance:12345" not in r.text, f"{r.status_code} {d}")
    check("M6.2 at = started_at epoch seconds; duration_s = finished-started;"
          " running → None",
          by_st.get("failed", {}).get("at") == ts - 100
          and by_st["failed"]["duration_s"] == 3
          and by_st.get("running", {}).get("duration_s") is None
          and isinstance(by_st["running"]["at"], int), str(calls6))
    _js = open(_os.path.join(REPO, "static", "pages", "tools.js"),
               encoding="utf-8").read()
    check("M6.3 drawer renders at in local time + i18n status (no created_at/ISO)",
          "created_at" not in _js and "toISOString" not in _js
          and "toLocaleString" in _js and "'mytools_call_st_' + c.status" in _js)
    from i18n import SERVER_I18N
    _keys = [f"mytools_call_st_{s}" for s in ("ok", "failed", "insufficient",
                                              "running")]
    _i18n_js = open(_os.path.join(REPO, "static", "i18n.js"),
                    encoding="utf-8").read()
    check("M6.4 status keys present in all 10 locales (source + generated)",
          len(SERVER_I18N) == 10
          and all(all(SERVER_I18N[l].get(k) for k in _keys) for l in SERVER_I18N)
          and all(k in _i18n_js for k in _keys))

    # ══════════ M7. docs truth ══════════
    _topup_emc = open(_os.path.join(REPO, "app", "routes", "topup.py"),
                  encoding="utf-8").read()
    check("M7 topup comments: upgrade credits are paid-only (no update-event grant claim)",
          "由 update 事件补授" not in _topup_emc and "由 update 事件补差" not in _topup_emc)

    # ══════════ M8. paid replay never regresses lifecycle ══════════
    for st_evt, st_name in (("subscription.canceled", "canceled"),
                            ("subscription.past_due", "past_due")):
        s8 = f"sub_{_u}_{st_name}"
        u8 = _seed_member(st_name[:4] + "8", plus_id, P_PLUS, s8,
                          f"txn_{_u}_{st_name}1")
        _hev(st_evt, _sub_emc(s8, PS1, PE1, P_PLUS, status=st_name))
        b8 = spendable_balance(u8)
        r8 = _hev("subscription.paid", _sub_emc(s8, PS1, PE1, P_PLUS,
                                            f"txn_{_u}_{st_name}1"))
        m = _membership(u8)
        check(f"M8.{st_name} same-period paid replay keeps {st_name}, zero grant",
              m["status"] == st_name and int(m["renews_at"]) == PE1
              and spendable_balance(u8) == b8 and r8.get("credits") == 0,
              f"{r8} {m}")
    s8o = f"sub_{_u}_ooo"
    u8o = _seed_member("ooo8", plus_id, P_PLUS, s8o, f"txn_{_u}_ooo1")
    PSb, PEb = PE1, PE1 + 30 * 86400
    _hev("subscription.paid", _sub_emc(s8o, PSb, PEb, P_PLUS, f"txn_{_u}_ooo2"))
    _hev("subscription.canceled", _sub_emc(s8o, PSb, PEb, P_PLUS, status="canceled"))
    b8o = spendable_balance(u8o)
    _hev("subscription.paid", _sub_emc(s8o, PS1, PE1, P_PLUS, f"txn_{_u}_ooo1"))
    m = _membership(u8o)
    check("M8.ooo late older-period paid → newer boundary + canceled kept, zero grant",
          m["status"] == "canceled" and int(m["renews_at"]) == PEb
          and int(m["last_grant_period_start"]) == PSb
          and spendable_balance(u8o) == b8o, str(m))
    s8n = f"sub_{_u}_new"
    u8n = _seed_member("new8", plus_id, P_PLUS, s8n, f"txn_{_u}_new1")
    _hev("subscription.past_due", _sub_emc(s8n, PS1, PE1, P_PLUS, status="past_due"))
    _hev("subscription.paid", _sub_emc(s8n, PSb, PEb, P_PLUS, f"txn_{_u}_new2"))
    m = _membership(u8n)
    check("M8.new a NEW period's paid still recovers past_due → active",
          m["status"] == "active" and int(m["renews_at"]) == PEb, str(m))

    # M8.race: an expired commit landing BETWEEN apply_paid_period's
    # snapshot read and its final write must win — the monotonic branch
    # is computed from the FOR UPDATE re-read inside the write
    # transaction, so the expired state survives (renews_at and
    # period_start never regress to the stale snapshot's values).
    s8x = f"sub_{_u}_snaprace"
    u8x = _seed_member("snaprace8", plus_id, P_PLUS, s8x, f"txn_{_u}_snaprace1")
    b8x = spendable_balance(u8x)
    from database import db as _db
    from repo import orders as _orders_repo
    _real_pbp = _orders_repo.period_base_payment
    _race = {"fired": False}
    def _pbp_then_expire(con, sub_id, period_start):
        row = _real_pbp(con, sub_id, period_start)
        if not _race["fired"]:
            _race["fired"] = True
            # 独立事务提交 expired —— 恰落在快照读之后、加锁重读之前
            # (快照读无行锁, 此处提交畅通; 复刻 expired 事件的行效果)
            with _db() as excon:
                excon.execute(
                    "update memberships set status='expired', renews_at=%s,"
                    " updated_at=%s where provider_subscription_id=%s",
                    (ts, ts, "sub:" + s8x))
        return row
    _orders_repo.period_base_payment = _pbp_then_expire
    try:
        r8x = _hev("subscription.paid",
                   _sub_emc(s8x, PS1, PE1, P_PLUS, f"txn_{_u}_snaprace1"))
    finally:
        _orders_repo.period_base_payment = _real_pbp
    m = _membership(u8x)
    check("M8.race expired committed mid-apply wins (status expired, no regress)",
          _race["fired"] and m["status"] == "expired"
          and spendable_balance(u8x) == b8x
          and int(m["last_grant_period_start"]) == PS1,
          f"{r8x} {m}")

    # ════════ M8b. 旧 paid 重放不得覆盖交错期间接受的新套餐/取消标记 ════════
    # 确定性交错 (A01): 旧 paid 的快照读完成后 (period_base_payment 钩子),
    # 真实 subscription.update (plus→pro) 与 scheduled_cancel 在独立事务提交,
    # 再继续旧 paid 的最终持锁写 — 新套餐与取消标记必须保留, 重放零新 grant。
    s8b = f"sub_{_u}_snaprace2"
    u8b = _seed_member("snap2", plus_id, P_PLUS, s8b, f"txn_{_u}_sr2_1")
    _real_pbp8b = _orders_repo.period_base_payment
    _race8b = {"fired": False}
    def _pbp8b(con, sub_id, period_start):
        row = _real_pbp8b(con, sub_id, period_start)
        if not _race8b["fired"]:
            _race8b["fired"] = True
            with _db() as excon:
                _hev("subscription.update", _sub_emc(s8b, PS1, PE1, P_PRO))
                _hev("subscription.scheduled_cancel",
                     _sub_emc(s8b, PS1, PE1, P_PLUS))
        return row
    _orders_repo.period_base_payment = _pbp8b
    _b8b = spendable_balance(u8b)
    try:
        # 同周期旧 paid 重放 (同 txn → replay 分支, 零新 grant)
        _hev("subscription.paid",
             _sub_emc(s8b, PS1, PE1, P_PLUS, txn=f"txn_{_u}_sr2_1"))
    finally:
        _orders_repo.period_base_payment = _real_pbp8b
    m8b = _membership(u8b)
    check("M8b.1 旧 paid 重放交错 update(pro)+scheduled_cancel → 新套餐保留",
          _race8b["fired"] and m8b["code"] == f"pro_{_u}",
          f"race={_race8b['fired']} {m8b}")
    check("M8b.2 取消标记不被旧 paid 清空",
          m8b["cancel_scheduled_at"] is not None, str(m8b))
    check("M8b.3 重放零新 grant / 状态仍 active / 周期不回退",
          spendable_balance(u8b) == _b8b and m8b["status"] == "active"
          and int(m8b["last_grant_period_start"]) == PS1, str(m8b))

    # ══════════ M9. access-triggered reconcile (throttled) ══════════
    _fetched = []
    _SUBS = {}
    def _fake_fetch_sub(sub_id):
        _fetched.append(sub_id)
        s = _SUBS.get(sub_id)
        return dict(s) if s else None
    _rec.fetch_subscription = _fake_fetch_sub
    s9 = f"sub_{_u}_acc"
    u9 = _seed_member("acc9", plus_id, P_PLUS, s9, f"txn_{_u}_acc1",
                      ps=ts - 40 * 86400, pe=ts - 10 * 86400)
    with db() as con:
        con.execute("update memberships set status='active' where user_id=%s",
                    (u9,))
    PS9, PE9 = ts - 10 * 86400, ts + 20 * 86400
    _SUBS[s9] = _sub_emc(s9, PS9, PE9, P_PLUS, f"txn_{_u}_acc2")
    _TXNS[f"txn_{_u}_acc2"] = {"object": "transaction", "id": f"txn_{_u}_acc2",
                               "status": "paid", "type": "invoice",
                               "subscription": s9, "period_start": _iso_emc(PS9),
                               "period_end": _iso_emc(PE9)}
    b9 = spendable_balance(u9)
    ck9 = create_browser_session(u9)
    def _wait(pred, sec=10):
        end = _time.time() + sec
        while _time.time() < end:
            if pred():
                return True
            _time.sleep(0.1)
        return pred()
    client.get("/healthz", cookies={COOKIE_NAME: ck9})
    ok9 = _wait(lambda: (_membership(u9) or {}).get("renews_at") == PE9
                and spendable_balance(u9) == b9 + 100)
    m = _membership(u9)
    check("M9.1 session access with lapsed renews_at → reconcile heals the"
          " lost renewal (+100, renews advanced)",
          ok9 and _fetched.count(s9) == 1 and m["status"] == "active",
          f"{m} fetched={_fetched} bal={spendable_balance(u9)}")
    with db() as con:
        con.execute("update memberships set renews_at=%s where user_id=%s",
                    (ts - 5, u9))
    client.get("/healthz", cookies={COOKIE_NAME: ck9})
    _time.sleep(1.0)
    check("M9.2 second access within the throttle window → no second attempt",
          _fetched.count(s9) == 1, str(_fetched))
    s9b = f"sub_{_u}_accb"
    u9b = _seed_member("acb9", plus_id, P_PLUS, s9b, f"txn_{_u}_accb1")
    client.get("/healthz", cookies={COOKIE_NAME: create_browser_session(u9b)})
    _time.sleep(1.0)
    check("M9.3 live (not lapsed) membership → no provider fetch",
          s9b not in _fetched, str(_fetched))

    # ════════ M9b. OAuth (fsot_) 与 fsk_ 访问同样触发补偿 (A03) ════════
    # 中间件 auth_via ∈ (session, token, oauth): 触发面 + 无效令牌不触发
    # + 端到端愈合 (provider 已续费但 webhook 丢失 → 相同周期/积分事实)。
    import base64 as _b64_9b, hashlib as _hashlib_9b, secrets as _secrets_9b
    from identity import oauth_provider as _oa
    from identity.apikeys import create_key
    from identity.oauth_provider import MCP_RESOURCE as _RES9b
    _ver9b = _secrets_9b.token_urlsafe(48)
    _ch9b = _b64_9b.urlsafe_b64encode(
        _hashlib_9b.sha256(_ver9b.encode()).digest()).rstrip(b"=").decode()
    _CID9B = f"https://plugin-{_u}-acc.example/oauth.json"
    _RED9B = f"https://plugin-{_u}-acc.example/cb"

    def _mint_fsot(uid_9b):
        _c = _oa.create_authorization_code(
            uid_9b, _CID9B, _RED9B, _ch9b, "tools:call", _RES9b)
        _t = _oa.exchange_authorization_code(
            _c, _CID9B, _RED9B, _ver9b, _RES9b)
        return _t["access_token"]

    _PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    _orig_roa = _rec.reconcile_on_access
    _trig9b = []
    _rec.reconcile_on_access = lambda uid: _trig9b.append(int(uid)) or True
    u9t = _mkuser(f"{_u}acctrig")
    _fsk9t = create_key(u9t, "m9b-trig")["key"]
    _fsot9t = _mint_fsot(u9t)
    try:
        client.post("/mcp", json=_PING,
                    headers={"Authorization": "Bearer fsot_invalid_9b"})
        check("M9b.0 无效 fsot_ → 认证失败且不触发补偿", _trig9b == [],
              str(_trig9b))
        client.post("/mcp", json=_PING,
                    headers={"Authorization": f"Bearer {_fsot9t}"})
        check("M9b.1 oauth (fsot_) 访问触发 reconcile",
              _trig9b == [u9t], str(_trig9b))
        client.post("/mcp", json=_PING,
                    headers={"Authorization": f"Bearer {_fsk9t}"})
        check("M9b.2 fsk_ (token) 访问触发 reconcile",
              _trig9b == [u9t, u9t], str(_trig9b))
        client.get("/healthz")
        check("M9b.3 匿名访问不触发", len(_trig9b) == 2, str(_trig9b))
    finally:
        _rec.reconcile_on_access = _orig_roa

    # 端到端: OAuth 用户续费丢失 → 一次访问后台愈合 (+100, renews 推进)
    def _lapsed_healed(tag, sub_id, txn_new, ps_new, pe_new):
        uid_ = _seed_member(tag, plus_id, P_PLUS, sub_id, txn_new + "a",
                            ps=ts - 40 * 86400, pe=ts - 10 * 86400)
        with db() as con:
            con.execute("update memberships set status='active' where"
                        " user_id=%s", (uid_,))
        _SUBS[sub_id] = _sub_emc(sub_id, ps_new, pe_new, P_PLUS, txn_new)
        _TXNS[txn_new] = {"object": "transaction", "id": txn_new,
                          "status": "paid", "type": "invoice",
                          "subscription": sub_id,
                          "period_start": _iso_emc(ps_new),
                          "period_end": _iso_emc(pe_new)}
        return uid_

    s9c = f"sub_{_u}_acco"
    _txn9c = f"txn_{_u}_acco2"
    PS9c, PE9c = ts - 10 * 86400, ts + 20 * 86400
    u9c = _lapsed_healed("acco", s9c, _txn9c, PS9c, PE9c)
    _b9c = spendable_balance(u9c)
    client.post("/mcp", json=_PING,
                headers={"Authorization": f"Bearer {_mint_fsot(u9c)}"})
    _ok9c = _wait(lambda: (_membership(u9c) or {}).get("renews_at") == PE9c
                  and spendable_balance(u9c) == _b9c + 100)
    m9c = _membership(u9c)
    check("M9b.4 oauth 访问 + 丢失的续费 paid → 后台补偿愈合 (+100 / renews 推进)",
          _ok9c and m9c["status"] == "active"
          and _fetched.count(s9c) == 1,
          f"{m9c} fetched={_fetched.count(s9c)} bal={spendable_balance(u9c)}")
    # 节流窗口内重复访问不二次拉取/赠送
    client.post("/mcp", json=_PING,
                headers={"Authorization": f"Bearer {_mint_fsot(u9c)}"})
    _time.sleep(1.0)
    check("M9b.5 oauth 窗口内重复访问 → 无第二次 provider 拉取",
          _fetched.count(s9c) == 1, str(_fetched.count(s9c)))

    s9d = f"sub_{_u}_acck"
    _txn9d = f"txn_{_u}_acck2"
    u9d = _lapsed_healed("acck", s9d, _txn9d, PS9c, PE9c)
    _b9d = spendable_balance(u9d)
    client.post("/mcp", json=_PING,
                headers={"Authorization":
                         f"Bearer {create_key(u9d, 'm9b-e2e')['key']}"})
    _ok9d = _wait(lambda: (_membership(u9d) or {}).get("renews_at") == PE9c
                  and spendable_balance(u9d) == _b9d + 100)
    m9d = _membership(u9d)
    check("M9b.6 fsk_ 访问同样愈合相同周期/积分事实 (+100 / renews 推进)",
          _ok9d and m9d["status"] == "active"
          and _fetched.count(s9d) == 1
          and int(m9d["last_grant_period_start"]) == PS9c,
          f"{m9d} bal={spendable_balance(u9d)}")

    # ══════════ M10. 迟到的旧周期 lifecycle 事件不回退已付费新周期 ══════════
    for st_evt, st_name in (("subscription.canceled", "canceled"),
                            ("subscription.expired", "expired"),
                            ("subscription.scheduled_cancel",
                             "scheduled_cancel"),
                            ("subscription.past_due", "past_due")):
        s10 = f"sub_{_u}_st{st_name[:3]}"
        u10 = _seed_member("st" + st_name[:3], plus_id, P_PLUS, s10,
                           f"txn_{_u}_st{st_name}1")
        _hev("subscription.paid", _sub_emc(s10, PE1, PE1 + 30 * 86400, P_PLUS,
                                       f"txn_{_u}_st{st_name}2"))
        # 迟到的旧周期事件 (payload period end = 旧周期 PE1)
        r10 = _hev(st_evt, _sub_emc(s10, PS1, PE1, P_PLUS))
        m = _membership(u10)
        check(f"M10 stale {st_name} (old period) → ignored, paid period kept",
              m["status"] == "active"
              and int(m["renews_at"]) == PE1 + 30 * 86400,
              f"{r10} {m}")

    # 当前周期事件照常生效
    s10c = f"sub_{_u}_curc"
    u10c = _seed_member("curc", plus_id, P_PLUS, s10c, f"txn_{_u}_curc1")
    _hev("subscription.canceled", _sub_emc(s10c, PS1, PE1, P_PLUS,
                                       status="canceled"))
    m = _membership(u10c)
    check("M10b current-period canceled → rights kept till period end",
          m["status"] == "canceled" and int(m["renews_at"]) == PE1, str(m))
    s10e = f"sub_{_u}_cure"
    u10e = _seed_member("cure", plus_id, P_PLUS, s10e, f"txn_{_u}_cure1")
    _hev("subscription.expired", _sub_emc(s10e, PS1, PE1, P_PLUS,
                                      status="expired"))
    m = _membership(u10e)
    check("M10c current-period expired → status expired",
          m["status"] == "expired", str(m))

    # paid 与迟到的旧 canceled 并发 → 最终仍为新周期
    import threading as _th10
    s10r = f"sub_{_u}_race"
    u10r = _seed_member("rac", plus_id, P_PLUS, s10r, f"txn_{_u}_race1")
    def _fire_paid():
        _hev("subscription.paid", _sub_emc(s10r, PE1, PE1 + 30 * 86400, P_PLUS,
                                       f"txn_{_u}_race2"))
    def _fire_old_cancel():
        _hev("subscription.canceled", _sub_emc(s10r, PS1, PE1, P_PLUS))
    t1 = _th10.Thread(target=_fire_paid)
    t2 = _th10.Thread(target=_fire_old_cancel)
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    m = _membership(u10r)
    check("M10d concurrent new paid + stale canceled → new period wins",
          int(m["renews_at"]) == PE1 + 30 * 86400, str(m))

    # updated_at = 写入时刻 (不再是 started_at)
    s10u = f"sub_{_u}_upd"
    u10u = _seed_member("upd", plus_id, P_PLUS, s10u, f"txn_{_u}_upd1")
    with db() as con:
        con.execute("update memberships set started_at=%s, updated_at=%s"
                    " where user_id=%s", (ts - 3600, ts - 3600, u10u))
    _t10a = int(_time.time())
    _hev("subscription.past_due", _sub_emc(s10u, PS1, PE1, P_PLUS,
                                       status="past_due"))
    m = _membership(u10u)
    check("M10e updated_at = write time (not started_at)",
          _t10a - 2 <= int(m["updated_at"]) <= _t10a + 5
          and int(m["updated_at"]) != ts - 3600, str(m))
    # M10f: 同一周期、payload end 早于 renews_at 的 expired → 照常立即生效
    s10f = f"sub_{_u}_curf"
    u10f = _seed_member("curf", plus_id, P_PLUS, s10f, f"txn_{_u}_curf1")
    _hev("subscription.paid", _sub_emc(s10f, PE1, PE1 + 30 * 86400, P_PLUS,
                                   f"txn_{_u}_curf2"))
    r10f = _hev("subscription.expired",
                _sub_emc(s10f, PE1, PE1 - 5 * 86400, P_PLUS, status="expired"))
    m = _membership(u10f)
    check("M10f same-period early-end expired → applies immediately",
          m["status"] == "expired"
          and int(m["renews_at"]) == PE1 - 5 * 86400
          and str(r10f.get("status")) == "expired", f"{r10f} {m}")

    # M10g: plans 行锁 (后台 FOR UPDATE) 不阻塞 lifecycle 事件
    # (membership_by_sub 只锁 memberships 行)
    s10g = f"sub_{_u}_ga"
    u10g = _seed_member("g10a", plus_id, P_PLUS, s10g, f"txn_{_u}_ga1")
    s10h = f"sub_{_u}_gb"
    u10h = _seed_member("g10b", pro_id, P_PRO, s10h, f"txn_{_u}_gb1")
    _lock_err = []

    def _hold_plans_lock():
        try:
            with db() as con:
                con.execute("SELECT id FROM plans WHERE id = %s FOR UPDATE",
                            (plus_id,))
                _time.sleep(3)
        except Exception as exc:  # rollback on close is expected
            if "rollback" not in str(exc).lower():
                _lock_err.append(str(exc))

    _t_lock = _th10.Thread(target=_hold_plans_lock)
    _t_lock.start()
    _time.sleep(0.3)  # lock acquired
    _tg0 = _time.time()
    _hev("subscription.canceled", _sub_emc(s10g, PS1, PE1, P_PLUS,
                                       status="canceled"))
    _hev("subscription.canceled", _sub_emc(s10h, PS1, PE1, P_PRO,
                                       status="canceled"))
    _tg_dt = _time.time() - _tg0
    _t_lock.join(10)
    check("M10g plans row lock never blocks lifecycle events (<1s while held 3s)",
          _tg_dt < 1.0 and not _lock_err
          and _membership(u10g)["status"] == "canceled"
          and _membership(u10h)["status"] == "canceled",
          f"{_tg_dt:.2f}s err={_lock_err}")


finally:
    _cleanup()


with db() as con:
    left = con.execute(
        "select (select count(*) from users where username like %s) u,"
        " (select count(*) from plans where code like %s) p,"
        " (select count(*) from platform_tools where tool_key like %s) t",
        (f"{_u}%", f"%{_u}%", f"%{_u}%")).fetchone()
check("CLEANUP all suite rows removed",
      int(left["u"]) == 0 and int(left["p"]) == 0 and int(left["t"]) == 0,
      str(dict(left)))


finish()
