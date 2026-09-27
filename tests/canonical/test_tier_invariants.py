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
from tests.support.cleanup import purge_users  # noqa: E402
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


def _fresh_paid_rank():
    """当前共享库未占用的 paid rank (本 suite 任何非 rank-collision 夹具
    都必须用它 — 共享 CI 顺序库里 rank 已被前序 suite 占满)。"""
    _cur.execute(
        "select coalesce(max(sort_order), 0) + 1 "
        "from plans where code <> 'free'")
    return int(_cur.fetchone()[0])


# 套件自状态清理: 只清本 suite 的 inv*/ti*/tj*/dk*/dl*/invuser 命名域,
# 绝不触碰前序 suite 的数据 (共享 canonical DB 合同)。
def _cleanup_own_rows():
    _cur.execute(
        "delete from pending_downgrades where user_id in"
        " (select id from users where username like 'invuser' or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$')")
    _cur.execute(
        "delete from memberships where user_id in"
        " (select id from users where username like 'invuser' or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$')")
    _cur.execute(
        "delete from sessions where user_id in"
        " (select id from users where username like 'invuser' or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$')")
    _cur.execute(
        "delete from orders where user_id in"
        " (select id from users where username like 'invuser' or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$')")
    _cur.execute(
        "select id from users where username like 'invuser'"
        " or username ~ '^(ti|tj|dk|dl)[0-9a-f]{7}$'")
    purge_users(_cur, [r[0] for r in _cur.fetchall()])
    _cur.execute(
        "delete from plans where code in"
        " ('inv8tier', 'inv6tier', 'inv4tier', 'inv5tier', 'inv11t')")


# 首跑前清一次: 上次异常退出遗留的本 suite 数据不得造成假失败/假通过
_cleanup_own_rows()

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
# badge_key NULL 不再被 init_db 补齐 (historical backfill 已删) —
# 空壳行属 schema violation, 由 validate_config 负责暴露而非修复。
_cur.execute("update plans set badge_key=NULL where code='free'")
_dbmod.init_db()
_t = plan_catalog.catalog(refresh=True)
_free = next(t for t in _t if t.code == "free")
check("INV3b init_db 不再修 badge_key (无 backfill, NULL 存活)",
      _free.badge_key == "", _free.badge_key)  # Tier 投影: NULL → ""

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
             " values ('inv8tier', 'Inv8', 100, 500, %s, FALSE, 'prod_inv8',"
             " ARRAY[5], ARRAY[10], %s, %s) on conflict (code) do nothing",
             (_fresh_paid_rank(), ts, ts))
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
    " values ('inv6tier', 'Inv6', 100, 500, %s, TRUE, 'prod_inv6', 10, 10485760,"
    " ARRAY[]::INTEGER[], ARRAY[]::INTEGER[], %s, %s) on conflict (code) do nothing",
    (_fresh_paid_rank(), ts, ts))
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
except psycopg2.IntegrityError as exc:
    # 只有撞 rank 唯一约束才算本断言成立; 其它约束 (如 code 残留) 必须 FAIL
    _cname = exc.diag.constraint_name
    check("INV4 duplicate paid sort_order → DB 拒绝 (plans_paid_sort_order_uk)",
          _cname == "plans_paid_sort_order_uk", f"constraint={_cname}")

# ═══ INV5: duplicate creem_product_id → DB 拒绝 ═══
_cur.execute("update plans set creem_product_id='prod_inv5' where code='pro'")
try:
    _cur.execute(
        "insert into plans (code, name, monthly_credits, price_cents, sort_order,"
        " active, creem_product_id, created_at, updated_at) values"
        " ('inv5tier', 'Inv5', 100, 500, %s, TRUE, 'prod_inv5', %s, %s)",
        (_fresh_paid_rank(), ts, ts))  # fresh rank: 本测试只验 product 唯一性
    check("INV5 duplicate product → DB 拒绝", False, "insert succeeded")
