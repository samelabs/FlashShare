#!/usr/bin/env python3
"""Membership selection canonical invariants — plan_of() read invariant.

锁住: 无 membership → None; 唯一 live → 返回 (即使非排序第一); 多 live
→ MembershipAmbiguousError (绝不 silently pick); 无 live → deterministic
最近历史 fact (member=false)。Access Projection 传播 ambiguity;
Entitlements fail-safe Free。遵循 canonical fail-closed 规则。
"""
import os as _os, sys as _sys
import sys, time

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

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
from economy.subscription import plan_of, MembershipAmbiguousError
from governance.access_projection import project
from governance.entitlements import resolve, FREE

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
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

def _seed_plan(code, monthly_credits=100):
    _cur.execute(
        "insert into plans (code, name, monthly_credits, price_cents, created_at, updated_at)"
        " values (%s, %s, %s, 500, %s, %s) returning id",
        (code, code.upper(), monthly_credits, _ts, _ts))
    return _cur.fetchone()[0]

def _upsert_plan(uid, plan_id, status, started, renews):
    _cur.execute(
        "insert into user_plans (user_id, plan_id, status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, %s)"
        " on conflict (user_id, plan_id) do update set status=excluded.status,"
        " started_at=excluded.started_at, renews_at=excluded.renews_at",
        (uid, plan_id, status, started, renews, _ts))

FUT = _ts + 30 * 86400   # future renews (live boundary)
PAST = _ts - 86400       # past renews (dead)

# ═══ MS1 — no membership ═══
u1, uid1 = _new_user("ms1")
check("MS1 无 membership → plan_of() is None", plan_of(uid1) is None)

# ═══ MS2 — single live ═══
u2, uid2 = _new_user("ms2")
p2 = _seed_plan(f"plus{uid2}")
_upsert_plan(uid2, p2, "active", _ts - 100, FUT)
r = plan_of(uid2)
check("MS2 唯一 live → 该 plan, member=true, shape 不变",
      r and r["code"] == f"plus{uid2}" and r["member"] is True
      and set(r) == {"code", "name", "status", "renews_at",
                     "cancel_scheduled_at", "member"}, str(r))

# ═══ MS3 — live beats newer historical ═══
u3, uid3 = _new_user("ms3")
pa = _seed_plan(f"planA{uid3}")
pb = _seed_plan(f"planB{uid3}")
# A: live 但 renews 较早 (30 天后); B: non-live 但 renews 时间更晚 (值在未来但
# status=expired → 非 live) 且 started 更晚:
_upsert_plan(uid3, pa, "active", _ts - 200, _ts + 30 * 86400)
_upsert_plan(uid3, pb, "expired", _ts - 50, _ts + 60 * 86400)
r = plan_of(uid3)
check("MS3 live beats newer historical (live 语义 > 机械排序)",
      r and r["code"] == f"planA{uid3}" and r["member"] is True, str(r))

# ═══ MS4 — multiple historical deterministic ═══
u4, uid4 = _new_user("ms4")
h1 = _seed_plan(f"old{uid4}")
h2 = _seed_plan(f"new{uid4}")
# 两条均 non-live; h2 renews 更近 → 排序第一:
_upsert_plan(uid4, h1, "expired", _ts - 90, PAST - 5000)
_upsert_plan(uid4, h2, "expired", _ts - 50, PAST)
r = plan_of(uid4)
check("MS4a 两条 historical → renews_at DESC 最近者, member=false",
      r and r["code"] == f"new{uid4}" and r["member"] is False, str(r))
# 反转插入顺序, 结果不变:
_cur.execute("delete from user_plans where user_id=%s", (uid4,))
_upsert_plan(uid4, h2, "expired", _ts - 50, PAST)
_upsert_plan(uid4, h1, "expired", _ts - 90, PAST - 5000)
r = plan_of(uid4)
check("MS4b 插入顺序反转 → 结果不变 (deterministic)", r and r["code"] == f"new{uid4}")
# tie: 同 renews → started_at DESC:
_cur.execute("delete from user_plans where user_id=%s", (uid4,))
_upsert_plan(uid4, h1, "expired", _ts - 10, PAST)
_upsert_plan(uid4, h2, "expired", _ts - 90, PAST)
r = plan_of(uid4)
check("MS4c 同 renews → started_at DESC (h1 后 start 胜)", r and r["code"] == f"old{uid4}", str(r))

# ═══ MS5 — two live = AMBIGUOUS ═══
u5, uid5 = _new_user("ms5")
la = _seed_plan(f"liveA{uid5}")
lb = _seed_plan(f"liveB{uid5}")
_upsert_plan(uid5, la, "active", _ts - 100, FUT)
_upsert_plan(uid5, lb, "active", _ts - 90, FUT + 86400)
try:
    plan_of(uid5)
    check("MS5 双 live → MembershipAmbiguousError (绝不返回其一)", False, "no raise")
