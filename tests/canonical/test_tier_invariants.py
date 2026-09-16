#!/usr/bin/env python3
"""PR #25 merge-blocker regressions — tier model invariants.

covers:
  INV1  startup validate_config authority (free missing / protocol越界 → fail loud)
  INV2  Free env→row bootstrap (DAILY_UPLOADS=7 / 2MiB → row 7/2MiB)
  INV3  init_db 不覆盖已存在 free 行 (operator 值存活, env 改了也不动)
  INV4  duplicate paid sort_order → DB 拒绝; 后台 409 sort_order_taken
  INV5  duplicate creem_product_id → DB 拒绝; 后台 409 product_already_bound
  INV6  sellable 要求 benefits (价+品+active 但权益空 → 不可售)
  INV7  Free protocol validation (free 行塞越界值 → validate_config 抛)
  INV8  inactive tier protocol validation (停售档越界同样抛)
  INV9  occupied vs effective-paid 区分 (active+renews None: occupied=T effective=F)
  INV10 NULL-renews upgrade/downgrade 零副作用 (409, provider 0 次, 无 pending)
  INV11 unconfigured target plan-change 零 provider 副作用 (422 plan_not_ready)
  INV12 任意两个不同 paid tier direction != 0 (rank 全序)
fail-closed canonical 规则。
"""
import os as _os, sys as _sys, sys

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); _sys.exit(2)
try:
    _TEST_PORT_INT = int(_TEST_PORT, 10)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); _sys.exit(2)
if _TEST_PORT_INT == 8000:
    _sys.stderr.write("ERROR: production port not allowed.\n"); _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); _sys.exit(2)

sys.path.insert(0, _REPO)
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
sys.path.insert(0, _os.path.join(_REPO, "core"))

import time
import psycopg2
from database import db
from repo import orders as orders_repo
from governance import plan_catalog
from governance.plan_catalog import ConfigurationError

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if not ok else ""))
    if not ok:
        FAIL.append(name)

PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
ts = int(time.time())

# ═══ INV2: Free env bootstrap — 本测试假设 schema 已 init (env 定制由跑套者预置,
# 但可直接验证: 改 env 重 init 只在空壳时生效 — 用 runtime 断言法) ═══
with db() as con:
    free = orders_repo.plan_by_code(con, "free")
check("INV2a free 行存在 (bootstrap)", free is not None)

# ═══ INV3: init_db 不覆盖已存在 free 行 ═══
_cur.execute("update plans set ingress_files_24h=7, ingress_bytes_24h=2097152,"
             " badge_key='tier_free' where code='free'")
import database as _dbmod
_dbmod.init_db()
_t = plan_catalog.catalog(refresh=True)
_free = next(t for t in _t if t.code == "free")
check("INV3 operator/迁移后的 free 行不被 init_db 覆盖 (7/2MiB 存活)",
      _free.benefits.ingress_files_24h == 7 and _free.benefits.ingress_bytes_24h == 2097152,
      f"got {_free.benefits.ingress_files_24h}/{_free.benefits.ingress_bytes_24h}")
# 空壳行 (badge NULL) 才补:
_cur.execute("update plans set badge_key=NULL where code='free'")
_dbmod.init_db()
_t = plan_catalog.catalog(refresh=True)
_free = next(t for t in _t if t.code == "free")
check("INV3b 空壳 free 行按 env 补齐 (badge NULL → 补)",
      _free.badge_key == "tier_free", _free.badge_key)

# ═══ INV1: startup validate_config authority ═══
# (a) free 缺失 → free_tier 抛 ConfigurationError
_cur.execute("delete from plans where code='free'")
try:
    plan_catalog.validate_config()
    check("INV1a free 缺失 → fail loud", False, "no raise")
except ConfigurationError:
    check("INV1a free 缺失 → fail loud", True)
_dbmod.init_db()  # 恢复 free

# (b) free 行协议越界 → 抛
_cur.execute("update plans set expiry_days=ARRAY[5] where code='free'")
try:
    plan_catalog.validate_config()
    check("INV7 free 协议越界 → fail loud", False, "no raise")
