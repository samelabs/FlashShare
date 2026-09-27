#!/usr/bin/env python3
"""Operator audit canonical — mutation → durable append-only audit fact.

Proves over real routes/functions (isolated DB, browser-session operator):
  authority frozen (session+samelabs gate; no Bearer; no status route);
  pricing / plan / package / presentation / credits / withdrawal / remote
  mutations each land exactly one allowlisted audit fact in the SAME
  transaction (forced audit failure → full rollback; Creem failure → zero
  committed audit; audit-before-Creem ordering); credits replay = one
  ledger + one audit; append-only (no UPDATE/DELETE writer); snapshots
  carry allowlist fields only — never URL/endpoint_json/auth/Vault.
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
from tests.support.cleanup import purge_users  # noqa: E402

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

from database import db

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

_u = "oa" + _os.urandom(4).hex()

from app.web_auth import create_browser_session, COOKIE_NAME
from core.identity.apikeys import create_key
from fastapi.testclient import TestClient
from app.main import app as _app
# forced-failure tests raise inside routes; TestClient (httpx) re-raises
# app exceptions — observe them as plain 500s by calling through a
# request-scoped guard
def _post(path, **kw):
    try:
        return client.post(path, **kw)
    except Exception:
        class _R:
            status_code = 500
            def json(self):
                return {"ok": False, "error": "forced"}
        return _R()

def _patch(path, **kw):
    try:
        return client.patch(path, **kw)
    except Exception:
        class _R:
            status_code = 500
            def json(self):
                return {"ok": False, "error": "forced"}
        return _R()

client = TestClient(_app)

op = _mkuser(_u + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
op_sess = create_browser_session(op)
plain = _mkuser(_u + "plain")   # non-operator browser session
plain_sess = create_browser_session(plain)
op_token = create_key(op, "oa-test").get("key")  # operator Bearer

# ══════════ A. authority frozen ══════════
r = _post("/samelabs/tools/x/pricing", json={"cost": 1},
                cookies={COOKIE_NAME: plain_sess}, follow_redirects=False)
check("A1 非 operator browser session → mutation gate 拒绝 (302 redirect)",
      r.status_code in (301, 302, 401, 403, 404), str(r.status_code))
r = _post("/samelabs/tools/x/pricing", json={"cost": 1},
                headers={"Authorization": f"Bearer {op_token}"},
                follow_redirects=False)
check("A2 operator Bearer token → /samelabs 拒绝 (session-only, 302)",
      r.status_code in (301, 302, 401, 403, 404), str(r.status_code))
import inspect as _inspect
import app.routes.samelabs as _sam_mod
# S4: the monolith became a package — inspect the concatenation of its
# route submodules (gate/context live in common.py; mutations in the rest)
import pathlib as _pl
_src = "\n".join(
    _pl.Path(_sam_mod.__path__[0], f + ".py").read_text(encoding="utf-8")
    for f in ("common", "overview", "users", "tools", "commerce",
              "economy", "audit"))
check("A3 无 users.status mutation route",
      "users set status" not in _src.replace("update users set status='samelabs' where id=%s", "")
      and "status='samelabs'" not in _src.replace(
          "update users set status='samelabs' where id=%s", ""))

# ══════════ helpers ══════════
from repo import operator_audit as _aud

def _audit_rows(action=None, target=None):
    """audit rows scoped to THIS run's entities (target_id filter) so the
    suite never depends on a pristine table."""
    with db() as con:
        if action and target:
            rows = con.execute(
                "select * from operator_audit where action=%s and target_id=%s"
                " order by id", (action, target)).fetchall()
        elif action:
            rows = con.execute(
                "select * from operator_audit where action=%s and target_id"
                " like %s order by id",
                (action, f"%{_u}%")).fetchall()
        else:
            rows = con.execute(
                "select * from operator_audit where target_id like %s"
                " order by id", (f"%{_u}%",)).fetchall()
    return [dict(r) for r in rows]

def _no_secrets(rows):
    import json as _j
    bad = []
    for r in rows:
        for col in ("before_json", "after_json"):
            v = r.get(col)
            if not v:
                continue
            s = _j.dumps(v) if not isinstance(v, str) else v
            for marker in ("password", "auth_token", "auth_header",
                           "endpoint_json", "vault", "http://", "https://",
                           "fsk_", "apikey", "api_key"):
                if marker in s.lower():
                    bad.append((r["id"], col, marker))
    return bad

# mirror fakes for plan routes (Creem isolated)
from economy import creem as _creem
_MIRROR = {f"prod_oa1_{_u}": {"price": 999, "currency": "USD",
                        "billing_type": "recurring",
                        "billing_period": "every-month"},
           f"prod_oa2_{_u}": {"price": 1999, "currency": "USD",
                        "billing_type": "recurring",
                        "billing_period": "every-month"}}
_orig_fp = _creem._fetch_product
_orig_patch = _creem._patch
_creem.CREEM_API_KEY = "test-key"
def _fp(pid):
    d = _MIRROR.get(pid)
    return dict(d) if d else None
def _pt(path, payload):
    pid = path.rsplit("/", 1)[-1]
    d = _MIRROR[pid]
    _MIRROR[pid] = {**d, "price": payload["price"],
                    "currency": payload["currency"]}
    _PATCH_LOG.append((pid, payload["price"]))
    return {"object": "product", "id": pid, "price": payload["price"],
            "currency": payload["currency"], "billing_type": "recurring",
            "billing_period": "every-month"}
_PATCH_LOG = []
_creem._fetch_product = _fp
_creem._patch = _pt

def _audit_fail_on(marker_action):
    """Force record() to raise for one action (simulated insert failure)."""
    _orig = _aud.record
    def _boom(con, actor, action, *a, **k):
        if action == marker_action:
            raise RuntimeError("forced audit insert failure")
        return _orig(con, actor, action, *a, **k)
    return _orig, _boom

# ══════════ B. pricing ══════════
# seed a platform tool row to point pricing at
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, visibility,"
        " enabled, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', 'produce', '{\"en\":\"OA\"}',"
        " 'wand', 'public', TRUE, %s, %s)",
        (f"oatool_{_u}", f"oatool_{_u}", ts, ts))
r = _post(f"/samelabs/tools/oatool_{_u}/pricing",
                json={"cost": 150, "enabled": True},
                cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("pricing.update", f"oatool_{_u}")
check("B1 pricing mutation → exactly one audit / actor / target / before=disabled / after",
      r.status_code == 200 and len(rows) == 1
      and rows[0]["actor_user_id"] == op
      and rows[0]["target_type"] == "pricing"
      and rows[0]["target_id"] == f"oatool_{_u}"
      and rows[0]["before_json"] is None      # no prior pricing row
      and rows[0]["after_json"]["cost"] == 150,
      f"{r.status_code} rows={len(rows)}")
# second update → before carries prior state
r = _post(f"/samelabs/tools/oatool_{_u}/pricing",
                json={"cost": 200, "enabled": True},
                cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("pricing.update", f"oatool_{_u}")
check("B2 second pricing update → before=150 / after=200",
      len(rows) == 2 and rows[1]["before_json"]["cost"] == 150
      and rows[1]["after_json"]["cost"] == 200)

# audit failure → pricing rollback
_orig_rec, _aud.record = _audit_fail_on("pricing.update")
r = _post(f"/samelabs/tools/oatool_{_u}/pricing",
                json={"cost": 999, "enabled": True},
                cookies={COOKIE_NAME: op_sess})
_aud.record = _orig_rec
with db() as con:
    cur = con.execute("select cost from pricing where tool_id=%s",
                      (f"oatool_{_u}",)).fetchone()
rows = _audit_rows("pricing.update", f"oatool_{_u}")
check("B3 forced audit failure → pricing rollback (cost 仍 200) + zero new audit",
      r.status_code == 500 and int(cur["cost"]) == 200 and len(rows) == 2,
      f"{r.status_code} cost={cur['cost'] if cur else None} rows={len(rows)}")

# ══════════ C. plan create/update ══════════
with db() as con:
    _so = int(con.execute(
        "SELECT COALESCE(MAX(p.sort_order),0)+1 AS so FROM plans p"
        " WHERE p.code <> 'free'").fetchone()["so"])
r = _post("/samelabs/commerce/products/plans", json={
    "code": f"oaplus_{_u}", "name": "OA Plus", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": f"prod_oa1_{_u}", "sort_order": _so,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
plan_id = r.json().get("plan", {}).get("id")
rows = _audit_rows("plan.create", str(plan_id))
check("C1 plan create → audit before=NULL / after=safe plan snapshot",
      r.status_code == 200 and len(rows) == 1 and plan_id is not None
      and rows[0]["before_json"] is None
      and rows[0]["after_json"]["code"] == f"oaplus_{_u}"
      and rows[0]["after_json"]["price_cents"] == 999
      and rows[0]["target_id"] == str(plan_id),
      f"{r.status_code} {rows[:1]}")

# audit insert failure on create → ZERO Creem call + rollback
_PATCH_LOG.clear()
_aud.record = _audit_fail_on("plan.create")[1]
r = _post("/samelabs/commerce/products/plans", json={
    "code": f"oax_{_u}", "name": "OA X", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": f"prod_oa2_{_u}",
    "sort_order": _so + 1, "ingress_files_24h": 500,
    "ingress_bytes_24h": 524288000, "expiry_days": [7],
    "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
_aud.record = _orig_rec
with db() as con:
    gone = con.execute("select 1 from plans where code=%s",
                       (f"oax_{_u}",)).fetchone()
    oax_audits = con.execute(
        "select count(*) c from operator_audit where action='plan.create'"
        " and after_json->>'code' = %s", (f"oax_{_u}",)).fetchone()
check("C2 audit insert failure → ZERO Creem PATCH + plan rollback + zero committed audit",
      r.status_code == 500 and not _PATCH_LOG and gone is None
      and int(oax_audits["c"]) == 0,
      f"{r.status_code} patches={_PATCH_LOG} gone={bool(gone)} oax_audits={oax_audits['c']}")

# local constraint failure (duplicate product binding) → zero audit + zero Creem
_PATCH_LOG.clear()
n_audit = len(_audit_rows())
r = _post("/samelabs/commerce/products/plans", json={
    "code": f"oay_{_u}", "name": "OA Y", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": f"prod_oa1_{_u}",
    "sort_order": _so + 2, "ingress_files_24h": 500,
    "ingress_bytes_24h": 524288000, "expiry_days": [7],
    "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
check("C3 constraint failure → 409 + zero audit + zero Creem",
      r.status_code == 409 and len(_audit_rows()) == n_audit
      and not _PATCH_LOG,
      f"{r.status_code} audits+={len(_audit_rows()) - n_audit} patches={_PATCH_LOG}")

# valid update → in-tx before/final after
r = _patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"price_cents": 1499},
                 cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("plan.update", str(plan_id))
check("C4 plan update → transaction 内 before=999 / after=1499",
      r.status_code == 200 and len(rows) == 1
      and rows[0]["before_json"]["price_cents"] == 999
      and rows[0]["after_json"]["price_cents"] == 1499,
      f"{r.status_code} {rows[:1]}")

# Creem failure → local rollback + ZERO committed audit
_aud.record = _orig_rec
def _patch_fail(path, payload):
    raise RuntimeError("provider down")
_creem._patch = _patch_fail
n_audit = len(_audit_rows())
r = _patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"price_cents": 1999},
                 cookies={COOKIE_NAME: op_sess})
_creem._patch = _pt
with db() as con:
    p = con.execute("select price_cents from plans where id=%s",
                    (plan_id,)).fetchone()
check("C5 Creem failure → 502 + local 仍 1499 + zero committed audit",
      r.status_code == 502 and int(p["price_cents"]) == 1499
      and len(_audit_rows()) == n_audit,
      f"{r.status_code} price={p['price_cents']} audits+={len(_audit_rows()) - n_audit}")

# audit-before-Creem ordering proof: audit failure during commercial update
# → rollback + ZERO Creem PATCH (audit must sit BEFORE the provider call)
_PATCH_LOG.clear()
_aud.record = _audit_fail_on("plan.update")[1]
r = _patch(f"/samelabs/commerce/products/plans/{plan_id}", json={"price_cents": 1799},
                 cookies={COOKIE_NAME: op_sess})
_aud.record = _orig_rec
check("C6 audit-before-Creem: audit failure → rollback + ZERO provider call",
      r.status_code == 500 and not _PATCH_LOG,
      f"{r.status_code} patches={_PATCH_LOG}")

# ══════════ D. credits adjust ══════════
target_user = _mkuser(_u + "tgt")
aid = f"act_{_u}1"
r = _post(f"/samelabs/users/{target_user}/credits",
          json={"amount": 500, "action_id": aid},
          cookies={COOKIE_NAME: op_sess})
with db() as con:
    lr = con.execute(
        "select id, delta, balance_after from ledger where user_id=%s"
        " and idem_key=%s",
        (target_user, f"adjust:{op}:{aid}")).fetchone()
rows = _audit_rows("credits.adjust", str(target_user))
check("D1 +adjust → ledger + audit 同事务 (delta 500 / ledger_id 正确)",
      r.status_code == 200 and lr is not None and rows
      and rows[-1]["after_json"]["delta"] == 500
      and rows[-1]["after_json"]["ledger_id"] == int(lr["id"])
      and rows[-1]["after_json"]["balance_before"] == 0
      and rows[-1]["after_json"]["balance_after"] == 500,
      f"{r.status_code} {lr and dict(lr)} {rows[-1:]}")

r = _post(f"/samelabs/users/{target_user}/credits",
          json={"amount": -200, "action_id": f"act_{_u}2"},
          cookies={COOKIE_NAME: op_sess})
with db() as con:
    lr2 = con.execute(
        "select delta, balance_after from ledger where user_id=%s"
        " and idem_key=%s",
        (target_user, f"adjust:{op}:act_{_u}2")).fetchone()
rows = _audit_rows("credits.adjust", str(target_user))
check("D2 -adjust → ledger + audit (delta -200 / 500→300)",
      r.status_code == 200 and lr2 is not None
      and rows[-1]["after_json"]["delta"] == -200
      and rows[-1]["after_json"]["balance_after"] == 300,
      f"{r.status_code} {lr2 and dict(lr2)}")

# same action_id replay → one ledger + one audit
n_ledger_pre = None
with db() as con:
    n_ledger_pre = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='adjust'",
        (target_user,)).fetchone()["c"])
r = _post(f"/samelabs/users/{target_user}/credits",
          json={"amount": 500, "action_id": aid},
          cookies={COOKIE_NAME: op_sess})
with db() as con:
    n_ledger_post = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='adjust'",
        (target_user,)).fetchone()["c"])
rows = _audit_rows("credits.adjust", str(target_user))
check("D3 same action_id replay → ledger 不增 + audit 不增",
      r.status_code == 200 and n_ledger_post == n_ledger_pre
      and len(rows) == 2,
      f"{r.status_code} ledger {n_ledger_pre}→{n_ledger_post} audits={len(rows)}")

# insufficient → zero audit zero ledger
r = _post(f"/samelabs/users/{target_user}/credits",
          json={"amount": -99999, "action_id": f"act_{_u}3"},
          cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("credits.adjust", str(target_user))
check("D4 insufficient → 409 + zero audit (仍 2)",
      r.status_code == 409 and len(rows) == 2,
      f"{r.status_code} audits={len(rows)}")

# forced audit failure → ledger rollback
_aud.record = _audit_fail_on("credits.adjust")[1]
with db() as con:
    pre_bal = con.execute(
        "select coalesce(sum(delta),0) b from ledger where user_id=%s",
        (target_user,)).fetchone()["b"]
r = _post(f"/samelabs/users/{target_user}/credits",
          json={"amount": 777, "action_id": f"act_{_u}4"},
          cookies={COOKIE_NAME: op_sess})
_aud.record = _orig_rec
with db() as con:
    post_bal = con.execute(
        "select coalesce(sum(delta),0) b from ledger where user_id=%s",
        (target_user,)).fetchone()["b"]
check("D5 forced audit failure → ledger rollback (余额不变)",
      r.status_code == 500 and int(pre_bal) == int(post_bal),
      f"{r.status_code} {pre_bal}→{post_bal}")

# ══════════ E. withdrawal ══════════
provider = _mkuser(_u + "prv")
with db() as con:
    con.execute(
        "insert into ledger (user_id, delta, balance_after, kind,"
        " created_at) values (%s, 20000, 20000, 'provider_earn', %s)",
        (provider, ts))
    wrow = con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot)"
        " values (%s, 5000, 'pending', %s, 'USDT-TRC20:e1') returning id",
        (provider, ts)).fetchone()
wid = int(wrow["id"])
r = _post(f"/samelabs/economy/withdrawals/{wid}/approve", json={},
          cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("withdrawal.approve", str(wid))
check("E1 approve → audit / before=pending / after=approved",
      r.status_code == 200 and len(rows) == 1
      and rows[0]["before_json"]["status"] == "pending"
      and rows[0]["after_json"]["status"] == "approved"
      and rows[0]["after_json"]["amount"] == 5000,
      f"{r.status_code} {rows[:1]}")

# S4 replay: approve on approved = replay success, NO duplicate audit
r = _post(f"/samelabs/economy/withdrawals/{wid}/approve", json={},
          cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("withdrawal.approve", str(wid))
check("E2 approve replay → 200 + 无重复 audit",
      r.status_code == 200 and len(rows) == 1, f"{r.status_code} {len(rows)}")

# reject path + forced audit failure rollback
with db() as con:
    w2 = con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot)"
        " values (%s, 1000, 'pending', %s, 'USDT-TRC20:e3') returning id",
        (provider, ts)).fetchone()
wid2 = int(w2["id"])
r = _post(f"/samelabs/economy/withdrawals/{wid2}/reject", json={},
          cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("withdrawal.reject", str(wid2))
check("E3 reject → audit / after=rejected",
      r.status_code == 200 and len(rows) == 1
      and rows[0]["after_json"]["status"] == "rejected",
      f"{r.status_code} {rows[:1]}")

with db() as con:
    w3 = con.execute(
        "insert into withdrawals (user_id, amount, status, created_at,"
        " payout_channel_snapshot) values (%s, 1000, 'pending', %s,"
        " 'USDT-TRC20:e4') returning id",
        (provider, ts)).fetchone()
wid3 = int(w3["id"])
_aud.record = _audit_fail_on("withdrawal.approve")[1]
from economy.earnings import approve_withdrawal as _pw
try:
    _pw(wid3, admin_user_id=op)
    forced = "no-raise"
except Exception:
    forced = "raised"
_aud.record = _orig_rec
with db() as con:
    st = con.execute("select status from withdrawals where id=%s",
                     (wid3,)).fetchone()
    led = con.execute(
        "select count(*) c from ledger where kind='withdraw'"
        " and ref_id=%s", (str(wid3),)).fetchone()
check("E4 forced audit failure → withdrawal + ledger 全 rollback",
      forced == "raised" and st["status"] == "pending"
      and int(led["c"]) == 0,
      f"{forced} {st and dict(st)} ledger={led['c']}")

# ══════════ F. remote create/delete ══════════
rt = f"oart_{_u}"
# The credential-bearing shape depends on the vault: with
# TOOL_VAULT_KEY configured it persists (sealed); without it the route
# must 503 with zero persistence. Assert BOTH behaviors explicitly —
# the unavailable case via a subprocess probe (vault removed).
r = _post("/samelabs/tools/create", json={
    "tool_id": rt, "label": "OA Remote", "url": "https://ops.example/hook",
    "timeout_s": 30, "cost": 50,
}, cookies={COOKIE_NAME: op_sess})
from secrets_vault import vault_enabled as _vault_on
_tok_expect = 200 if _vault_on() else 503
r_tok = _post("/samelabs/tools/create", json={
    "tool_id": rt + "t", "label": "OA Tok", "url": "https://ops.example/t",
    "timeout_s": 30, "auth_token": "SUPERSECRET123", "cost": 10,
}, cookies={COOKIE_NAME: op_sess})
with db() as con:
    tok_row = con.execute(
        "select endpoint_json from platform_tools where tool_key=%s",
        (rt + "t",)).fetchone()
rows = _audit_rows("remote.create", rt)
check("F1 remote create → tool + pricing + audit (credential shape per vault)",
      r.status_code == 200 and len(rows) == 1
      and rows[0]["before_json"] is None
      and rows[0]["after_json"]["tool_key"] == rt
      and rows[0]["after_json"]["cost"] == 50
      and r_tok.status_code == _tok_expect
      and (tok_row is not None if _vault_on() else tok_row is None),
      f"{r.status_code}/{r_tok.status_code} tok={tok_row} {rows[:1]}")

# forced audit failure on remote create → rollback
_aud.record = _audit_fail_on("remote.create")[1]
r = _post("/samelabs/tools/create", json={
    "tool_id": rt + "x", "label": "X", "url": "https://ops.example/x",
    "timeout_s": 30, "cost": 10,
}, cookies={COOKIE_NAME: op_sess})
_aud.record = _orig_rec
with db() as con:
    exists = con.execute(
        "select 1 from platform_tools where tool_key=%s", (rt + "x",)
    ).fetchone()
check("F2 forced audit failure → remote create rollback (行不存在)",
      r.status_code in (409, 500) and exists is None,
      f"{r.status_code} exists={bool(exists)}")

# audit JSON contains NO secrets (create carried auth_token + URL)
all_rows = _audit_rows()
bad = _no_secrets(all_rows)
check("F3 audit 快照无 endpoint_json/auth_token/auth_header/Vault/URL",
      not bad, str(bad[:3]))

# remote delete → one tx: tool + pricing disabled + audit
r = _post(f"/samelabs/tools/{rt}/delete", json={},
          cookies={COOKIE_NAME: op_sess})
with db() as con:
    gone = con.execute(
        "select 1 from platform_tools where tool_key=%s", (rt,)).fetchone()
    pr = con.execute(
        "select cost, enabled from pricing where tool_id=%s", (rt,)).fetchone()
rows = _audit_rows("remote.delete", rt)
check("F4 remote delete → tool 行删除 + pricing disabled + audit before=safe/after=NULL",
      r.status_code == 200 and gone is None and pr is not None
      and not pr["enabled"] and len(rows) == 1
      and rows[0]["before_json"]["tool_key"] == rt
      and rows[0]["after_json"] is None,
      f"{r.status_code} gone={gone is None} pricing={dict(pr) if pr else None} {rows[:1]}")

# forced audit failure on delete → tool row + pricing unchanged
import json as _json3
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, visibility,"
        " enabled, created_at, updated_at)"
        " values (%s, NULL, %s, 'http', 'produce', %s,"
        " 'wand', 'public', TRUE, %s, %s)",
        (rt + "2", rt + "2", _json3.dumps({"en": "OA2"}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 30, TRUE, %s)", (rt + "2", ts))
_aud.record = _audit_fail_on("remote.delete")[1]
r = _post(f"/samelabs/tools/{rt + '2'}/delete", json={},
          cookies={COOKIE_NAME: op_sess})
_aud.record = _orig_rec
with db() as con:
    still = con.execute(
        "select 1 from platform_tools where tool_key=%s", (rt + "2",)
    ).fetchone()
    pr2 = con.execute(
        "select cost, enabled from pricing where tool_id=%s", (rt + "2",)
    ).fetchone()
check("F5 forced audit failure → delete rollback (tool 仍在 + pricing 仍 enabled=30)",
      r.status_code in (409, 500) and still is not None
      and pr2 is not None and pr2["enabled"],
      f"{r.status_code} still={bool(still)} pricing={dict(pr2) if pr2 else None}")

# ══════════ G. append-only / read API / package coverage ══════════
# package create/update audits (credits/price_cents inside the
# commercial floor: credits <= price_cents * 10)
r = _post("/samelabs/commerce/products/packages", json={
    "code": f"oapkg_{_u}", "name": "OA Pkg", "credits": 5000,
    "price_cents": 599,
}, cookies={COOKIE_NAME: op_sess})
pkg_id = r.json().get("package", {}).get("id")
rows = _audit_rows("package.create", str(pkg_id))
check("G1 package create → audit before=NULL / after=safe",
      r.status_code == 200 and len(rows) == 1
      and rows[0]["after_json"]["credits"] == 5000,
      f"{r.status_code} {rows[:1]}")
r = _patch(f"/samelabs/commerce/products/packages/{pkg_id}",
           json={"credits": 8000, "price_cents": 999},
           cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("package.update", str(pkg_id))
check("G2 package update → before=5000 / after=8000",
      r.status_code == 200 and len(rows) == 1
      and rows[0]["before_json"]["credits"] == 5000
      and rows[0]["after_json"]["credits"] == 8000,
      f"{r.status_code} {rows[:1]}")

# presentation audit
r = _patch(f"/samelabs/tools/oatool_{_u}/presentation",
           json={"featured": True, "sort_order": 7},
           cookies={COOKIE_NAME: op_sess})
rows = _audit_rows("tool.presentation.update", f"oatool_{_u}")
check("G3 presentation → exact audit (before/after 只含 tool_key/featured/sort_order)",
      r.status_code == 200 and len(rows) == 1
      and set(rows[0]["after_json"].keys()) == {"tool_key", "featured",
                                                "sort_order"}
      and rows[0]["after_json"]["featured"] is True,
      f"{r.status_code} {rows[:1]}")

# repo read API: count (list_recent retired with the FX-2 dead-fn sweep;
# the audit log surface reads through recent_pair_rows projections)
with db() as con:
    total = _aud.count(con)
check("G4 audit count 一致 (≥8 rows recorded)",
      total >= 8, f"total={total}")

# append-only: no UPDATE/DELETE writer in production code
import subprocess as _sp
grep_upd = _sp.run(["grep", "-rn", "-E",
                    "UPDATE +operator_audit|DELETE +FROM +operator_audit",
                    "core/", "app/"], capture_output=True, text=True)
check("G5 生产代码零 UPDATE/DELETE operator_audit writer",
      not grep_upd.stdout.strip(), grep_upd.stdout[:100])
ins_writers = _sp.run(["grep", "-rn", "INSERT INTO operator_audit",
                       "core/", "app/"], capture_output=True, text=True)
check("G6 单一 INSERT writer = repo/operator_audit.py",
      ins_writers.stdout.strip().count(":") >= 1
      and all("operator_audit.py" in line for line in
              ins_writers.stdout.strip().splitlines()),
      ins_writers.stdout[:150])

# all rows carry actor_user_id (non-null)
with db() as con:
    n_no_actor = int(con.execute(
        "select count(*) c from operator_audit where actor_user_id is null"
    ).fetchone()["c"])
check("G7 所有 audit 行均有 actor_user_id", n_no_actor == 0, str(n_no_actor))

# every audit JSON key within allowlist (no secret-ish keys)
import json as _json2
offenders = []
for r_ in _audit_rows():
    allowed = _aud.TARGET_SAFE_KEYS.get(r_["target_type"])
    for col in ("before_json", "after_json"):
        v = r_.get(col)
        if v and isinstance(v, dict):
            for k in v:
                if allowed is None or k not in allowed:
                    offenders.append((r_["id"], r_["target_type"], col, k))
check("G8 全部快照键 ∈ 该 target 的 allowlist", not offenders,
      str(offenders[:3]))

# ══════════ H. concurrency serialization + target allowlists ══════════
import threading

# H1/H2: concurrent remote delete → exactly one audit; loser not_found
rt3 = f"oart3_{_u}"
r = _post("/samelabs/tools/create", json={
    "tool_id": rt3, "label": "CC", "url": "https://ops.example/cc",
    "timeout_s": 30, "cost": 20,
}, cookies={COOKIE_NAME: op_sess})
check("H1pre concurrent-delete fixture created", r.status_code == 200,
      str(r.status_code))
results = {}
barrier = threading.Barrier(2)

def _del_worker(tag):
    barrier.wait(timeout=15)
    results[tag] = _post(f"/samelabs/tools/{rt3}/delete", json={},
                         cookies={COOKIE_NAME: op_sess})

t1 = threading.Thread(target=_del_worker, args=("A",))
t2 = threading.Thread(target=_del_worker, args=("B",))
t1.start(); t2.start(); t1.join(30); t2.join(30)
with db() as con:
    row_left = con.execute(
        "select 1 from platform_tools where tool_key=%s", (rt3,)).fetchone()
    aud_n = int(con.execute(
        "select count(*) c from operator_audit where action='remote.delete'"
        " and target_id=%s", (rt3,)).fetchone()["c"])
    pr = con.execute(
        "select enabled from pricing where tool_id=%s", (rt3,)).fetchone()
sc_a, sc_b = results["A"].status_code, results["B"].status_code
one_ok = (sc_a == 200) ^ (sc_b == 200)
one_404 = (sc_a == 404) or (sc_b == 404)
check("H1 concurrent delete → row absent / pricing disabled / audit=1 / 一成一 404",
      row_left is None and not pr["enabled"] and aud_n == 1
      and one_ok and one_404,
      f"A={sc_a} B={sc_b} aud={aud_n} pr={dict(pr) if pr else None}")

# H3: concurrent pricing update → second audit before = first committed after
rt4 = f"oatool4_{_u}"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, visibility,"
        " enabled, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', 'produce', %s, 'wand',"
        " 'public', TRUE, %s, %s)",
        (rt4, rt4, _json3.dumps({"en": "CC4"}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 100, TRUE, %s)", (rt4, ts))

_entered = threading.Event()
_release = threading.Event()
import repo.catalog as _cat_mod
_orig_tfu = _cat_mod.tool_row_for_update
_gate_state = {"armed": False, "stalled": False}

def _gated_tfu(con, tool_key):
    if tool_key == rt4 and _gate_state["armed"] \
            and not _gate_state["stalled"]:
        # FIRST armed caller (B) stalls AFTER entering the route but
        # BEFORE taking the row lock — its before-read would be stale
        # without lock-first ordering. A (next caller) proceeds.
        _gate_state["stalled"] = True
        _entered.set()
        _release.wait(timeout=30)
    return _orig_tfu(con, tool_key)

_cat_mod.tool_row_for_update = _gated_tfu

res_b = {}
def _pricing_worker_b():
    res_b["r"] = _post(f"/samelabs/tools/{rt4}/pricing",
                       json={"cost": 300, "enabled": True},
                       cookies={COOKIE_NAME: op_sess})

_gate_state["armed"] = True
tb = threading.Thread(target=_pricing_worker_b)
tb.start()
check("H3pre B entered and stalled before lock",
      _entered.wait(timeout=10))
rA = _post(f"/samelabs/tools/{rt4}/pricing",
           json={"cost": 200, "enabled": True},
           cookies={COOKIE_NAME: op_sess})
_release.set()
tb.join(30)
_cat_mod.tool_row_for_update = _orig_tfu
rows4 = _audit_rows("pricing.update", rt4)
b_ok = res_b["r"].status_code == 200
seq_ok = (rows4 and rows4[0]["after_json"]["cost"] == 200
          and rows4[-1]["before_json"]["cost"] == 200
          and rows4[-1]["after_json"]["cost"] == 300)
check("H3 B audit before=200 (A 已 commit 的 after), 非 stale 100 → 300",
      rA.status_code == 200 and b_ok and seq_ok and len(rows4) == 2,
      f"A={rA.status_code} B={res_b['r'].status_code} "
      f"rows={[(x['before_json'], x['after_json']) for x in rows4]}")

# H4: concurrent package update → second audit before = first committed after
with db() as con:
    prow = con.execute(
        "insert into packages (code, name, credits, price_cents, active,"
        " created_at, updated_at) values (%s, 'CCPkg', 100, 999, TRUE, %s,"
        " %s) returning id", (f"pkg4_{_u}", ts, ts)).fetchone()
pkg4 = int(prow["id"])

_pkg_entered = threading.Event()
_pkg_release = threading.Event()
import repo.orders as _ord_mod
_orig_pbfu = _ord_mod.package_by_id_for_update
_pgate = {"armed": False, "stalled": False}

def _gated_pbfu(con, package_id):
    if int(package_id) == pkg4 and _pgate["armed"] \
            and not _pgate["stalled"]:
        _pgate["stalled"] = True
        _pkg_entered.set()
        _pkg_release.wait(timeout=30)
    return _orig_pbfu(con, package_id)

_ord_mod.package_by_id_for_update = _gated_pbfu

res_pb = {}
def _pkg_worker_b():
    res_pb["r"] = _patch(f"/samelabs/commerce/products/packages/{pkg4}",
                         json={"credits": 800},
                         cookies={COOKIE_NAME: op_sess})

_pgate["armed"] = True
tb2 = threading.Thread(target=_pkg_worker_b)
tb2.start()
check("H4pre B entered and stalled before package lock",
      _pkg_entered.wait(timeout=10))
rA2 = _patch(f"/samelabs/commerce/products/packages/{pkg4}", json={"credits": 500},
             cookies={COOKIE_NAME: op_sess})
_pkg_release.set()
tb2.join(30)
_ord_mod.package_by_id_for_update = _orig_pbfu
rows5 = _audit_rows("package.update", str(pkg4))
seq2_ok = (rows5 and rows5[0]["after_json"]["credits"] == 500
           and rows5[-1]["before_json"]["credits"] == 500
           and rows5[-1]["after_json"]["credits"] == 800)
check("H4 B audit before=500 (A 已 commit), 非 stale 100 → 800",
      rA2.status_code == 200 and res_pb["r"].status_code == 200
      and seq2_ok and len(rows5) == 2,
      f"A={rA2.status_code} B={res_pb['r'].status_code} "
      f"rows={[(x['before_json'], x['after_json']) for x in rows5]}")

# H5: pricing/presentation/remote-delete share the SAME tool-row boundary
# (proved structurally: all three call tool_row_for_update /
# platform_row_for_update which locks platform_tools.tool_key)
_src_sam = open(_os.path.join(_REPO, "app", "routes", "samelabs",
                               "tools.py"),
                encoding="utf-8").read() \
    + open(_os.path.join(_REPO, "core", "backoffice", "tools.py"),
           encoding="utf-8").read()
check("H5 三类 tool mutation 同走 tool-row FOR UPDATE 边界",
      _src_sam.count("tool_row_for_update(") >= 2
      and "platform_row_for_update(con, tool_key" in _src_sam)

# H6: target-specific allowlist rejects foreign fields
def _expect_record_raise(target, snap, label):
    try:
        with db() as con:
            _orig_rec(con, op, "x.test", target, "t", None, snap, ts)
        check(f"H6 {label} → record raises", False, "no raise")
    except ValueError as e:
        check(f"H6 {label} → record raises",
              "not_allowed" in str(e) or "unknown" in str(e), str(e))

_expect_record_raise("remote_tool", {"url": "https://x"}, "remote_tool+url")
_expect_record_raise("withdrawal",
                     {"creem_product_id": "prod_x"},
                     "withdrawal+creem_product_id")
_expect_record_raise("pricing", {"ledger_id": 1}, "pricing+ledger_id")
try:
    with db() as con:
        _orig_rec(con, op, "x.test", "bogus_target", "t", None,
                  {"anything": 1}, ts)
    check("H6d unknown target_type → raise", False, "no raise")
except ValueError as e:
    check("H6d unknown target_type → raise",
          "audit_target_type_unknown" in str(e), str(e))

# H7: forced allowlist failure rolls the business transaction back
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, visibility,"
        " enabled, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', 'produce', %s, 'wand',"
        " 'public', TRUE, %s, %s)",
        (rt4 + "z", rt4 + "z", _json3.dumps({"en": "ZZ"}), ts, ts))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 10, TRUE, %s)", (rt4 + "z", ts))
import backoffice.tools as _bo_tools      # app-path identity (routes use it)
_orig_snap = _bo_tools._snap_pricing
def _bad_snap(r):
    d = _orig_snap(r)
    return {**(d or {}), "url": "https://leak"} if d is not None else d
_bo_tools._snap_pricing = _bad_snap
r7 = _post(f"/samelabs/tools/{rt4}z/pricing",
           json={"cost": 55, "enabled": True},
           cookies={COOKIE_NAME: op_sess})
_bo_tools._snap_pricing = _orig_snap
with db() as con:
    cost_now = con.execute("select cost from pricing where tool_id=%s",
                           (rt4 + "z",)).fetchone()
check("H7 allowlist violation → pricing rollback (cost 仍 10) + 500",
      r7.status_code == 500 and int(cost_now["cost"]) == 10,
      f"{r7.status_code} cost={cost_now['cost']}")

# ══════════ I. unconditional target validation + pricing orphan gate ══════════

# I1: unknown target + NULL snapshots → raise + zero insert
_n_before = len(_audit_rows())
try:
    with db() as con:
        _orig_rec(con, op, "x.test", "bogus_target", "t", None, None, ts)
    check("I1 bogus target + NULL snapshots → raise + zero insert",
          False, "no raise")
except ValueError as e:
    check("I1 bogus target + NULL snapshots → raise + zero insert",
          "audit_target_type_unknown:bogus_target" in str(e)
          and len(_audit_rows()) == _n_before, str(e))

# I2: nonexistent pricing target (no tool row, no pricing row) → 404
ghost = f"ghost_{_u}"
r = _post(f"/samelabs/tools/{ghost}/pricing",
          json={"cost": 50, "enabled": True},
          cookies={COOKIE_NAME: op_sess})
with db() as con:
    orphan = con.execute("select 1 from pricing where tool_id=%s",
                         (ghost,)).fetchone()
    ghost_aud = con.execute(
        "select count(*) c from operator_audit where target_id=%s",
        (ghost,)).fetchone()
check("I2 nonexistent pricing target → 404 + zero pricing + zero audit",
      r.status_code == 404 and orphan is None
      and int(ghost_aud["c"]) == 0,
      f"{r.status_code} orphan={bool(orphan)} aud={ghost_aud['c']}")

# I3: two threads race the same nonexistent target → no orphan either way
results3 = {}
barrier3 = threading.Barrier(2)
def _ghost_worker(tag):
    barrier3.wait(timeout=15)
    results3[tag] = _post(f"/samelabs/tools/{ghost}/pricing",
                          json={"cost": 50, "enabled": True},
                          cookies={COOKIE_NAME: op_sess})
t3a = threading.Thread(target=_ghost_worker, args=("A",))
t3b = threading.Thread(target=_ghost_worker, args=("B",))
t3a.start(); t3b.start(); t3a.join(30); t3b.join(30)
with db() as con:
    orphan3 = con.execute("select 1 from pricing where tool_id=%s",
                          (ghost,)).fetchone()
    aud3 = int(con.execute(
        "select count(*) c from operator_audit where target_id=%s",
        (ghost,)).fetchone()["c"])
check("I3 双线程 nonexistent target → 均 404 + 零 orphan pricing + 零 audit",
      results3["A"].status_code == 404 and results3["B"].status_code == 404
      and orphan3 is None and aud3 == 0,
      f"A={results3['A'].status_code} B={results3['B'].status_code}"
      f" orphan={bool(orphan3)} aud={aud3}")

# I4: registered tool + missing pricing → repair allowed (pricing + audit)
rt5 = f"oatool5_{_u}"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " tool_type, label_json, icon, visibility,"
        " enabled, created_at, updated_at)"
        " values (%s, NULL, %s, 'local', 'produce', %s, 'wand',"
        " 'public', TRUE, %s, %s)",
        (rt5, rt5, _json3.dumps({"en": "R5"}), ts, ts))
r = _post(f"/samelabs/tools/{rt5}/pricing",
          json={"cost": 42, "enabled": True},
          cookies={COOKIE_NAME: op_sess})
rows5 = _audit_rows("pricing.update", rt5)
check("I4 registered tool 缺 pricing → 正常创建 pricing + audit (before=NULL)",
      r.status_code == 200 and len(rows5) == 1
      and rows5[0]["before_json"] is None
      and rows5[0]["after_json"]["cost"] == 42,
      f"{r.status_code} {rows5[:1]}")

# I5: retired pricing-only target → update + audit still work
rt6 = f"oatool6_{_u}"
with db() as con:
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 77, TRUE, %s)", (rt6, ts))
r = _post(f"/samelabs/tools/{rt6}/pricing",
          json={"cost": 88, "enabled": True},
          cookies={COOKIE_NAME: op_sess})
rows6 = _audit_rows("pricing.update", rt6)
check("I5 retired pricing-only target → 正常 update + audit (77→88)",
      r.status_code == 200 and len(rows6) == 1
      and rows6[0]["before_json"]["cost"] == 77
      and rows6[0]["after_json"]["cost"] == 88,
      f"{r.status_code} {rows6[:1]}")

# ═══ cleanup: every fixture this suite created is removed (leaked
# platform_tools rows drift the browser gate's market page)
with db() as con:
    con.execute("delete from pricing where tool_id like %s", (f"%{_u}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"%{_u}%",))
    uids = [int(r["id"]) for r in con.execute(
        "select id from users where username like %s", (_u + "%",)).fetchall()]
    if uids:
        # operator_audit rows reference actor/target users by FK — delete
        # them before the users (target_id is text: match both shapes)
        con.execute(
            "delete from operator_audit where actor_user_id = ANY(%s)"
            " or target_id = ANY(%s)",
            (uids, [str(u) for u in uids]))
    con.execute("delete from operator_audit where target_id like %s",
                (f"%{_u}%",))
    if uids:
        con.execute("delete from tool_calls where user_id = ANY(%s)", (uids,))
        con.execute("delete from ledger where user_id = ANY(%s)", (uids,))
        con.execute("delete from upload_usage where user_id = ANY(%s)", (uids,))
        con.execute("delete from ingress_requests where user_id = ANY(%s)", (uids,))
        con.execute("delete from uploads where user_id = ANY(%s)", (uids,))
        purge_users(con, uids,)


print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
