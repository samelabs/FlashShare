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
sys.path.insert(0, _os.path.join(_REPO, "core"))

import psycopg2
from database import db
from repo import orders as orders_repo
from economy import subscription as _sub
from economy.billing import spendable_balance
from economy import creem as _creem
from governance.tiers import PAID_TIER_CODES, TIER_PLAN_SPECS, is_paid_tier
from governance.entitlements import resolve, resolve_from_membership, entitlement_profile_exists
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
_cur.execute("delete from users where username like 'pt%'")

def _new_user(prefix):
    import uuid
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts, _ts))
    return u, _cur.fetchone()[0]

def _sub_snap(status, p_start, p_end, product=None, sub_id="ptsub"):
    d = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": p_start, "current_period_end_date": p_end}
    if product:
        d["product"] = product
    return d

def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _seed_sub_user(prefix, plan_code, product, sub_id, paid=True):
    u, uid = _new_user(prefix)
    _cur.execute("select id from plans where code=%s", (plan_code,))
    row = _cur.fetchone()
    plan_id = row[0]
    _cur.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, created_at, paid_at)"
        " values (%s, 'plan', %s, 'creem', %s, 'paid', 500, %s, %s) returning id",
        (uid, plan_id, f"sub:{sub_id}", _ts, _ts))
    _sub.bind_checkout({"id": "ch_" + sub_id, "subscription": {"id": sub_id}})
    if paid:
        _sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), sub_id=sub_id))
    return u, uid, plan_id

def _grants(uid, kind):
    _cur.execute("select count(*) from ledger where user_id=%s and kind=%s", (uid, kind))
    return _cur.fetchone()[0]

def _plan_credits_total(uid, period_start):
    with db() as con:
        return orders_repo.period_plan_credits(con, uid, period_start)

# ═══ T1: paid tier codes exactly plus/pro ═══
check("T1 PAID_TIER_CODES == ('plus','pro')", PAID_TIER_CODES == ("plus", "pro"),
      str(PAID_TIER_CODES))

# ═══ T2: arbitrary third active plan 不进入 sellable ═══
_cur.execute(
    "insert into plans (code, name, monthly_credits, period_days, period,"
    " price_cents, sort_order, active, created_at, updated_at)"
    " values ('pt_business', 'Business', 999, 30, 'monthly', 900, 5, TRUE, %s, %s)"
    " on conflict (code) do nothing", (_ts, _ts))
with db() as con:
    sellable = [r["code"] for r in orders_repo.canonical_plans(con)]
check("T2 第三 active plan 不进 sellable catalog (canonical_plans 只 plus/pro)",
      "pt_business" not in sellable and set(sellable) <= {"plus", "pro"}, str(sellable))

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
check("T5 Plus monthly credits = 100", TIER_PLAN_SPECS["plus"]["monthly_credits"] == 100)
check("T6 Pro monthly credits = 500", TIER_PLAN_SPECS["pro"]["monthly_credits"] == 500)
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
_u7, uid7, _ = _seed_sub_user("pt7", "plus", "prod_plus", "ptsub7", paid=False)
_cur.execute("update plans set creem_product_id='prod_pro' where code='pro'")
_cur.execute("update plans set creem_product_id='prod_plus' where code='plus'")
out = _creem.create_subscription_checkout(1234, {"creem_product_id": "prod_pro"}, {"id": uid7})
check("T7 subscription checkout → Pro product / ZERO custom_price",
      _captured["payload"] == {"product_id": "prod_pro"}, str(_captured["payload"]))
out = _creem.create_subscription_checkout(1235, {"creem_product_id": "prod_plus"}, {"id": uid7})
check("T8 subscription checkout → Plus product / ZERO custom_price",
      _captured["payload"] == {"product_id": "prod_plus"}, str(_captured["payload"]))

# ═══ 前合同: shared Credits Product / custom_price / wrong product / amount / replay ═══
_os.environ["CREEM_CREDITS_PRODUCT_ID"] = "prod_credits"
import importlib
importlib.reload(_creem)
_captured2 = {}
_creem._checkout_session = lambda order_id, user, pp: (_captured2.update(payload=pp) or {"ok": True, "checkout_url": "u", "checkout_id": "c"})
_creem.create_credits_checkout(99, 2500, {"id": 1})
check("P1 credits checkout: shared product + custom_price=amount",
      _captured2["payload"]["product_id"] == "prod_credits"
      and _captured2["payload"]["custom_price"] == "2500", str(_captured2["payload"]))