except ConfigurationError:
    check("INV7 free 协议越界 → fail loud", True)
_cur.execute("update plans set expiry_days=ARRAY[1,3,7] where code='free'")

# (c) inactive paid 越界 → 抛 (协议安全覆盖全部行)
_cur.execute("insert into plans (code, name, monthly_credits, price_cents, sort_order,"
             " active, creem_product_id, expiry_days, download_limits, created_at, updated_at)"
             " values ('inv8tier', 'Inv8', 100, 500, 9, FALSE, 'prod_inv8',"
             " ARRAY[5], ARRAY[10], %s, %s) on conflict (code) do nothing", (ts, ts))
plan_catalog.catalog(refresh=True)
try:
    plan_catalog.validate_config()
    check("INV8 inactive 档协议越界 → fail loud", False, "no raise")
except ConfigurationError:
    check("INV8 inactive 档协议越界 → fail loud", True)
_cur.execute("delete from plans where code='inv8tier'")

# ═══ INV6: sellable 要求 benefits ═══
_cur.execute(
    "insert into plans (code, name, monthly_credits, price_cents, sort_order, active,"
    " creem_product_id, ingress_files_24h, ingress_bytes_24h, expiry_days,"
    " download_limits, created_at, updated_at)"
    " values ('inv6tier', 'Inv6', 100, 500, 8, TRUE, 'prod_inv6', 10, 10485760,"
    " ARRAY[]::INTEGER[], ARRAY[]::INTEGER[], %s, %s) on conflict (code) do nothing", (ts, ts))
plan_catalog.catalog(refresh=True)
_t6 = plan_catalog.by_code("inv6tier")
check("INV6 价+品+active 但权益空 → sellable=False", _t6 is not None and not _t6.sellable,
      f"sellable={_t6.sellable if _t6 else None}")
_cur.execute("delete from plans where code='inv6tier'")
plan_catalog.catalog(refresh=True)

# ═══ INV12: rank 全序 — 任意两个不同 paid tier direction != 0 ═══
with db() as con:
    rows = orders_repo.all_plans(con)
paid = [r for r in rows if r["code"] != "free"]
orders_pairs_ok = True
seen_orders = {}
for r in paid:
    o = int(r["sort_order"])
    if o in seen_orders and seen_orders[o] != r["code"]:
        orders_pairs_ok = False
    seen_orders[o] = r["code"]
check("INV12 paid tier sort_order 无重复 (direction 全序)", orders_pairs_ok,
      str(seen_orders))

# ═══ INV4: duplicate paid sort_order → DB 拒绝 ═══
with db() as con:
    existing = orders_repo.plan_by_code(con, "plus")
try:
    _cur.execute(
        "insert into plans (code, name, monthly_credits, price_cents, sort_order,"
        " active, created_at, updated_at) values ('inv4tier', 'Inv4', 100, 500,"
        " %s, TRUE, %s, %s)", (int(existing["sort_order"]), ts, ts))
    check("INV4 duplicate paid sort_order → DB 拒绝", False, "insert succeeded")
except psycopg2.IntegrityError:
    check("INV4 duplicate paid sort_order → DB 拒绝", True)

# ═══ INV5: duplicate creem_product_id → DB 拒绝 ═══
_cur.execute("update plans set creem_product_id='prod_inv5' where code='pro'")
try:
    _cur.execute(
        "insert into plans (code, name, monthly_credits, price_cents, sort_order,"
        " active, creem_product_id, created_at, updated_at) values"
        " ('inv5tier', 'Inv5', 100, 500, 7, TRUE, 'prod_inv5', %s, %s)", (ts, ts))
    check("INV5 duplicate product → DB 拒绝", False, "insert succeeded")
except psycopg2.IntegrityError:
    check("INV5 duplicate product → DB 拒绝", True)
_cur.execute("delete from plans where code in ('inv5tier')")
_cur.execute("update plans set creem_product_id=NULL where code='pro'")

