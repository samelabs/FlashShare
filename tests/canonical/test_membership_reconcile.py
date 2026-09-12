#!/usr/bin/env python3
"""Membership reconcile canonical invariants — 四事实边界合同.

锁住: user_subscriptions 显式 read model、唯一 paid-period 实现、
补偿 eligibility (active/scheduled_cancel only)、scheduled_cancel
marker 保留、last_grant 判据、ledger 幂等防线、provider failure
best-effort。遵循 canonical fail-closed 规则。
"""
import os as _os, sys as _sys
import sys
import json, time

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

# ── FAIL-CLOSED GATE (同 canonical 族) ──────────────────────────────────
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
from economy import reconcile as _rec
from economy.billing import spendable_balance

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

def _seed_plan_order(uid, monthly_credits=100):
    _cur.execute(
        "insert into plans (code, name, monthly_credits, price_cents, created_at, updated_at)"
        " values (%s, %s, %s, 500, %s, %s) returning id",
        (f"plus{uid}", f"PLUS{uid}", monthly_credits, _ts, _ts))
    plan_id = _cur.fetchone()[0]
    _cur.execute(
        "insert into orders (user_id, kind, item_id, provider, provider_order_id,"
        " status, amount_cents, created_at, paid_at)"
        " values (%s, 'plan', %s, 'creem', %s, 'paid', 500, %s, %s) returning id",
        (uid, plan_id, f"sub:test{uid}", _ts, _ts))
    oid = _cur.fetchone()[0]
    # membership 建档 (新单行模型; fixture 直建, sub 唯一)
    _cur.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, updated_at)"
        " values (%s, %s, %s, %s, %s, NULL, %s)"
        " on conflict (user_id) do update set plan_id=excluded.plan_id,"
        " provider_subscription_id=excluded.provider_subscription_id, status=excluded.status",
        (uid, plan_id, f"sub:test{uid}", "active", _ts, _ts))
    return plan_id, oid, f"sub:test{uid}"

def _sub_snapshot(status, p_start, p_end):
    return {"object": "subscription", "status": status,
            "current_period_start_date": p_start, "current_period_end_date": p_end}

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

# ═══ R1 — read contract ═══
u1, uid1 = _new_user("rc1")
pid1, oid1, sub1 = _seed_plan_order(uid1)
with db() as con:
    rows = orders_repo.user_subscriptions(con, uid1)
row = dict(rows[0])
_required = {"order_id", "plan_id", "provider_order_id", "order_status",
             "order_created_at", "membership_status", "membership_started_at",
             "membership_renews_at", "last_grant_period_start",
             "cancel_scheduled_at"}
check("R1a user_subscriptions 显式 alias 字段齐全", _required <= set(row), str(sorted(row)))
check("R1b order fact 正确 (order_status=paid, plan_id)",
      row["order_status"] == "paid" and row["plan_id"] == pid1)
check("R1c 无 membership 行时 membership_status 为 NULL (LEFT JOIN)",
      row["membership_status"] is None and row["last_grant_period_start"] is None)

# ═══ R2 — active missing grant → exactly one grant ═══
PS, PE = _ts - 3600, _ts + 2592000
def _fake_fetch(snap):
    def _f(sub_id):
        return snap
    return _f

_bal0 = spendable_balance(uid1)
_gr0 = _ledger_grants(uid1)
_rec.fetch_subscription = _fake_fetch(_sub_snapshot("active", _iso(PS), _iso(PE)))
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
_rec.fetch_subscription = _fake_fetch(_sub_snapshot("trialing", _iso(PS), _iso(PE)))
r = _rec.reconcile_user(uid2)
check("R5 trialing: credits 零变 / membership 不被写成 active",
      spendable_balance(uid2) == _bal5 and _plan_row(uid2) is None, f"r={r}")

# ═══ R6 — past_due ═══
u3, uid3 = _new_user("rc3")
pid3, _, sub3 = _seed_plan_order(uid3)
with db() as con:
    orders_repo.upsert_membership(con, uid3, pid3, sub3, "past_due",
                                  started_at=_ts, renews_at=PE)
_bal6 = spendable_balance(uid3)
_rec.fetch_subscription = _fake_fetch(_sub_snapshot("past_due", _iso(PS), _iso(PE)))
r = _rec.reconcile_user(uid3)
pr = _plan_row(uid3)
check("R6 past_due: 不 grant / membership 仍 past_due (reconcile 不强行 active)",
      spendable_balance(uid3) == _bal6 and pr[0] == "past_due", f"r={r} plan={pr}")

# ═══ R7 — scheduled_cancel ═══
u4, uid4 = _new_user("rc7")
pid4, _, sub4 = _seed_plan_order(uid4)
_rec.fetch_subscription = _fake_fetch(_sub_snapshot("scheduled_cancel", _iso(PS), _iso(PE)))
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
    _rec.fetch_subscription = _fake_fetch(_sub_snapshot(st, _iso(PS), _iso(PE)))
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
_rec_src = open(_os.path.join(_REPO, "core", "economy", "reconcile.py"), encoding="utf-8").read()
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
_sub_src = open(_os.path.join(_REPO, "core", "economy", "subscription.py"), encoding="utf-8").read()
_paid_blk = _sub_src.split("def apply_paid_period")[1].split("\ndef ")[0]
_ev_blk = _sub_src.split("def _ev_paid")[1].split("\ndef ")[0]
check("SG7 _ev_paid 与 reconcile 共用同一 helper",
      "apply_paid_period" in _ev_blk)
check("SG8 idem key 仍为 plan:{uid}:{period_start}",
      'f"plan:{order[\'user_id\']}:{period_start}"' in _paid_blk)
check("SG9 grant 在 upsert 前 (顺序保持)",
      _paid_blk.index("grant(") < _paid_blk.index("upsert_membership"))
_auth_src = open(_os.path.join(_REPO, "app", "routes", "auth_route.py"), encoding="utf-8").read()
check("SG10 _login_maintenance 仍调用 reconcile_user",
      "reconcile_user" in _auth_src)

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