# fulfill wrong product → zero grant:
_u9, uid9 = _new_user("pt9")
_cur.execute(
    "insert into orders (user_id, kind, item_id, provider, provider_order_id, status, amount_cents, currency, created_at)"
    " values (%s, 'package', 1, 'creem', 'ord_pt9', 'pending', 500, 'USD', %s) returning id", (uid9, _ts))
_oid9 = _cur.fetchone()[0]
try:
    _creem.fulfill("ord_pt9", "", 500, product_id="prod_WRONG")
    check("P2 wrong product → ZERO grant (raise)", False, "no raise")
except KeyError as e:
    _cur.execute("select count(*) from ledger where user_id=%s", (uid9,))
    check("P2 wrong product → ZERO grant (raise)", "product_mismatch" in str(e) and _cur.fetchone()[0] == 0, str(e))
# amount mismatch → zero:
try:
    _creem.fulfill("ord_pt9", "", 999, product_id="prod_credits")
    check("P3 amount mismatch → ZERO grant", False, "no raise")
except KeyError as e:
    _cur.execute("select count(*) from ledger where user_id=%s", (uid9,))
    check("P3 amount mismatch → ZERO grant", "amount_mismatch" in str(e) and _cur.fetchone()[0] == 0)
# correct → grant once; replay → exactly once:
_cur.execute("select credits from packages order by id limit 1")
_pkgc = _cur.fetchone()
_pkg_credits = _pkgc[0] if _pkgc else 100
if not _pkgc:
    _cur.execute("insert into packages (code, name, credits, price_cents, currency, active, created_at, updated_at)"
                 " values ('pt_pkg', 'PT', 100, 500, 'USD', TRUE, %s, %s)", (_ts, _ts))
    _cur.execute("update orders set item_id=(select id from packages where code='pt_pkg') where id=%s", (_oid9,))
    _pkg_credits = 100
r1 = _creem.fulfill("ord_pt9", "", 500, product_id="prod_credits")
_bal = spendable_balance(uid9)
r2 = _creem.fulfill("ord_pt9", "", 500, product_id="prod_credits")
check("P4 correct product+amount → grant once; replay exactly once",
      spendable_balance(uid9) == _pkg_credits and _grants(uid9, "grant_package") == 1,
      f"bal={spendable_balance(uid9)} grants={_grants(uid9,'grant_package')}")

# ═══ T9: Plus → Pro upgrade call shape ═══
_up_called = {}
def _fake_post(path, payload):
    _up_called["path"] = path
    _up_called["payload"] = payload
    return {"id": "ptsub10", "status": "active"}
_orig_post = _creem._post
_creem._post = _fake_post
_creem.upgrade_subscription("ptsub10", "prod_pro")
check("T9 upgrade → POST /v1/subscriptions/{id}/upgrade, target Pro product, proration-charge-immediately",
      _up_called["path"] == "/v1/subscriptions/ptsub10/upgrade"
      and _up_called["payload"] == {"product_id": "prod_pro",
                                    "update_behavior": "proration-charge-immediately"},
      str(_up_called))

# ═══ T10: subscription.update Pro product → Membership/Projection Pro ═══
_u10, uid10, _ = _seed_sub_user("pt10", "plus", "prod_plus", "ptsub10")
_pp = project(uid10)
check("T10pre update 前 Membership=Plus / 500 files",
      _pp["entitlements"]["plan_code"] == "plus" and _pp["entitlements"]["ingress_files_24h"] == 500)
_sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
_pp = project(uid10)
_r10 = resolve(uid10)
check("T10 update Pro product → Membership Pro / projection Pro / 2000 files / 20GiB",
      _pp["membership"]["code"] == "pro" and _pp["entitlements"]["plan_code"] == "pro"
      and _r10["ingress_files_24h"] == 2000 and _r10["ingress_bytes_24h"] == 20 * 1024**3,
      str(_pp["entitlements"]["plan_code"]))

# ═══ T11: Plus 100 → upgrade 补差 +400 → period total 500 ═══
_bal11 = spendable_balance(uid10)
_adj = _grants(uid10, "grant_plan_upgrade")
_total11 = _plan_credits_total(uid10, PS)
check("T11 upgrade 补差 +400 / plan-derived period total = 500 (非 600)",
      _adj == 1 and spendable_balance(uid10) == _bal11  # balance 已含在 update 内授
      and _total11 == 500, f"adj_grants={_adj} total={_total11}")
# 重放同 update:
_sub.handle_event("subscription.update", _sub_snap("active", _iso(PS), _iso(PE), product={"id": "prod_pro"}, sub_id="ptsub10"))
# ═══ T12: replay → exactly once ═══
check("T12 replay upgrade events → 补差 exactly once",
      _grants(uid10, "grant_plan_upgrade") == 1 and _plan_credits_total(uid10, PS) == 500)
