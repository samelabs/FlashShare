#!/usr/bin/env python3
"""State-schema invariant canonical — one field, one domain, one writer.

Behavior tests over the real routes + real DB constraints (fixtures
cleaned; consecutive runs on one TEST_DB stay green):
  Plan period_days: create/update reject <=0 (422), legal values persist
  verbatim, direct SQL <=0 hits the DB CHECK, Tier no longer rewrites 0
  to 30; numeric CHECKs reject negative values at the DB level.
  Package: create/update both reject credits<=0; price=0 stays legal.
  Pricing: writer rejects negative cost; DB rejects negative cost; 0 ok.
  tool_calls: status domain + cost_snapshot>=0 + running/finished_at
  time semantics at the DB level; existing claim/finalize writers stay
  legal.
  Withdrawals: status domain at DB level; pending/approved/rejected/paid
  rows all legal.
  Orders: negative amount / negative snapshot rejected; invalid status
  rejected; existing statuses legal.
  Plan invalid value → 422 with ZERO Creem call / ZERO audit / ZERO
  mutation (provider ordering preserved).
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

import time as _time
ts = int(_time.time())
_u = "ss" + _os.urandom(4).hex()

from database import db

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)

op = _mkuser(_u + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess = create_browser_session(op)

# Creem isolation (mirror fakes — any PATCH is a failure signal for
# invalid-value tests)
from economy import creem as _creem
_PATCH_LOG = []
_MIRROR = {"prod_ss1": {"price": 999, "currency": "USD",
                        "billing_type": "recurring",
                        "billing_period": "every-month"}}
_creem.CREEM_API_KEY = "test-key"
_orig_fp = _creem._fetch_product
_orig_patch = _creem._patch
_creem._fetch_product = lambda pid: dict(_MIRROR[pid]) if pid in _MIRROR else None
def _spy_patch(path, payload):
    _PATCH_LOG.append((path, payload))
    return {"object": "product", "id": path.rsplit("/", 1)[-1],
            "price": payload["price"], "currency": payload["currency"],
            "billing_type": "recurring", "billing_period": "every-month"}
_creem._patch = _spy_patch

def _db_rejects(sql, params=None):
    try:
        with db() as con:
            con.execute(sql, params or ())
        return False
    except Exception:
        return True

# ══════════ A. Plan period_days 三层统一 ══════════
def _so():
    with db() as con:
        return int(con.execute(
            "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p"
            " WHERE p.code <> 'free'").fetchone()["so"])

plan_ok = None
r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"ssplus_{_u}", "name": "SS Plus", "monthly_credits": 100,
    "price_cents": 999, "period_days": 45, "sort_order": _so(),
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
plan_ok = r.json().get("plan", {}).get("id")
check("A1 create 合法 period_days=45 原值落库",
      r.status_code == 200 and plan_ok is not None, f"{r.status_code}")
with db() as con:
    pd = con.execute("select period_days from plans where id=%s",
                     (plan_ok,)).fetchone()["period_days"]
check("A1b DB period_days == 45 (无 30 改写)", int(pd) == 45, str(pd))

r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"sszero_{_u}", "name": "SS Zero", "monthly_credits": 100,
    "price_cents": 999, "period_days": 0, "sort_order": _so() + 1,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
check("A2 create period_days=0 → 422 (不再静默 30)",
      r.status_code == 422, str(r.status_code))

r = client.patch(f"/samelabs/commerce/products/plans/{plan_ok}", json={"period_days": 0},
                 cookies={COOKIE_NAME: op_sess})
check("A3 update period_days=0 → 422",
      r.status_code == 422, str(r.status_code))
r = client.patch(f"/samelabs/commerce/products/plans/{plan_ok}", json={"period_days": 15},
                 cookies={COOKIE_NAME: op_sess})
check("A4 update 合法 period_days=15 原值落库", r.status_code == 200)
with db() as con:
    pd = con.execute("select period_days from plans where id=%s",
                     (plan_ok,)).fetchone()["period_days"]
check("A4b DB period_days == 15", int(pd) == 15, str(pd))

check("A5 direct SQL period_days=0 → DB CHECK reject",
      _db_rejects("update plans set period_days=0 where id=%s", (plan_ok,)))

# Tier read-model: DB 行事实直读 (构造 period_days=31 行, Tier 必须报 31)
with db() as con:
    con.execute(
        "insert into plans (code, name, monthly_credits, period_days, period,"
        " price_cents, currency, sort_order, active, ingress_files_24h,"
        " ingress_bytes_24h, expiry_days, download_limits, created_at,"
        " updated_at) values (%s, 'SS Tier', 50, 31, 'monthly', 499, 'USD',"
        " %s, TRUE, 100, 104857600, ARRAY[]::INTEGER[], ARRAY[]::INTEGER[],"
        " %s, %s)",
        (f"sstier_{_u}", _so() + 2, ts, ts))
from governance.plan_catalog import by_code, catalog
catalog(refresh=True)   # 目录缓存刷新 (生产 route 写后同机制)
tier = by_code(f"sstier_{_u}")
check("A6 Tier 读 period_days=31 (无 or-30 fallback)",
      tier is not None and tier.period_days == 31,
      str(tier and tier.period_days))

check("A7 monthly_credits 负数 direct SQL reject",
      _db_rejects("update plans set monthly_credits=-1 where id=%s", (plan_ok,)))
check("A8 price_cents 负数 direct SQL reject",
      _db_rejects("update plans set price_cents=-1 where id=%s", (plan_ok,)))
check("A9 ingress_files 负数 direct SQL reject",
      _db_rejects("update plans set ingress_files_24h=-1 where id=%s", (plan_ok,)))
check("A10 sort_order 负数 direct SQL reject",
      _db_rejects("update plans set sort_order=-1 where id=%s", (plan_ok,)))

# A11: invalid Plan value → 422 + ZERO Creem + ZERO audit + ZERO mutation
_PATCH_LOG.clear()
_n_aud = None
with db() as con:
    _n_aud = int(con.execute("select count(*) c from operator_audit"
                             ).fetchone()["c"])
r = client.post("/samelabs/commerce/products/plans", json={
    "code": f"ssbad_{_u}", "name": "SS Bad", "monthly_credits": 100,
    "price_cents": 999, "period_days": 0, "creem_product_id": "prod_ss1",
    "sort_order": _so() + 3, "ingress_files_24h": 500,
    "ingress_bytes_24h": 524288000, "expiry_days": [7],
    "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
with db() as con:
    _n_aud2 = int(con.execute("select count(*) c from operator_audit"
                              ).fetchone()["c"])
    _row = con.execute("select 1 from plans where code=%s",
                       (f"ssbad_{_u}",)).fetchone()
check("A11 invalid plan (period 0, 绑品) → 422 + 零 Creem PATCH + 零 audit + 零落库",
      r.status_code == 422 and not _PATCH_LOG
      and _n_aud2 == _n_aud and _row is None,
      f"{r.status_code} patches={len(_PATCH_LOG)} aud={_n_aud2-_n_aud}")

# ══════════ B. Package writer 一致性 ══════════
# credits 4990 @ 499 = exact commercial-floor boundary (credits <= price*10)
r = client.post("/samelabs/commerce/products/packages", json={
    "code": f"sspkg_{_u}", "name": "SS Pkg", "credits": 4990,
    "price_cents": 499,
}, cookies={COOKIE_NAME: op_sess})
pkg_id = r.json().get("package", {}).get("id")
check("B1 package create 合法", r.status_code == 200 and pkg_id is not None,
      str(r.status_code))
r = client.post("/samelabs/commerce/products/packages", json={
    "code": f"sspkg0_{_u}", "name": "SS Zero", "credits": 0, "price": 499,
}, cookies={COOKIE_NAME: op_sess})
check("B2 create credits=0 → 422", r.status_code == 422, str(r.status_code))
r = client.patch(f"/samelabs/commerce/products/packages/{pkg_id}", json={"credits": 0},
                 cookies={COOKIE_NAME: op_sess})
check("B3 update credits=0 → 422 (与 create 一致)", r.status_code == 422,
      str(r.status_code))
check("B4 direct SQL credits=0 → DB CHECK reject",
      _db_rejects("update packages set credits=0 where id=%s", (pkg_id,)))
check("B5 direct SQL price_cents<0 → DB CHECK reject",
      _db_rejects("update packages set price_cents=-1 where id=%s", (pkg_id,)))
# commercial price floor: a package with Credits cannot be priced at $0
# (credits <= price_cents*10 → credits>0 forces price>0)
r = client.patch(f"/samelabs/commerce/products/packages/{pkg_id}", json={"price_cents": 0},
                 cookies={COOKIE_NAME: op_sess})
check("B6 price=0 → 422 credits_per_usd_exceeded (floor 关闭免费 package)",
      r.status_code == 422 and r.json().get("error") == "credits_per_usd_exceeded",
      str(r.status_code))

# ══════════ C. Pricing 不变量 ══════════
from governance.pricing import set_pricing
import json as _json
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, visibility,"
        " enabled, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', 'produce', %s, 'wand',"
        " 'public', TRUE, %s, %s)",
        (f"sstool_{_u}", f"sstool_{_u}", _json.dumps({"en": "SS"}), ts, ts))
try:
    set_pricing(f"sstool_{_u}", -5, True)
    c1 = False
except ValueError:
    c1 = True
check("C1 set_pricing 负 cost → ValueError", c1)
check("C2 direct SQL 负 cost → DB CHECK reject",
      _db_rejects("insert into pricing (tool_id, cost, enabled, updated_at)"
                  " values (%s, -1, TRUE, %s)", (f"sstool2_{_u}", ts)))
r = client.post(f"/samelabs/tools/sstool_{_u}/pricing",
                json={"cost": 0, "enabled": True},
                cookies={COOKIE_NAME: op_sess})
check("C3 cost=0 免费语义仍合法", r.status_code == 200, str(r.status_code))

# ══════════ D. tool_calls DB 不变量 ══════════
tu = _mkuser(_u + "tc")
with db() as con:
    anchor = con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, commission_rate_snapshot, invocation_hash,"
        " started_at) values (%s, %s, %s, 'running', 10, 30, %s, %s)"
        " returning id",
        (f"cr_{_u}", tu, f"sstool_{_u}", "1" * 64, ts)).fetchone()
call_id = int(anchor["id"])
check("D1 invalid status direct SQL → reject",
      _db_rejects("update tool_calls set status='bogus' where id=%s",
                  (call_id,)))
check("D2 negative cost_snapshot → reject",
      _db_rejects("update tool_calls set cost_snapshot=-1 where id=%s",
                  (call_id,)))
check("D3 running + finished_at non-null → reject",
      _db_rejects("update tool_calls set finished_at=%s where id=%s",
                  (ts, call_id)))
from repo.ledger import finalize_call_ok, finalize_call_failed
with db() as con:
    finalize_call_failed(con, call_id, "failed", "test", ts)
check("D4 running → failed (finished_at 写入) 现有 writer 合法", True)
check("D5 terminal + 清空 finished_at → reject",
      _db_rejects("update tool_calls set finished_at=NULL where id=%s",
                  (call_id,)))
with db() as con:
    a2 = con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, commission_rate_snapshot, invocation_hash,"
        " started_at) values (%s, %s, %s, 'running', 0, 30, %s, %s)"
        " returning id",
        (f"cr2_{_u}", tu, f"sstool_{_u}", "2" * 64, ts)).fetchone()
with db() as con:
    finalize_call_ok(con, int(a2["id"]), "pub", "{}", ts)
check("D6 running → ok 现有 writer 合法", True)
check("D7 insufficient 终态合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at, finished_at) values"
          " (%s, %s, %s, 'insufficient', 0, %s, %s)",
          (f"cr3_{_u}", tu, f"sstool_{_u}", ts, ts)))
check("D8 commission snapshot -1 → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at) values"
          " (%s, %s, %s, 'running', 10, -1, %s)",
          (f"cr4_{_u}", tu, f"sstool_{_u}", ts)))
check("D8b commission snapshot 101 → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at) values"
          " (%s, %s, %s, 'running', 10, 101, %s)",
          (f"cr5_{_u}", tu, f"sstool_{_u}", ts)))
check("D9 commission snapshot 0/100 边界合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, invocation_hash,"
          " started_at) values (%s, %s, %s, 'running', 10, 0, %s, %s)",
          (f"cr6_{_u}", tu, f"sstool_{_u}", "3" * 64, ts))
      and not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, invocation_hash,"
          " started_at) values (%s, %s, %s, 'running', 10, 100, %s, %s)",
          (f"cr7_{_u}", tu, f"sstool_{_u}", "4" * 64, ts)))
check("D10 running + NULL commission snapshot → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at) values (%s, %s, %s, 'running', 10, %s)",
          (f"cr8_{_u}", tu, f"sstool_{_u}", ts)))
check("D11 historical terminal (failed) NULL snapshot 合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at, finished_at) values"
          " (%s, %s, %s, 'failed', 10, %s, %s)",
          (f"cr9_{_u}", tu, f"sstool_{_u}", ts, ts)))
check("D12 running + NULL invocation_hash → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at) values"
          " (%s, %s, %s, 'running', 10, 30, %s)",
          (f"cr10_{_u}", tu, f"sstool_{_u}", ts)))
check("D12b ok + NULL invocation_hash → reject",
      _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, commission_rate_snapshot, started_at,"
          " finished_at, result_public_id, result_json) values"
          " (%s, %s, %s, 'ok', 10, 30, %s, %s, 'pub', '{}')",
          (f"cr11_{_u}", tu, f"sstool_{_u}", ts, ts)))
check("D13 failed + NULL hash (historical) 合法",
      not _db_rejects(
          "insert into tool_calls (client_ref, user_id, tool_id, status,"
          " cost_snapshot, started_at, finished_at) values"
          " (%s, %s, %s, 'failed', 10, %s, %s)",
          (f"cr12_{_u}", tu, f"sstool_{_u}", ts, ts)))

# ══════════ E. withdrawals 状态域 ══════════
wu = _mkuser(_u + "wd")
with db() as con:
    con.execute(
        "insert into ledger (user_id, delta, balance_after, kind, created_at)"
        " values (%s, 20000, 20000, 'provider_earn', %s)", (wu, ts))
    w1 = con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot)"
        " values (%s, 100, 'pending', %s, 'USDT-TRC20:ssi') returning id",
        (wu, ts)).fetchone()
check("E1 pending 合法 (bare pending, review/paid fields NULL)", w1 is not None)
# S4 per-status CHECKs: approved/rejected need review fields; paid needs
# review+paid fields+non-empty payout_ref; bare inserts must REJECT.
for st in ("approved", "rejected", "paid"):
    check(f"E2 bare {st} (no review fields) → DB reject",
          _db_rejects("insert into withdrawals (user_id, amount, status,"
                      " created_at, payout_channel_snapshot)"
                      " values (%s, 100, %s, %s, 'USDT-TRC20:ssi')",
                      (wu, st, ts)))
with db() as con:
    con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot, reviewed_at, reviewed_by)"
        " values (%s, 100, 'rejected', %s, 'USDT-TRC20:ssi', %s, %s)",
        (wu, ts, ts, wu))
check("E2b rejected WITH review fields 合法", True)
with db() as con:
    con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot, reviewed_at, reviewed_by, paid_at,"
        " paid_by, payout_ref)"
        " values (%s, 100, 'paid', %s, 'USDT-TRC20:ssi', %s, %s, %s, %s,"
        " 'REF-E2c')", (wu, ts, ts, wu, ts, wu))
check("E2c paid WITH review+paid fields+ref 合法", True)
check("E3 invalid withdrawal status → DB reject",
      _db_rejects("insert into withdrawals (user_id, amount, status,"
                  " created_at, payout_channel_snapshot)"
                  " values (%s, 100, 'bogus', %s, 'USDT-TRC20:ssi')",
                  (wu, ts)))
check("E4 blank payout_channel_snapshot → DB reject",
      _db_rejects("insert into withdrawals (user_id, amount, status,"
                  " created_at, payout_channel_snapshot)"
                  " values (%s, 100, 'pending', %s, '  ')", (wu, ts)))

# ══════════ F. orders 不变量 ════════
ou = _mkuser(_u + "ord")
check("F1 negative amount_cents → reject",
      _db_rejects("insert into orders (user_id, kind, item_id, provider,"
                  " status, amount_cents, currency, created_at)"
                  " values (%s, 'package', 1, 'creem', 'pending', -1,"
                  " 'USD', %s)", (ou, ts)))
check("F2 negative credits_snapshot → reject",
      _db_rejects("insert into orders (user_id, kind, item_id, provider,"
                  " status, amount_cents, currency, credits_snapshot,"
                  " created_at) values (%s, 'package', 1, 'creem',"
                  " 'pending', 100, 'USD', -5, %s)", (ou, ts)))
check("F3 invalid order status → reject",
      _db_rejects("insert into orders (user_id, kind, item_id, provider,"
                  " status, amount_cents, currency, created_at)"
                  " values (%s, 'package', 1, 'creem', 'bogus', 100,"
                  " 'USD', %s)", (ou, ts)))
ok_statuses = True
for st in ("pending", "paid", "failed", "refunded", "disputed"):
    # canonical field combinations: snapshot>0; paid carries paid_at(+granted);
    # reversals carry refunded_at (granted reflects the prior paid fact)
    granted = st in ("paid", "refunded", "disputed")
    paid_at = ts if st in ("paid", "refunded", "disputed") else "NULL"
    refunded_at = ts if st in ("refunded", "disputed") else "NULL"
    if _db_rejects("insert into orders (user_id, kind, item_id, provider,"
                   " provider_order_id, status, amount_cents, currency,"
                   " credits_snapshot, granted, paid_at, refunded_at,"
                   " created_at) values (%s, 'package', 1, 'creem',"
                   " %s, %s, 100, 'USD', 100, %s, %s, %s, %s)",
                   (ou,
                    f"ord_f4_{st}" if st != "pending" and st != "failed" else None,
                    st, granted,
                    paid_at if paid_at != "NULL" else None,
                    refunded_at if refunded_at != "NULL" else None, ts)):
        ok_statuses = False
check("F4 五个既有 order 状态均合法 (canonical 组合)", ok_statuses)

# ═══ F5: package terminal 状态禁止 provisional ch:* identity (writer
# contract: successful payment closes on the REAL provider order id;
# ch:* is pending/failed-only) ═══
def _pkg_terminal_row(status, prov):
    # per-row UNIQUE provider id (a shared id would turn a later
    # UNIQUE violation into a fake "reject" and mask the real verdict)
    return ("insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency,"
            " credits_snapshot, granted, paid_at, refunded_at,"
            " created_at) values (%s, 'package', 1, 'creem', %s, %s,"
            " 100, 'USD', 100, TRUE, %s, %s, %s)",
            (ou, f"{prov}_{status}", status, ts,
             ts if status in ("refunded", "disputed") else None, ts))

check("F5a package paid + provider_order_id ch:* → DB reject",
      _db_rejects(*_pkg_terminal_row("paid", "ch:chk_terminal")))
check("F5b package refunded + ch:* → DB reject (terminal 范围含 reversal)",
      _db_rejects(*_pkg_terminal_row("refunded", "ch:chk_terminal")))
check("F5c package disputed + ch:* → DB reject",
      _db_rejects(*_pkg_terminal_row("disputed", "ch:chk_terminal")))
# pending/failed + ch:* 仍合法 (provisional namespace 的唯一合法阶段;
# 独立 user 避开 one-pending-per-(user,item) partial unique)
ou2 = _mkuser(_u + "ord2")
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " credits_snapshot, created_at) values (%s, 'package', 1,"
        " 'creem', %s, 'pending', 100, 'USD', 100, %s)",
        (ou2, f"ch:pend_{_u}", ts))
check("F5d pending + ch:* 仍合法 (provisional 阶段)",
      not _db_rejects("update orders set status='failed' where"
                      " provider_order_id=%s", (f"ch:pend_{_u}",)))
# real canonical provider id 的 paid package 仍合法
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " credits_snapshot, granted, paid_at, created_at)"
        " values (%s, 'package', 1, 'creem', %s, 'paid', 100, 'USD',"
        " 100, TRUE, %s, %s)",
        (ou, f"ord_real_{_u}", ts, ts))
check("F5e paid + real canonical provider id 仍合法",
      not _db_rejects("update orders set refunded_at=%s where"
                      " provider_order_id=%s", (None, f"ord_real_{_u}")))

# ══════════ cleanup (FK-safe, prefix-scoped only) ══════════
# 分立事务: 每步独立提交, 单点失败不回滚其余清理
with db() as con:
    for uid in (tu, wu, ou, ou2, op):
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
with db() as con:
    con.execute("delete from tool_calls where user_id=%s", (tu,))
with db() as con:
    con.execute("delete from ledger where user_id=%s", (wu,))
    con.execute("delete from withdrawals where user_id=%s", (wu,))
with db() as con:
    con.execute("delete from orders where user_id in (%s, %s)", (ou, ou2))
with db() as con:
    con.execute("delete from pricing where tool_id like %s", (f"sstool%_{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"sstool%_{_u}%",))
with db() as con:
    con.execute("delete from plans where code like %s", (f"ss%_{_u}",))
    con.execute("delete from packages where code like %s", (f"sspkg%_{_u}",))
for uid in (tu, wu, ou, ou2, op):
    with db() as con:
        con.execute("delete from sessions where user_id=%s", (uid,))
        con.execute("delete from api_keys where user_id=%s", (uid,))
        con.execute("delete from users where id=%s", (uid,))
print("cleanup: state-schema fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
