#!/usr/bin/env python3
"""Topup route-contract canonical: membership actions under /api/topup/*.

HTTP boundary regression: real ASGI app, real routing table, no direct
route-function calls. Guards:
  - POST /api/topup/subscribe   reaches the subscription handler
  - POST /api/topup/plan-upgrade   reaches the upgrade handler
  - POST /api/topup/plan-downgrade reaches the downgrade handler
  - POST /api/topup/checkout (credits top-up) does not regress
  - old bare paths (/subscribe, /plan-upgrade, /plan-downgrade) are 404

Creem is isolated by module-level fakes (same pattern as
test_membership_governance / test_payment_tiers). "Reaches the handler"
is proven by handler-specific outcomes, not just non-404: subscribe
creates a pending plan order + fake checkout URL; upgrade requires an
active paid membership (plan_change_required / no_membership proves the
handler ran its own gate); downgrade likewise proves its gate path.

fail-closed: requires FLASHSHARE_TEST_PORT + TEST_DB_URL (isolated).
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
import uuid

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
_ts = int(time.time())

# ── fixture: users + session cookie (real auth path) ────────────────────
sys.path.insert(0, _REPO)
from app.main import app  # real ASGI app with the real routing table
from app.web_auth import create_browser_session, COOKIE_NAME
import warnings as _w
with _w.catch_warnings():
    _w.simplefilter("ignore", DeprecationWarning)  # starlette prefers httpx2; httpx fallback warns
    from starlette.testclient import TestClient

_cur.execute("delete from packages where code='tc_pkg'")
_cur.execute("delete from users where username like 'tc%'")
def _new_user(prefix):
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", _ts, _ts))
    return u, _cur.fetchone()[0]

u1, uid1 = _new_user("tc1")
u2, uid2 = _new_user("tc2")
u3, uid3 = _new_user("tc3")
u4, uid4 = _new_user("tc4")
sess1 = create_browser_session(uid1)
sess2 = create_browser_session(uid2)
sess3 = create_browser_session(uid3)
sess4 = create_browser_session(uid4)

# ── Creem isolation (module-level fakes; no network) ────────────────────
from economy import creem as _creem
_creem_calls = {"create_sub": 0, "create_credits": 0}
def _fake_sub_checkout(order_id, plan, user):
    _creem_calls["create_sub"] += 1
    return {"ok": True, "checkout_url": f"https://creem.test/c/{order_id}",
            "checkout_id": f"ch_tc_{order_id}"}
def _fake_credits_checkout(order_id, cents, user):
    _creem_calls["create_credits"] += 1
    return {"ok": True, "checkout_url": f"https://creem.test/t/{order_id}",
            "checkout_id": f"ch_tc_{order_id}"}
_orig = (_creem.create_subscription_checkout, _creem.create_credits_checkout,
         _creem.creem_enabled, _creem.credits_ready, _creem.CREEM_CREDITS_CURRENCY)
_creem.create_subscription_checkout = _fake_sub_checkout
_creem.create_credits_checkout = _fake_credits_checkout
_creem.creem_enabled = lambda: True
_creem.credits_ready = lambda: True
_creem.CREEM_CREDITS_CURRENCY = "USD"  # checkout handler compares pkg currency to this constant

# sellable plan fixture: bind creem_product_id on the seeded plus plan.
# Save the original value first — restore exactly what was there, never
# assume NULL (a future non-NULL binding must survive this test).
_cur.execute("select creem_product_id, price_cents from plans where code='plus'")
_plus_orig_pid, _plus_orig_price = _cur.fetchone()
# sellable 合同 = active ∧ price>0 ∧ product 绑定; fixture 使 plus 真可售
_cur.execute("update plans set creem_product_id='price_tc_plus', price_cents=500 where code='plus'")
from governance.plan_catalog import validate_config as _vc
_vc()  # fixture 直写行后刷新进程内目录缓存

client = TestClient(app)

_ps_seq = [0]
def _post(path, body, cookie):
    r = client.post(path, json=body, cookies={COOKIE_NAME: cookie},
                    follow_redirects=False)
    try:
        d = r.json()
    except Exception:
        d = {"_raw": r.text[:120]}
    return r.status_code, d

# ═══ 1. subscribe reaches the subscription handler ═══
st, d = _post("/api/topup/subscribe", {"plan_id": None}, sess1)  # None → int() fails → handler's own 422
check("TR1 /api/topup/subscribe 不再404 (进入handler)",
      st in (200, 409, 422) and d.get("error") != "not_found", f"st={st} d={d}")
# valid plan → handler creates pending plan order + fake checkout
_cur.execute("select id from plans where code='plus'")
plus_id = _cur.fetchone()[0]
st, d = _post("/api/topup/subscribe", {"plan_id": plus_id}, sess1)
_cur.execute("select count(*) from orders where user_id=%s and kind='plan'", (uid1,))
_n_plan_orders = _cur.fetchone()[0]
check("TR2 subscribe 真入订阅handler (plan order 建立 + checkout_url 返回)",
      st == 200 and d.get("checkout_url", "").startswith("https://creem.test/")
      and _n_plan_orders == 1 and _creem_calls["create_sub"] == 1, f"st={st} d={d} n={_n_plan_orders}")

# ═══ 2. plan-upgrade reaches the upgrade handler ═══
st, d = _post("/api/topup/plan-upgrade", {"plan": "pro"}, sess2)
check("TR3 /api/topup/plan-upgrade 不再404 (进入handler)",
      st in (200, 409, 422) and d.get("error") != "not_found", f"st={st} d={d}")
check("TR4 upgrade 真入handler (无会员 → no_active_membership 自有门)",
      st in (409, 422) and d.get("error") in ("no_active_membership", "no_membership",
                                              "plan_change_required", "not_on_paid_tier", "bad_plan"), f"st={st} d={d}")

# ═══ 3. plan-downgrade reaches the downgrade handler ═══
st, d = _post("/api/topup/plan-downgrade", {"plan": "plus"}, sess3)
check("TR5 /api/topup/plan-downgrade 不再404 (进入handler)",
      st in (200, 409, 422) and d.get("error") != "not_found", f"st={st} d={d}")
check("TR6 downgrade 真入handler (无会员 → no_active_membership 自有拒绝)",
      st in (409, 422) and d.get("error") in ("no_active_membership", "no_membership",
                                              "plan_change_required", "not_on_paid_tier", "bad_plan"), f"st={st} d={d}")

# ═══ 4. credits checkout (existing) does not regress ═══
_cur.execute(
    "insert into packages (code, name, credits, price_cents, currency, active, created_at, updated_at)"
    " values ('tc_pkg', 'TC Pkg', 100, 500, 'USD', TRUE, %s, %s)"
    " on conflict (code) do update set active=TRUE returning id", (_ts, _ts))
_pkg_id = _cur.fetchone()[0]
st, d = _post("/api/topup/checkout", {"package_id": _pkg_id}, sess4)
check("TR7 /api/topup/checkout 积分充值不回归 (200 + checkout_url)",
      st == 200 and d.get("checkout_url", "").startswith("https://creem.test/")
      and _creem_calls["create_credits"] == 1, f"st={st} d={d}")

# ═══ 5. old bare paths are gone ═══
# 全局 404 handler 对非 API 404 静默 302 回首页 (既有行为), 故未命中路由
# 表现 302 而非 404; /api/* 前缀例外直接 404。两者都证明"路由表无此路径"。
for old in ("/subscribe", "/plan-upgrade", "/plan-downgrade"):
    st, d = _post(old, {}, sess1)
    loc = client.post(old, json={}, cookies={COOKIE_NAME: sess1},
                      follow_redirects=False).headers.get("location", "")
    check(f"TR8 旧裸路径 {old} 已退役 (404/405/全局302)",
          st in (404, 405) or (st == 302 and loc == "/"), f"st={st} loc={loc}")

# ── restore + cleanup ───────────────────────────────────────────────────
(_creem.create_subscription_checkout, _creem.create_credits_checkout,
 _creem.creem_enabled, _creem.credits_ready, _creem.CREEM_CREDITS_CURRENCY) = _orig
_cur.execute("update plans set creem_product_id=%s, price_cents=%s where code='plus'",
             (_plus_orig_pid, _plus_orig_price))
_cur.execute("delete from packages where code='tc_pkg'")
_cur.execute("delete from users where username like 'tc%'")

print()
print("FAIL: " + (", ".join(FAIL) if FAIL else "无 — 全绿"))
_sys.exit(1 if FAIL else 0)
