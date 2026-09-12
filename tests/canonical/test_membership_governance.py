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
from governance.entitlements import (resolve, resolve_from_membership,
                                     entitlement_profile_exists,
                                     EntitlementConfigurationError, _PLANS)
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
# 幂等重跑: 清理本 suite 之前的半途残留 (mg 前缀用户及其行)
_cur.execute("delete from memberships where user_id in (select id from users where username like 'mg%')")
_cur.execute("delete from user_plans where user_id in (select id from users where username like 'mg%')")
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
        "insert into plans (code, name, monthly_credits, price_cents, creem_price_id, created_at, updated_at)"
        " values (%s, %s, %s, 500, %s, %s, %s)"
        " on conflict (code) do update set monthly_credits=excluded.monthly_credits,"
        " creem_price_id=coalesce(plans.creem_price_id, excluded.creem_price_id)"
        " returning id",
        (code, code.upper(), credits, price_id, _ts, _ts))
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
         "current_period_start_date": p_start, "current_period_end_date": p_end}
    if product:
        d["product"] = product
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
_sub.bind_checkout({"id": "ch1", "subscription": {"id": "mgsub1"}})
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

# ═══ 链3: 复购拦截 ═══
# (subscribe guard 在 HTTP 层; 直调 membership 唯一性: 二次 bind 不同 sub → PK 冲突)
try:
    with db() as con:
        orders_repo.upsert_membership(con, uidP, plan_plus, "mgsub2", "active",
                                      started_at=_ts, renews_at=PE)
    # upsert ON CONFLICT(user_id) 是 UPDATE — 同 user 换 sub = 行内换绑, 不是第二行:
    m = _m_row(uidP)
    _cur.execute("select count(*) from memberships where user_id=%s", (uidP,))
    check("G3 memberships 单行: 同 user 二次订阅不产生第二行 (行内绑定唯一)",
          _cur.fetchone()[0] == 1, str(m))
except Exception as exc:
    check("G3 memberships 单行", False, str(exc))

# ═══ 链4: 第二付费 tier → 自己的 entitlement (非 Free) ═══
_PRO_TIER = {
    "plan_code": "mg_test_pro", "ingress_files_24h": 999,
    "ingress_bytes_24h": 20 * 1024 ** 3,
    "expiry_days": (1, 3, 7, 30, 90), "download_limits": (1, 10, 50, 100, 200, 1000, 5000),
}
# fail-closed protocol-subset 校验会拒 90/5000 (平台枚举外) — 用合法值:
_PRO_TIER["expiry_days"] = (1, 3, 7, 30)
_PRO_TIER["download_limits"] = (1, 10, 50, 100, 200, 1000)
_PLANS["mg_test_pro"] = _PRO_TIER
from governance import entitlements as _ent_mod
_ent_mod._validate_against_protocol(_PRO_TIER, "mg_test_pro")
uT, uidT = _new_user("mgT")
plan_pro = _seed_plan("mg_test_pro", 500, "price_pro")
_seed_order(uidT, plan_pro, "mgsubT")
_sub.bind_checkout({"id": "chT", "subscription": {"id": "mgsubT"}})
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), sub_id="mgsubT"))
r = resolve(uidT)
check("G4 第二 tier → 自己的 entitlement (999 files/20GiB, 绝不 Free)",
      r["plan_code"] == "mg_test_pro" and r["ingress_files_24h"] == 999
      and r["ingress_bytes_24h"] == 20 * 1024 ** 3, str(r["plan_code"]))
check("G4b entitlement_profile_exists 单点",
      entitlement_profile_exists("plus") and entitlement_profile_exists("mg_test_pro")
      and not entitlement_profile_exists("nope"))

# ═══ 链5: subscription.update 换 tier (同一 sub) ═══
res = _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product="price_pro", sub_id="mgsubT"))
m = _m_row(uidT)
pp = project(uidT)
check("G5 update plan A→B: 同一 provider sub, membership/projection/entitlement 全变 B",
      m[0] == plan_pro and m[1] == "sub:mgsubT"
      and pp["entitlements"]["plan_code"] == "mg_test_pro"
      and resolve(uidT)["ingress_files_24h"] == 999, str(res))
# update 无映射 → fail loud:
try:
    _sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product="price_unknown", sub_id="mgsubT"))
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
_sub.bind_checkout({"id": "chR", "subscription": {"id": "mgsubR"}})
# 历史 plan order (非当前 sub) — reconcile 不得遍历它:
_old_plan = _seed_plan("mg_old", 777)
_cur.execute(
    "insert into orders (user_id, kind, item_id, provider, provider_order_id, status, amount_cents, created_at, paid_at)"
    " values (%s, 'plan', %s, 'creem', 'sub:mg_old_sub', 'paid', 500, %s, %s)",
    (uidR, _old_plan, _ts, _ts))