except psycopg2.IntegrityError as exc:
    # 只有撞 product 唯一约束才算本断言成立; rank 冲突等其它约束必须 FAIL
    _cname = exc.diag.constraint_name
    check("INV5 duplicate product → DB 拒绝 (plans_product_binding_uk)",
          _cname == "plans_product_binding_uk", f"constraint={_cname}")
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
    " values ('inv11t', 'Inv11T', 2000, 900, %s, TRUE, 'prod_inv11', 10, 10485760,"
    " ARRAY[]::INTEGER[], ARRAY[]::INTEGER[], %s, %s) on conflict (code) do nothing",
    (_fresh_paid_rank(), ts, ts))
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
_cur.execute(
    "select id from users where username='invuser'")
purge_users(_cur, [r[0] for r in _cur.fetchall()])
plan_catalog.catalog(refresh=True)

# ═══ INV10/INV11 route-level: 真实 ASGI 路由 + provider 调用计数 ═══
import uuid
sys.path.insert(0, _REPO)
from app.main import app
from app.web_auth import create_browser_session, COOKIE_NAME
import warnings as _w
with _w.catch_warnings():
    _w.simplefilter("ignore", DeprecationWarning)
    from starlette.testclient import TestClient
from economy import creem as _creem

_client = TestClient(app)
_prov_calls = {"upgrade": 0}
_orig_upgrade = _creem.upgrade_subscription
def _fake_upgrade(sub, prod, update_behavior="x"):
    _prov_calls["upgrade"] += 1
    return {"id": sub}
_creem.upgrade_subscription = _fake_upgrade
_orig_enabled = _creem.creem_enabled
_creem.creem_enabled = lambda: True

def _post(path, body, cookie):
    r = _client.post(path, json=body, cookies={COOKIE_NAME: cookie},
                     follow_redirects=False)
    try:
        d = r.json()
    except Exception:
        d = {"_raw": r.text[:120]}
    return r.status_code, d

# ── C1: bound-but-unpaid (active + renews_at=NULL) ──
_u = "ti" + uuid.uuid4().hex[:7]
_cur.execute(
    "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
    " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id", (_u, f"{_u}@t.co", ts, ts))
_c1_uid = _cur.fetchone()[0]
with db() as con:
    _plus_id = orders_repo.plan_by_code(con, "plus")["id"]
    _pro_id = orders_repo.plan_by_code(con, "pro")["id"]
# pro 配齐可售 (upgrade/downgrade target 都有效), plus 是当前档
_cur.execute("update plans set creem_product_id='prod_ti_pro', price_cents=1500 where code='pro'")
_cur.execute("update plans set creem_product_id='prod_ti_plus', price_cents=500 where code='plus'")
from governance.plan_catalog import validate_config as _vci
_vci()
_cur.execute(
    "insert into memberships (user_id, plan_id, provider_subscription_id, status, started_at, renews_at)"
    " values (%s, %s, 'sub:tiunpaid', 'active', %s, NULL)", (_c1_uid, _plus_id, ts))
_c1_sess = create_browser_session(_c1_uid)
_cur.execute("select renews_at, status from memberships where user_id=%s", (_c1_uid,))
_m0 = _cur.fetchone()
_cur.execute("select count(*) from ledger where user_id=%s", (_c1_uid,))
_l0 = _cur.fetchone()[0]
_cur.execute("select count(*) from pending_downgrades where user_id=%s", (_c1_uid,))
_p0 = _cur.fetchone()[0]

st, d = _post("/api/topup/plan-upgrade", {"plan": "pro"}, _c1_sess)
check("INV10-R1 upgrade NULL-renews → 409 no_active_membership",
      st == 409 and d.get("error") == "no_active_membership", f"st={st} d={d}")
st, d = _post("/api/topup/plan-downgrade", {"plan": "pro"}, _c1_sess)
check("INV10-R1b downgrade NULL-renews → 409 no_active_membership",
      st == 409 and d.get("error") == "no_active_membership", f"st={st} d={d}")
check("INV10-R2 provider calls = 0 (双请求)",
      _prov_calls["upgrade"] == 0, _prov_calls)