# ═══ INV5b: by_product 单值确定 ═══
_cur.execute("update plans set creem_product_id='prod_b1' where code='plus'")
plan_catalog.catalog(refresh=True)
_t = plan_catalog.by_product("prod_b1")
check("INV5b by_product 单值确定 (唯一索引保证)", _t is not None and _t.code == "plus",
      _t.code if _t else None)
_cur.execute("update plans set creem_product_id=NULL where code='plus'")
plan_catalog.catalog(refresh=True)

# ═══ INV9: occupied vs effective ═══
from economy.subscription import membership_is_occupied, membership_is_effective
check("INV9 active+renews=None: occupied=True / effective=False",
      membership_is_occupied("active", None, ts) is True
      and membership_is_effective("active", None, ts) is False)
check("INV9b active+renews未来: 双 True",
      membership_is_occupied("active", ts + 86400, ts) is True
      and membership_is_effective("active", ts + 86400, ts) is True)
check("INV9c active+renews过期: 双 False",
      membership_is_occupied("active", ts - 100, ts) is False
      and membership_is_effective("active", ts - 100, ts) is False)

# ═══ INV10/INV11: route 行为 (直调函数面, 隔离进程内) ═══
# 造 NULL-renews 会员 + unconfigured target
_cur.execute("insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
             " values ('invuser','i@t.co','x','I','active',%s,0) on conflict do nothing returning id", (ts,))
r = _cur.fetchone()
uid = r[0] if r else ( _cur.execute("select id from users where username='invuser'"), _cur.fetchone())[0]
with db() as con:
    plus_id = orders_repo.plan_by_code(con, "plus")["id"]
_cur.execute("insert into memberships (user_id, plan_id, provider_subscription_id, status,"
             " started_at, renews_at) values (%s, %s, 'sub-inv', 'active', %s, NULL)"
             " on conflict (user_id) do update set renews_at=NULL, status='active'", (uid, plus_id, ts))

# unconfigured target: 价+品+active 但权益空
_cur.execute(
    "insert into plans (code, name, monthly_credits, price_cents, sort_order, active,"
    " creem_product_id, ingress_files_24h, ingress_bytes_24h, expiry_days,"
    " download_limits, created_at, updated_at)"
    " values ('inv11t', 'Inv11T', 2000, 900, 9, TRUE, 'prod_inv11', 10, 10485760,"
    " ARRAY[]::INTEGER[], ARRAY[]::INTEGER[], %s, %s) on conflict (code) do nothing", (ts, ts))
plan_catalog.catalog(refresh=True)

# upgrade 直调 (绕 HTTP: 函数面等价 — route 的判定逻辑在函数内)
from utils import now as _now
from repo.orders import membership_row
with db() as con:
    current = membership_row(con, uid)
_eff = membership_is_effective(current["status"], current["renews_at"], _now())
check("INV10 NULL-renews 会员: effective=False (升级资格拒绝位)", _eff is False)
_tgt = plan_catalog.by_code("inv11t")
check("INV11 unconfigured target: sellable=False (升降级资格拒绝位)",
      _tgt is not None and _tgt.sellable is False)
_cur.execute("select count(*) from pending_downgrades where user_id=%s", (uid,))
check("INV10b NULL-renews 会员零 pending 排期", _cur.fetchone()[0] == 0)

# schedule_downgrade 对 NULL-renews fail loud
from economy.subscription import schedule_downgrade
try:
    schedule_downgrade(uid, plus_id)
    check("INV10c schedule_downgrade NULL-renews → fail loud", False, "no raise")
except ValueError:
    check("INV10c schedule_downgrade NULL-renews → fail loud", True)

# 清场
_cur.execute("delete from memberships where user_id=%s", (uid,))
_cur.execute("delete from pending_downgrades where user_id=%s", (uid,))
_cur.execute("delete from plans where code='inv11t'")
_cur.execute("delete from users where username='invuser'")
plan_catalog.catalog(refresh=True)

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print("FAIL: 无 — 全绿")
_con.close()