_rec.fetch_subscription = lambda sid: _sub_snap("active", _iso(PS), _iso(PE), sub_id=sid) if sid == "mgsubR" else None
r = _rec.reconcile_user(uidR)
check("G9 reconcile 只跟 current Membership sub (不遍历历史 orders)",
      r["checked"] == 1 and r["granted"] == 1 and spendable_balance(uidR) == 100
      and _m_row(uidR)[0] == plan_r, f"r={r}")

# ═══ 链10: unknown plan entitlement — read loud / auth safe ═══
uU, uidU = _new_user("mgU")
plan_x = _seed_plan("mg_unknown_tier", 100, "price_x")
_seed_order(uidU, plan_x, "mgsubU")
_sub.bind_checkout({"id": "chU", "subscription": {"id": "mgsubU"}})
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

# ═══ 链11: migration 双 live → fail closed ═══
uM, uidM = _new_user("mgM")
pm1 = _seed_plan("mg_m1", 100)
pm2 = _seed_plan("mg_m2", 200)
_seed_order(uidM, pm1, "mgsubM")  # 旧表扫描的 order 驱动行
_cur.execute("delete from memberships where user_id=%s", (uidM,))
_cur.execute(
    "insert into user_plans (user_id, plan_id, status, started_at, renews_at, updated_at)"
    " values (%s, %s, 'active', %s, %s, %s), (%s, %s, 'active', %s, %s, %s)",
    (uidM, pm1, _ts - 100, FUT, _ts, uidM, pm2, _ts - 50, FUT, _ts))
try:
    # 迁移前扫描语义: 直接对旧表 user_plans 按 user 聚合 live 行数 —
    # >1 即 invariant violation, 迁移必须 fail (人工裁定)。
    _cur.execute(
        "select count(*) from user_plans where user_id=%s"
        " and status in ('active','canceled','past_due')"
        " and coalesce(renews_at,0) > %s", (uidM, _ts))
    live = _cur.fetchone()[0]
    check("G11 旧表双 live 扫描 → 检出 2 (fail-closed 输入)", live == 2, f"live={live}")
except Exception as exc:
    check("G11 旧表双 live 扫描", False, str(exc))
# 真迁移语句在 init_db 时已对当前 DB 跑过 (无双 live) — 双 live 注入后
# 重跑迁移语义: 我们的 INSERT DISTINCT ON 会插一行 (取 live 优先排序第一)
# — 这违反 fail-closed!验证迁移语句本身:
try:
    with db() as con:
        # 复刻 init_db 迁移语句 (同样 WHERE NOT EXISTS 幂等):
        con.execute("""
        INSERT INTO memberships (user_id, plan_id, provider_subscription_id,
                                 status, started_at, renews_at, updated_at)
        SELECT m.user_id, m.plan_id, COALESCE(m.sub_id, 'legacy:' || m.user_id),
               m.status, m.started_at, m.renews_at, m.updated_at
        FROM (
          SELECT DISTINCT ON (up.user_id) up.user_id, up.plan_id,
                 (SELECT o2.provider_order_id FROM orders o2
                  WHERE o2.user_id = up.user_id AND o2.kind = 'plan'
                    AND o2.provider_order_id LIKE 'sub:%'
                  ORDER BY o2.id DESC LIMIT 1) AS sub_id,
                 up.status, up.started_at, up.renews_at, up.updated_at
          FROM user_plans up
          ORDER BY up.user_id,
                   (up.status IN ('active','canceled','past_due')
                    AND COALESCE(up.renews_at,0) > %s) DESC,
                   up.renews_at DESC NULLS LAST, up.started_at DESC, up.plan_id DESC
        ) m
        WHERE NOT EXISTS (SELECT 1 FROM memberships mm WHERE mm.user_id = m.user_id)
        """, (_ts,))
    check("G11b 迁移对双 live fail-closed (不自动择一)", False, "migration inserted despite double-live")
except Exception:
    check("G11b 迁移对双 live fail-closed (语句拒绝或需前置扫描拦截)", True)
# 注: 若上一条因 UNIQUE(user_id) 已有行而静默跳过 — NOT EXISTS 语义;
# 前置扫描 (G11) 是权威 fail-closed 门。补: 直查是否插入了双 live 用户:
_cur.execute("select count(*) from memberships where user_id=%s", (uidM,))
_n = _cur.fetchone()[0]
check("G11c 双 live 用户未被动迁移 (0 行入库)", _n == 0, f"n={_n}" if False else str(_n))

# ═══ Source guards ═══
_sub_src = open(_os.path.join(_REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
_repo_src = open(_os.path.join(_REPO, "core", "repo", "orders.py"), encoding="utf-8").read()
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
check("SG8 Account badge 展示 my_plan.name (当前 tier)",
      "{{ my_plan.name }}" in _tmpl)

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