_cur.execute("select renews_at, status from memberships where user_id=%s", (_c1_uid,))
check("INV10-R3 membership 行不变", _cur.fetchone() == _m0)
_cur.execute("select count(*) from ledger where user_id=%s", (_c1_uid,))
check("INV10-R4 ledger 不变", _cur.fetchone()[0] == _l0)
_cur.execute("select count(*) from pending_downgrades where user_id=%s", (_c1_uid,))
check("INV10-R5 pending_downgrades 零写入", _cur.fetchone()[0] == _p0 == 0)

# ── C2: target 价+品+active 但 benefits 未配置 (有效会员) ──
_u2 = "tj" + uuid.uuid4().hex[:7]
_cur.execute(
    "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
    " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id", (_u2, f"{_u2}@t.co", ts, ts))
_c2_uid = _cur.fetchone()[0]
_cur.execute(
    "insert into memberships (user_id, plan_id, provider_subscription_id, status, started_at, renews_at)"
    " values (%s, %s, 'sub:tipaid', 'active', %s, %s)", (_c2_uid, _plus_id, ts, ts + 30*86400))
_c2_sess = create_browser_session(_c2_uid)
# target = pro 但清空 benefits (价/品保留) — upgrade direction 合法但 target 不可售
_cur.execute("update plans set expiry_days=ARRAY[]::INTEGER[], download_limits=ARRAY[]::INTEGER[] where code='pro'")
_vci()
_cur.execute("select count(*) from ledger where user_id=%s", (_c2_uid,))
_l2 = _cur.fetchone()[0]
_cur.execute("select count(*) from pending_downgrades where user_id=%s", (_c2_uid,))
_p2 = _cur.fetchone()[0]
st, d = _post("/api/topup/plan-upgrade", {"plan": "pro"}, _c2_sess)
check("INV11-R1 upgrade unconfigured target → 422 plan_not_ready",
      st == 422 and d.get("error") == "plan_not_ready", f"st={st} d={d}")
# downgrade direction: 清 TARGET(plus) 的 benefits (current=pro 保持完整可读)
_cur.execute("update plans set creem_product_id='prod_ti_pro2', price_cents=1500, expiry_days=ARRAY[1,3,7,30], download_limits=ARRAY[1,10,50,100,200,1000] where code='pro'")
_cur.execute("update plans set expiry_days=ARRAY[]::INTEGER[], download_limits=ARRAY[]::INTEGER[] where code='plus'")
_cur.execute("update memberships set plan_id=%s where user_id=%s", (_pro_id, _c2_uid))
_vci()
st, d = _post("/api/topup/plan-downgrade", {"plan": "plus"}, _c2_sess)
check("INV11-R1b downgrade unconfigured target → 422 plan_not_ready",
      st == 422 and d.get("error") == "plan_not_ready", f"st={st} d={d}")
check("INV11-R2 provider calls 仍 = 0",
      _prov_calls["upgrade"] == 0, _prov_calls)
_cur.execute("select count(*) from pending_downgrades where user_id=%s", (_c2_uid,))
check("INV11-R3 pending 零写入", _cur.fetchone()[0] == _p2 == 0)
_cur.execute("select count(*) from ledger where user_id=%s", (_c2_uid,))
check("INV11-R4 ledger 不变", _cur.fetchone()[0] == _l2)

# ── 恢复 fixture 原值 (不污染后续 suite) ──
_cur.execute("update plans set creem_product_id=NULL, price_cents=0,"
             " expiry_days=ARRAY[1,3,7,30], download_limits=ARRAY[1,10,50,100,200,1000] where code='pro'")
_cur.execute("update plans set creem_product_id=NULL, price_cents=0,"
             " expiry_days=ARRAY[1,3,7,30], download_limits=ARRAY[1,10,50,100,200,1000] where code='plus'")
_cur.execute("delete from memberships where user_id in (%s, %s)", (_c1_uid, _c2_uid))
_cur.execute("delete from pending_downgrades where user_id in (%s, %s)", (_c1_uid, _c2_uid))
_cur.execute("delete from sessions where user_id in (%s, %s)", (_c1_uid, _c2_uid))
purge_users(_cur, [_c1_uid, _c2_uid])
_vci()
_creem.upgrade_subscription = _orig_upgrade
_creem.creem_enabled = _orig_enabled

# 清场
_cleanup_own_rows()

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print("FAIL: 无 — 全绿")
_con.close()