except MembershipAmbiguousError as exc:
    check("MS5 双 live → MembershipAmbiguousError (绝不返回其一)", "live" in str(exc))
# canceled-live / past_due-live 也算 live:
_cur.execute("delete from user_plans where user_id=%s", (uid5,))
_upsert_plan(uid5, la, "canceled", _ts - 100, FUT)      # canceled 当期内 = live
_upsert_plan(uid5, lb, "past_due", _ts - 90, FUT)       # 宽限期 = live
try:
    plan_of(uid5)
    check("MS5b canceled/past_due 当期双 live → 同样 AMBIGUOUS", False, "no raise")
except MembershipAmbiguousError:
    check("MS5b canceled/past_due 当期双 live → 同样 AMBIGUOUS", True)

# ═══ MS6 — Access Projection propagates ═══
try:
    project(uid5)
    check("MS6 双 live → project() 传播 MembershipAmbiguousError (不装 None/Free)",
          False, "no raise")
except MembershipAmbiguousError:
    check("MS6 双 live → project() 传播 MembershipAmbiguousError (不装 None/Free)", True)

# ═══ MS7 — Entitlement fail-safe ═══
r = resolve(uid5)
check("MS7 同一双 live 用户 → resolve() fail-safe FREE (fact loud, auth safe)",
      r == dict(FREE) or (r["plan_code"] == "free"
      and r["ingress_files_24h"] == FREE["ingress_files_24h"]), str(r["plan_code"]))

# ═══ MS8 — one live + historical rows ═══
u8, uid8 = _new_user("ms8")
o1 = _seed_plan(f"old{uid8}")
o2 = _seed_plan(f"canc{uid8}")
o3 = _seed_plan(f"curr{uid8}")
_upsert_plan(uid8, o1, "expired", _ts - 300, PAST - 10000)      # 老 expired
_upsert_plan(uid8, o2, "canceled", _ts - 200, PAST)             # canceled 已过期
_upsert_plan(uid8, o3, "active", _ts - 100, FUT)                # 当前 active
r = plan_of(uid8)
check("MS8 expired-old + canceled-ended + active-current → 稳定返回唯一 active",
      r and r["code"] == f"curr{uid8}" and r["member"] is True, str(r))

# ═══ Source guards ═══
_repo_src = open(_os.path.join(_REPO, "core", "repo", "orders.py"), encoding="utf-8").read()
_ms_blk = _repo_src.split("def membership_rows")[1].split("\ndef ")[0]
check("SG1 repo membership reader 返回 list (fetchall)",
      "fetchall()" in _ms_blk and ".fetchone()" not in _ms_blk)
check("SG2 明确 ORDER BY 三键", "ORDER BY up.renews_at DESC NULLS LAST" in _ms_blk
      and "up.started_at DESC" in _ms_blk and "up.plan_id DESC" in _ms_blk)
_ms_order = _ms_blk[_ms_blk.index("ORDER BY"):] if "ORDER BY" in _ms_blk else ""
check("SG3 ORDER BY 子句不引用 price/sort_order/credits",
      _ms_order and "price" not in _ms_order.lower()
      and "sort_order" not in _ms_order and "credits" not in _ms_order.lower())
_sub_src = open(_os.path.join(_REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
_po_blk = _sub_src.split("def plan_of")[1].split("\ndef ")[0]
check("SG4 plan_of 使用 plural rows reader",
      "membership_rows(" in _po_blk and ".fetchone()" not in _po_blk)
check("SG5 live candidates 计数 + >1 raise",
      "len(live) > 1" in _po_blk and "MembershipAmbiguousError" in _po_blk)
check("SG6 无 rows[0] 解双 live (dicts[0] 仅 0-live 历史回退)",
      "rows[0]" not in _po_blk and "dicts[0]" in _po_blk
      and _po_blk.index("len(live) > 1") < _po_blk.index("dicts[0]"))
_proj_src = open(_os.path.join(_REPO, "core", "governance", "access_projection.py"), encoding="utf-8").read()
check("SG7 projection 不 catch MembershipAmbiguousError/plan_of 异常",
      "MembershipAmbiguousError" not in _proj_src
      and "except" not in _proj_src.split("def project")[1])
_ent_src = open(_os.path.join(_REPO, "core", "governance", "entitlements.py"), encoding="utf-8").read()
_res_blk = _ent_src.split("def resolve(")[1].split("\ndef ")[0]
check("SG8 entitlements.resolve 既有 exception → Free fallback 保持",
      "except Exception:" in _res_blk)

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