# 同周期 paid 重放 → 不再全额 500:
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS), _iso(PE), sub_id="ptsub10"))
check("T12b 同周期 paid 重放 → period total 仍 500 (不 100+400+500)",
      _plan_credits_total(uid10, PS) == 500, str(_plan_credits_total(uid10, PS)))

# ═══ T13: Pro → Plus request → Membership 仍 Pro ═══
_u13, uid13, _ = _seed_sub_user("pt13", "pro", "prod_pro", "ptsub13")
_cur.execute("select id from plans where code='plus'")
_sub.schedule_downgrade(uid13, _cur.fetchone()[0])
_pp13 = project(uid13)
check("T13 downgrade 请求后 Membership 仍 Pro / entitlements Pro / 零追回",
      _pp13["membership"]["code"] == "pro" and _pp13["entitlements"]["plan_code"] == "pro"
      and _grants(uid13, "grant_plan") == 1 and spendable_balance(uid13) == 500)

# ═══ T14: period boundary → Plus / next-cycle 100 ═══
PS2, PE2 = _ts + 31 * 86400, _ts + 61 * 86400
_bal14 = spendable_balance(uid13)
_sub.handle_event("subscription.paid", _sub_snap("active", _iso(PS2), _iso(PE2), sub_id="ptsub13"))
_pp14 = project(uid13)
check("T14 周期边界 → Membership Plus / 新周期 grant 100 / pending fact 清除",
      _pp14["membership"]["code"] == "plus" and spendable_balance(uid13) == _bal14 + 100
      and _plan_credits_total(uid13, PS2) == 100,
      f"code={_pp14['membership']['code']} bal_delta={spendable_balance(uid13)-_bal14}")
with db() as con:
    _pend14 = orders_repo.pending_downgrade(con, uid13)
check("T14b pending downgrade fact 已消费", _pend14 is None)

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
_tiers_src = open(_os.path.join(_REPO, "core", "governance", "tiers.py"), encoding="utf-8").read()
_ent_src = open(_os.path.join(_REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_creem_src = open(_os.path.join(_REPO, "core", "economy", "creem.py"), encoding="utf-8").read()
_topup_src = open(_os.path.join(_REPO, "app", "routes", "topup.py"), encoding="utf-8").read()
_samelabs_src = open(_os.path.join(_REPO, "app", "routes", "samelabs.py"), encoding="utf-8").read()
_sub_src2 = open(_os.path.join(_REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
check("PG1 tiers 单源: PAID_TIER_CODES + TIER_PLAN_SPECS",
      'PAID_TIER_CODES = ("plus", "pro")' in _tiers_src
      and '"monthly_credits": 100' in _tiers_src and '"monthly_credits": 500' in _tiers_src)
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
check("PG4 POST /samelabs/plans 创建能力已移除",
      '@router.post("/samelabs/plans")' not in _samelabs_src
      and "not_canonical_tier" in _samelabs_src)
check("PG5 upgrade 走 provider API (无本地 Membership 伪造)",
      "upgrade_subscription" in _creem_src and "proration-charge-immediately" in _creem_src
      and "upgrade_subscription(" in _topup_src)
_sub_blk2 = _sub_src2.split("def _ev_update")[1].split("\ndef ")[0]
check("PG6 升级补差 ledger kind=grant_plan_upgrade / 幂等键含 target",
      "grant_plan_upgrade" in _sub_blk2 and "plan-upgrade:" in _sub_blk2)
check("PG7 period credits 从 ledger 聚合 (无第二余额字段)",
      "period_plan_credits" in _sub_src2 and "def period_plan_credits" in
      open(_os.path.join(_REPO, "core", "repo", "orders.py"), encoding="utf-8").read())
_db_src = open(_os.path.join(_REPO, "core", "database.py"), encoding="utf-8").read()
check("PG8 canonical seeds (plus 100 / pro 500) + rename 迁移",
      "'plus', 'Plus', 100" in _db_src.replace('"', "'") or ("'plus'" in _db_src and "'pro'" in _db_src)
      and "RENAME COLUMN creem_price_id TO creem_product_id" in _db_src)
_tmpl_src = open(_os.path.join(_REPO, "app", "templates", "account", "credits.html"), encoding="utf-8").read()
check("PG9 Account 三态动作 (Subscribe/Current/Upgrade/Downgrade)",
      "sub-upgrade" in _tmpl_src and "sub-downgrade" in _tmpl_src and "sub_current" in _tmpl_src)

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
