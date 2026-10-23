#!/usr/bin/env python3
"""Operator authority — every back-office mutation lands one audit fact,
the control plane and withdrawal workflow, and the back-office
supply / content / call-log surfaces.
"""
from _harness import REPO, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Operator audit
#
# Operator audit canonical — mutation → durable append-only audit fact.
#
# Proves over real routes/functions (isolated DB, browser-session operator):
#   authority frozen (session+samelabs gate; no Bearer; no status route);
#   pricing / plan / package / presentation / credits / withdrawal / remote
#   mutations each land exactly one allowlisted audit fact in the SAME
#   transaction (forced audit failure → full rollback; Creem failure → zero
#   committed audit; audit-before-Creem ordering); credits replay = one
#   ledger + one audit; append-only (no UPDATE/DELETE writer); snapshots
#   carry allowlist fields only — never URL/endpoint_json/auth/Vault.
# ════════════════════════════════════════════════════════════════════════
section('Operator audit')

import os as _os
import sys as _sys


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
        " label_json, visibility,"
        " enabled, created_at, updated_at, accept_json, format_family)"
        " values (%s, NULL, %s, 'local', '{\"en\":\"OA\"}',"
        " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
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

# ── F11/F12: active 显式声明 + creem_product_id 类型口径 ──
_PATCH_LOG.clear()
# active:false 创建 → 行 active=false, 不入可售投影; 切换后恢复
r = _post("/samelabs/commerce/products/plans", json={
    "code": f"oadraft_{_u}", "name": "OA Draft", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": None, "sort_order": _so + 2,
    "active": False,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
_draft_id = r.json().get("plan", {}).get("id")
with db() as con:
    _draft_row = con.execute(
        "select active, creem_product_id from plans where id=%s",
        (_draft_id,)).fetchone()
from governance import plan_catalog as _pc
_pc.catalog(refresh=True)
_sellable = [t_.code for t_ in _pc.catalog() if t_.sellable]
_dr_log = list(_PATCH_LOG)
check("C-draft active:false create → stored false, NULL binding, not sellable",
      r.status_code == 200 and _draft_row is not None
      and _draft_row["active"] is False
      and _draft_row["creem_product_id"] is None
      and f"oadraft_{_u}" not in _sellable,
      f"{r.status_code} {dict(_draft_row) if _draft_row else None}")
_MIRROR[f"prod_oad_{_u}"] = {"price": 999, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}
r = _patch(f"/samelabs/commerce/products/plans/{_draft_id}",
           json={"active": True, "creem_product_id": f"prod_oad_{_u}"},
           cookies={COOKIE_NAME: op_sess})
_pc.catalog(refresh=True)
_sellable = [t_.code for t_ in _pc.catalog() if t_.sellable]
check("C-draft switch (active+binding) → sellable",
      r.status_code == 200 and f"oadraft_{_u}" in _sellable,
      f"{r.status_code} {_sellable[:3]}")
# F12: 空绑定 (null) 创建零 provider I/O — 上面的 draft 创建即无镜像写
check("C-draft null-binding create made zero provider mirror calls",
      not _dr_log, str(_dr_log))
# creem_product_id 非字符串 → 解析失败 (422/400), 零写入
r = _post("/samelabs/commerce/products/plans", json={
    "code": f"oabad_{_u}", "name": "OA Bad", "monthly_credits": 100,
    "price_cents": 999, "creem_product_id": 12345, "sort_order": _so + 3,
    "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
    "expiry_days": [7], "download_limits": [50],
}, cookies={COOKIE_NAME: op_sess})
with db() as con:
    _bad_n = con.execute("select count(*) n from plans where code=%s",
                         (f"oabad_{_u}",)).fetchone()["n"]
check("C-bad non-string creem_product_id → rejected, zero rows",
      r.status_code in (400, 404, 422) and int(_bad_n) == 0,
      f"{r.status_code} rows={_bad_n}")

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
    "timeout_s": 30, "format_families": "image", "accept": ".png", "cost": 50,
}, cookies={COOKIE_NAME: op_sess})
from secrets_vault import vault_enabled as _vault_on
_tok_expect = 200 if _vault_on() else 503
r_tok = _post("/samelabs/tools/create", json={
    "tool_id": rt + "t", "label": "OA Tok", "url": "https://ops.example/t",
    "timeout_s": 30, "format_families": "image", "accept": ".png", "auth_token": "SUPERSECRET123", "cost": 10,
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
    "timeout_s": 30, "format_families": "image", "accept": ".png", "cost": 10,
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
        " label_json, visibility,"
        " enabled, created_at, updated_at, accept_json, format_family)"
        " values (%s, NULL, %s, 'http', %s,"
        " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
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
ins_writers = _sp.run(["grep", "-rn", "-I", "INSERT INTO operator_audit",
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
    "timeout_s": 30, "format_families": "image", "accept": ".png", "cost": 20,
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
        " label_json, visibility,"
        " enabled, created_at, updated_at, accept_json, format_family)"
        " values (%s, NULL, %s, 'local', %s,"
        " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
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
_src_sam = open(_os.path.join(REPO, "app", "routes", "samelabs",
                               "tools.py"),
                encoding="utf-8").read() \
    + open(_os.path.join(REPO, "core", "backoffice", "tools.py"),
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
        " label_json, visibility,"
        " enabled, created_at, updated_at, accept_json, format_family)"
        " values (%s, NULL, %s, 'local', %s,"
        " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
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
        " label_json, visibility,"
        " enabled, created_at, updated_at, accept_json, format_family)"
        " values (%s, NULL, %s, 'local', %s,"
        " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
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


# ════════════════════════════════════════════════════════════════════════
# Back-office supply, content governance, call log
#
# C2b back-office completion canonical.
#
# T*  Tool supply list: search (key / label / owner), class filter
#     (official vs community), delisted filter, pagination.
# D*  Tool detail page: declaration (labels per language, owner, http
#     endpoint WITHOUT credentials, input window, outputs, params,
#     pricing), 30-day call stats + provider earn, recent calls with
#     user links, this tool's audit history; mutations reuse the same
#     audited endpoints (delist switch shown).
# C*  Content governance: files (uploads) and results (/r) found by
#     share id / result id / user / tool; takedown deletes the mapping
#     row via the sharing single point and writes one upload.delete
#     audit fact (snapshot carries identity fields only).
# L*  Call log: filters (tool / user / status / error / date range),
#     per-row charge + settlement derived from ledger facts
#     (provider earn + commission / platform revenue / refunded).
# O*  Overview daily table: 14/30-day windows, per-day registrations /
#     calls / fail rate / net credits charged / platform income /
#     provider earn — aggregated live from users/tool_calls/ledger and
#     equal to direct SQL on today's row.
# K*  User detail API keys: prefix-only listing (full key never
#     rendered), operator delete removes the row and writes one
#     api_key.delete audit fact (no hash/sealed material); replay 404.
# X*  Operator gate on every new surface: browser session of a normal
#     user → login redirect; non-browser → 401 JSON; POST endpoints
#     denied the same way.
# ════════════════════════════════════════════════════════════════════════
section('Back-office supply, content governance, call log')

import os as _os
import sys as _sys


import json
import http.client as _hc
import time
import urllib.parse as _up
ts_bo = int(time.time())
_u_bo = "c2b" + _os.urandom(4).hex()

from database import db

HOST = ("127.0.0.1", int(TEST_PORT))


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body).encode()
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        return r.status, hdrs, json.loads(data) if data else None
    except Exception:
        return r.status, hdrs, data.decode(errors="replace")


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    conn = _hc.HTTPConnection(*HOST, timeout=30)
    b, ct = _form({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck


op_name = _u_bo + "op"
op_bo, OP_CK = _register(op_name)
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op_bo,))

prov_name = _u_bo + "prov"
prov, PROV_CK = _register(prov_name)
caller_name = _u_bo + "caller"
caller, CALLER_CK = _register(caller_name)

TOOL = f"{prov_name}_paytool"
AUTH_TOKEN_SECRET = "sk-c2b-secret-token-value"

# one provider tool (metered, params, auth) + one official local row
with db() as con:
    con.execute(
        """
        INSERT INTO platform_tools (tool_key, tool_id, execution,
          visibility, label_json, desc_json, enabled, owner_id,
          files_min, files_max, accept_json, format_family, outputs_json, params_json,
          endpoint_json, featured, sort_order, created_at, updated_at)
        VALUES (%s, 'paytool', 'http', 'public',
          '{"zh":"付费工具","en":"Paid Tool"}',
          '{"zh":"说明文字","en":"desc words"}', TRUE, %s,
          1, 3, '[".png",".pdf"]', 'image', '["text","file"]',
          '[{"key":"mode","type":"enum","label":"Mode",
             "required":true,"options":[{"value":"fast"}]}]',
          %s, TRUE, 5, %s, %s)
        """,
        (TOOL, prov, json.dumps(
            {"url": "https://c2b.example/hook", "timeout_s": 45,
             "auth_header": "X-Key", "auth_token": AUTH_TOKEN_SECRET}),
         ts_bo, ts_bo))
    con.execute(
        "INSERT INTO pricing (tool_id, cost, enabled, updated_at)"
        " VALUES (%s, 30, TRUE, %s)", (TOOL, ts_bo))
    con.execute(
        """
        INSERT INTO platform_tools (tool_key, tool_id, execution,
          visibility, label_json, enabled, created_at, updated_at, accept_json, format_family)
        VALUES ('c2boff', 'c2boff', 'local', 'public',
          '{"zh":"官方工具","en":"Official"}', TRUE, %s, %s, '[\".png\"]', 'image')
        """, (ts_bo, ts_bo))
    con.execute(
        "INSERT INTO pricing (tool_id, cost, enabled, updated_at)"
        " VALUES ('c2boff', 0, TRUE, %s)", (ts_bo,))

# calls + settlement ledger facts: one ok metered third-party call
# (payout 21 + commission 9 millis), one failed refunded call, one
# insufficient attempt — plus today's registration facts (3 users above)
with db() as con:
    pid = con.execute("select id from users where username='platform'"
                      ).fetchone()["id"]
    con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
          cost_snapshot, error, started_at, finished_at,
          commission_rate_snapshot, invocation_hash, result_public_id,
          result_json)
        VALUES ('c2b-ok', %s, %s, 'ok', 30, NULL, %s, %s, 30, 'h1',
                'rsltc2bok',
                '{"result_id":"rsltc2bok","items":[{"kind":"file",
                  "public_id":"shrc2bone","filename":"report.pdf"}]}')
        """, (caller, TOOL, ts_bo, ts_bo))
    ok_call = con.execute(
        "select id from tool_calls where client_ref='c2b-ok'"
        ).fetchone()["id"]
    con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
          cost_snapshot, error, started_at, finished_at,
          commission_rate_snapshot, invocation_hash)
        VALUES ('c2b-fail', %s, %s, 'failed', 30, 'tool_failed', %s, %s,
                30, 'h2')
        """, (caller, TOOL, ts_bo, ts_bo))
    fail_call = con.execute(
        "select id from tool_calls where client_ref='c2b-fail'"
        ).fetchone()["id"]
    con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
          cost_snapshot, error, started_at, finished_at,
          commission_rate_snapshot, invocation_hash)
        VALUES ('c2b-ins', %s, 'c2boff', 'insufficient', 0,
                'balance:0', %s, %s, 0, 'h3')
        """, (caller, ts_bo, ts_bo))
    for row in (
        (caller, -30, 70, "consume", "tool_call", str(ok_call),
         f"call:{ok_call}", TOOL, ts_bo),
        (prov, 21, 21, "provider_earn", "tool_call", str(ok_call),
         f"payout:{ok_call}", "payout", ts_bo),
        (pid, 9, 9, "platform_commission", "tool_call", str(ok_call),
         f"platform_commission:{ok_call}", "commission", ts_bo),
        (caller, -30, 40, "consume", "tool_call", str(fail_call),
         f"call:{fail_call}", TOOL, ts_bo),
        (caller, 30, 70, "refund", "ledger", None, None, "refund", ts_bo),
    ):
        con.execute(
            """
            INSERT INTO ledger (user_id, delta, balance_after, kind,
              ref_type, ref_id, idem_key, note, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, row)
    charge = con.execute(
        "select id from ledger where kind='consume' and idem_key=%s",
        (f"call:{fail_call}",)).fetchone()["id"]
    refund_id = con.execute(
        "select id from ledger where kind='refund' and note='refund'"
        " order by id desc limit 1").fetchone()["id"]
    con.execute("update ledger set ref_id=%s where id=%s",
                (str(charge), refund_id))
    con.execute(
        """
        INSERT INTO uploads (user_id, ip, gokapi_id, public_id,
          file_name, content_type, size_bytes, expiry_days,
          allowed_downloads, expire_at, url_download, url_hotlink,
          created_at)
        VALUES (%s, '127.0.0.1', 'gkc2bfake', 'shrc2bone',
          'report.pdf', 'application/pdf', 2048, 7, 5, %s,
          '/f/shrc2bone', NULL, %s)
        """, (caller, ts_bo + 7 * 86400, ts_bo))

# ═══ T. Tool supply list: search / filters / pagination ═══
H = {"Cookie": OP_CK}
st, _h, body = _req("GET", "/samelabs/tools", None, H)
body = body or ""
check("T1 default list renders both rows",
      TOOL in body and "c2boff" in body, st)
st, _h, body = _req("GET", f"/samelabs/tools?q={_up.quote('付费工具')}",
                    None, H)
check("T2 search matches label language (zh)",
      TOOL in (body or "") and "c2boff" not in (body or ""), st)
st, _h, body = _req("GET", f"/samelabs/tools?q={prov_name}", None, H)
check("T3 search matches owner username",
      TOOL in (body or ""), st)
st, _h, body = _req("GET", "/samelabs/tools?cls=official", None, H)
check("T4 official filter hides community rows",
      "c2boff" in (body or "") and TOOL not in (body or ""), st)
st, _h, body = _req("GET", "/samelabs/tools?cls=community", None, H)
check("T5 community filter hides official rows",
      TOOL in (body or "") and "c2boff" not in (body or ""), st)
st, _h, body = _req("GET", "/samelabs/tools?featured=1", None, H)
check("T6 featured filter keeps only featured",
      TOOL in (body or "") and "c2boff" not in (body or ""), st)
st, _h, body = _req("GET",
                    "/samelabs/tools?q=" + _up.quote("不存在的词xyz"),
                    None, H)
check("T7 no-hit search shows the empty state",
      "暂无工具" in (body or ""), st)
st, _h, body = _req("GET", "/samelabs/tools?q=&page=99", None, H)
check("T8 far page: valid response + empty state",
      st == 200 and "暂无工具" in (body or ""), st)

# ═══ D. Tool detail page ═══
enc = _up.quote(TOOL, safe="")
st, _h, body = _req("GET", f"/samelabs/tools/{enc}", None, H)
b = body or ""
check("D1 detail renders declaration",
      st == 200 and "付费工具" in b and "Paid Tool" in b
      and "说明文字" in b and "desc words" in b, st)
check("D2 owner links to the user detail page",
      f'href="/samelabs/users/{prov}"' in b)
check("D3 endpoint URL + timeout shown, credential NEVER",
      "https://c2b.example/hook" in b and "45" in b
      and AUTH_TOKEN_SECRET not in b and "X-Key" not in b)
check("D4 input window / formats / outputs / params",
      "1 – 3" in b and ".png" in b and ".pdf" in b
      and "text / file" in b and "mode" in b and "enum" in b)
check("D5 pricing + 30-day stats + earn",
      "30" in b and "已配置" in b
      and "$0.021" in b)
check("D6 recent call links user + result page",
      f'href="/samelabs/users/{caller}"' in b and "/r/rsltc2bok" in b)
check("D7 unknown tool detail → 404",
      _req("GET", "/samelabs/tools/nobody%2Fnone", None, H)[0] == 404)
# audit history reflects a real audited mutation (delist via endpoint)
st, _h, d = _req("POST", f"/samelabs/tools/{enc}/delist",
                 {"delisted": True},
                 {"Content-Type": "application/json", "Cookie": OP_CK})
st, _h, body = _req("GET", f"/samelabs/tools/{enc}", None, H)
check("D8 detail audit history shows the delist fact",
      d.get("ok") and "tool.delisted.update" in (body or ""),
      (st, d))
st, _h, body = _req("GET", "/samelabs/tools?delisted=1", None, H)
check("T9 delisted filter finds the taken-down tool",
      TOOL in (body or ""))
_req("POST", f"/samelabs/tools/{enc}/delist", {"delisted": False},
     {"Content-Type": "application/json", "Cookie": OP_CK})

# ═══ C. Content governance ═══
st, _h, body = _req("GET", "/samelabs/content?share=shrc2bone", None, H)
b = body or ""
check("C1 find file by share id",
      st == 200 and "report.pdf" in b and "application/pdf" in b
      and "2.0 KB" in b and f"/samelabs/users/{caller}" in b
      and "/f/shrc2bone" in b, st)
st, _h, body = _req("GET", f"/samelabs/content?user={caller_name}",
                    None, H)
check("C2 file list by username + result list by user",
      "report.pdf" in (body or "") and "/r/rsltc2bok" in (body or ""))
st, _h, body = _req("GET", f"/samelabs/content?tool={_up.quote(TOOL)}",
                    None, H)
check("C3 tool filter finds the produced file + the result",
      "report.pdf" in (body or "") and "/r/rsltc2bok" in (body or ""))
st, _h, body = _req("GET",
                    "/samelabs/content?share=missing&result=missing",
                    None, H)
check("C4 no-hit shows empty states",
      "未找到文件" in (body or "") and "未找到结果" in (body or ""))
st, _h, d = _req("POST", "/samelabs/content/uploads/999999/delete",
                 {}, {"Content-Type": "application/json", "Cookie": OP_CK})
check("C5 takedown of unknown upload → 404", st == 404, st)
with db() as con:
    up_id = con.execute(
        "select id from uploads where public_id='shrc2bone'"
        ).fetchone()["id"]
st, _h, d = _req("POST", f"/samelabs/content/uploads/{up_id}/delete",
                 {}, {"Content-Type": "application/json", "Cookie": OP_CK})
with db() as con:
    gone = con.execute(
        "select 1 from uploads where id=%s", (up_id,)).fetchone()
    au = con.execute(
        "select before_json, after_json from operator_audit"
        " where action='upload.delete' and target_type='upload'"
        " and target_id=%s", (str(up_id),)).fetchone()
check("C6 takedown deletes the mapping row + audits",
      d.get("ok") and gone is None and au is not None
      and au["before_json"]["public_id"] == "shrc2bone"
      and au["before_json"]["file_name"] == "report.pdf"
      and au["after_json"] is None, (d, au and dict(au)))
check("C7 audit snapshot carries identity fields only",
      set(au["before_json"].keys()) ==
      {"id", "user_id", "public_id", "file_name", "content_type",
       "size_bytes"}, sorted(au["before_json"].keys()))

# ═══ L. Call log ═══
st, _h, body = _req("GET", "/samelabs/calls", None, H)
b = body or ""
# Jinja |urlencode keeps '/' readable in the href — the :path route
# accepts the raw key, so assert the unencoded link
check("L1 default log renders both calls + links",
      f'href="/samelabs/tools/{TOOL}"' in b
      and f'href="/samelabs/users/{caller}"' in b, st)
check("L2 ok row shows charge + payout/commission settlement",
      "30" in b and "开发者收益 $0.021" in b and "抽佣 $0.009" in b)
check("L3 failed row shows refunded",
      "已退款" in b and "tool_failed" in b)
st, _h, body = _req("GET", "/samelabs/calls?status=failed", None, H)
b = body or ""
check("L4 status filter",
      "tool_failed" in b and "/r/rsltc2bok" not in b)
st, _h, body = _req("GET",
                    f"/samelabs/calls?tool={_up.quote(TOOL)}&error=tool",
                    None, H)
b = body or ""
check("L5 tool + error filters combine",
      "tool_failed" in b and "rsltc2bok" not in b)
_today = time.strftime("%Y-%m-%d", time.gmtime())
st, _h, body = _req("GET",
                    f"/samelabs/calls?user={caller_name}"
                    f"&date_from={_today}&date_to={_today}", None, H)
check("L6 user + date range filters find today's rows",
      "tool_failed" in (body or ""))
st, _h, body = _req("GET",
                    f"/samelabs/calls?user={caller_name}&date_from=2001-01-01&date_to=2001-01-02",
                    None, H)
check("L7 out-of-range dates → empty",
      "暂无调用" in (body or ""))

# ═══ O. Overview daily table ═══
st, _h, body = _req("GET", "/samelabs?range=14", None, H)
b = body or ""
_oldest = time.strftime("%Y-%m-%d", time.gmtime(ts_bo - 13 * 86400))
check("O1 daily table renders the full 14-day window",
      "最近 14 天" in b and f"<td>{_oldest}</td>" in b, st)
with db() as con:
    direct = con.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM users
             WHERE (to_timestamp(created_at) AT TIME ZONE 'UTC')::date
               = (to_timestamp(%s) AT TIME ZONE 'UTC')::date) AS reg,
          (SELECT COUNT(*) FROM tool_calls
             WHERE (to_timestamp(started_at) AT TIME ZONE 'UTC')::date
               = (to_timestamp(%s) AT TIME ZONE 'UTC')::date) AS calls,
          (SELECT -COALESCE(SUM(delta),0) FROM ledger WHERE kind='consume'
             AND (to_timestamp(created_at) AT TIME ZONE 'UTC')::date
               = (to_timestamp(%s) AT TIME ZONE 'UTC')::date)
           - (SELECT COALESCE(SUM(delta),0) FROM ledger WHERE kind='refund'
             AND (to_timestamp(created_at) AT TIME ZONE 'UTC')::date
               = (to_timestamp(%s) AT TIME ZONE 'UTC')::date) AS net
        """, (ts_bo, ts_bo, ts_bo, ts_bo)).fetchone()
_day = time.strftime("%Y-%m-%d", time.gmtime(ts_bo))
check("O2 today's row equals direct SQL (registrations/calls/net)",
      f"<td>{_day}</td>" in b
      and f">{int(direct['reg']):,}<" in b
      and f">{int(direct['calls']):,}<" in b
      and f">{int(direct['net']):,}<" in b,
      (dict(direct), _day))
st, _h, body30 = _req("GET", "/samelabs?range=30", None, H)
check("O3 30-day window available",
      "最近 30 天" in (body30 or ""))
st, _h, body9 = _req("GET", "/samelabs?range=9", None, H)
check("O4 out-of-vocabulary range falls back to 30",
      "最近 30 天" in (body9 or ""))

# ═══ K. User detail: API keys ═══
from identity.apikeys import create_key
krow = create_key(caller, "ci key")
full_key = krow["key"]
st, _h, body = _req("GET", f"/samelabs/users/{caller}", None, H)
b = body or ""
check("K1 user detail lists files + key prefix only",
      "ci key" in b and (krow["prefix"] + "…") in b
      and full_key not in b, st)
st, _h, d = _req("POST",
                 f"/samelabs/users/{caller}/keys/{krow['id']}/delete",
                 {}, {"Content-Type": "application/json", "Cookie": OP_CK})
with db() as con:
    gone = con.execute("select 1 from api_keys where id=%s",
                       (krow["id"],)).fetchone()
    au = con.execute(
        "select before_json, after_json from operator_audit"
        " where action='api_key.delete' and target_type='api_key'"
        " and target_id=%s", (str(krow["id"]),)).fetchone()
check("K2 operator key delete removes row + audits (prefix only)",
      d.get("ok") and gone is None and au is not None
      and au["before_json"]["prefix"] == krow["prefix"]
      and au["after_json"] is None, (d, au and dict(au)))
check("K3 audit snapshot has no secret material",
      set(au["before_json"].keys()) == {"id", "user_id", "name",
                                        "prefix"})
st, _h, d = _req("POST",
                 f"/samelabs/users/{caller}/keys/{krow['id']}/delete",
                 {}, {"Content-Type": "application/json", "Cookie": OP_CK})
check("K4 replay delete → 404", st == 404, st)
# authentication really stops for a deleted key (identity single point)
st, _h, d = _req("GET", "/api/me", None,
                 {"Authorization": f"Bearer {full_key}"})
check("K5 deleted key no longer authenticates", st == 401, st)

# ═══ X. Operator gate on the new surfaces ═══
for path, redir in (("/samelabs/content", True), ("/samelabs/calls", True),
                    (f"/samelabs/tools/{enc}", True),
                    (f"/samelabs/users/{caller}/keys/1/delete", False)):
    st, hdrs, _b = _req("GET" if redir else "POST", path, None,
                        {"Cookie": CALLER_CK,
                         "Accept": "text/html"} if redir else
                        {"Cookie": CALLER_CK,
                         "Content-Type": "application/json"})
    if redir:
        check(f"X {path} normal session → login redirect",
              st == 302 and "/auth/login" in hdrs.get("location", ""),
              (st, hdrs.get("location")))
    else:
        check(f"X {path} normal session POST → 401 JSON",
              st == 401, st)
st, hdrs, _b = _req("GET", "/samelabs/content")
check("X anonymous non-browser → 401 JSON", st == 401, st)

with db() as con:
    purge_users(con, [op_bo, prov, caller])
# tool rows / calls / ledger facts of this run roll away with the users;
# the one platform-side commission row is removed by its idem key (the
# platform bookkeeping account itself is never touched)
with db() as con:
    con.execute("delete from ledger where idem_key=%s",
                (f"platform_commission:{ok_call}",))
    con.execute("delete from pricing where tool_id=%s", (TOOL,))
    con.execute("delete from platform_tools where tool_key in (%s,%s)",
                (TOOL, 'c2boff'))


# ════════════════════════════════════════════════════════════════════════
# Control plane + withdrawal workflow
#
# S4 canonical: Samelabs Control Plane replacement + Withdrawal workflow
# + operator reporting truth.
#
# Covers (per batch A spec §24):
# Control Plane:
#   CP1  session operator PASS / anonymous / Bearer token cannot enter
#   CP2  Overview asset separation (no cross-asset SUM; USD via money)
#   CP3  Users: Spendable Credits != Provider Earnings (separate columns)
#   CP4  generic mixed ledger retired (no /samelabs/ledger)
#   CP5  Tool Supply uses catalog/pricing facts (classification)
#   CP6  Orders read-only (no payment-status mutation endpoint)
#   CP7  Membership projection from membership row (not orders)
#   CP8  operator audit page renders
# Withdrawal:
#   W1   request freezes payout destination (snapshot)
#   W2   later profile edit does not change existing request
#   W3   empty payout destination rejects request (payout_channel_required)
#   W4   pending→approved / W5 pending→rejected / W6 approved→paid
#   W7   pending→paid rejected
#   W8   approve exactly one debit / W9 paid zero second debit
#   W10  approval replay / W11 reject replay / W12 paid same-ref replay
#   W13  paid different-ref conflict
#   W14  concurrent same-provider approvals
#   W15  earning settlement vs withdrawal = same user-row serialization
#   W16  reconciliation invariant (per-status ledger count)
# Schema:
#   S1   fresh-schema CHECK invariants (status × fields)
# Back-office closure (B1):
#   X1   hostile values (query params, usernames, notes, audit JSON, payout
#        snapshots/refs, provider ids, plan names) render escaped on every
#        operator page
#   K1   provider tool keys 'owner/tool': pricing / presentation / enabled
#        routes match (encoded and raw), JS builds encoded URLs
#   E1   registry enabled toggle (system/operator/provider rows) = one tx
#        with operator_audit
#   PL1  PATCH plan DB conflicts → 409 sort_order_taken / product_already_bound
#   CR1  credits adjust: unknown user 404, out-of-range 422, same action id
#        + different amount → 409 action_id_conflict (no movement)
#   PF1  price floor: USD only, credits <= price_cents * 10 (packages+plans)
#   US1  user search (username/display name/email/id) + read-only detail
#   WQ1  withdrawal queue paging + status filter
#   RT1  reporting labels: effective Plan column / effective membership
#        count / approved-unpaid vs paid / Tool Credits net of refunds
# Hygiene: unique per-run names, own plan (never 'free'), full cleanup of
# every row created (re-runnable; order-independent vs other suites).
# ════════════════════════════════════════════════════════════════════════
section('Control plane + withdrawal workflow')

import json
import os
import sys
import threading
import time
import urllib.request

os.chdir(REPO)


from database import db, init_db  # noqa: E402

init_db()

from economy import earnings  # noqa: E402
from economy.billing import grant, SPENDABLE_KINDS  # noqa: E402
from economy.money import parse_usd_decimal, millis_to_usd_str  # noqa: E402
from repo import operator_views  # noqa: E402
from repo import economy as economy_repo  # noqa: E402
from utils import now  # noqa: E402

ts_cp = now()

# ── per-run naming + own-rows cleanup ──
# users:  s4u_<role>_<RUN>   tools: s4t_<name>_<RUN> (+ provider keys owned
# by s4u_* users)   plans: s4p<RUN><n>   packages: s4k<RUN><n>
_RUN = os.urandom(3).hex()
_USER_RE = r"^s4u_[a-z0-9]+_[0-9a-f]{6}$"
_TOOL_RE = r"^s4t_[a-z0-9]+_[0-9a-f]{6}$"
_PLAN_RE = r"^s4p[0-9a-f]{6}[a-z0-9]*$"
_PKG_RE = r"^s4k[0-9a-f]{6}[a-z0-9]*$"
_XUSER_RE = r"<script>s4x[0-9a-f]{6}</script>"   # hostile-payload usernames
_EXTRA_USERS: list = []


def uname(role: str) -> str:
    return f"s4u_{role}_{_RUN}"


def tkey(name: str) -> str:
    return f"s4t_{name}_{_RUN}"


def _cleanup_own_rows():
    """Delete every row this suite creates (current run AND leftovers of
    an aborted earlier run) — never another suite's data."""
    with db() as con:
        uids = [int(r["id"]) for r in con.execute(
            "SELECT id FROM users WHERE username ~ %s OR username ~ %s"
            " OR id = ANY(%s)",
            (_USER_RE, _XUSER_RE, list(_EXTRA_USERS))).fetchall()]
        keys = [r["tool_key"] for r in con.execute(
            "SELECT tool_key FROM platform_tools WHERE tool_key ~ %s"
            " OR owner_id = ANY(%s)", (_TOOL_RE, uids)).fetchall()]
        con.execute("DELETE FROM operator_audit WHERE actor_user_id = ANY(%s)",
                    (uids,))
        con.execute("DELETE FROM pricing WHERE tool_id ~ %s"
                    " OR tool_id = ANY(%s)", (_TOOL_RE, keys))
        con.execute("DELETE FROM platform_tools WHERE tool_key ~ %s"
                    " OR tool_key = ANY(%s)", (_TOOL_RE, keys))
        con.execute("DELETE FROM pending_downgrades WHERE user_id = ANY(%s)",
                    (uids,))
        purge_users(con, uids)
        con.execute("DELETE FROM pending_downgrades WHERE target_plan_id IN"
                    " (SELECT id FROM plans WHERE code ~ %s)", (_PLAN_RE,))
        con.execute("DELETE FROM plans WHERE code ~ %s", (_PLAN_RE,))
        con.execute("DELETE FROM packages WHERE code ~ %s", (_PKG_RE,))


_cleanup_own_rows()


def mkuser(name: str, status: str = "active", *, display_name=None,
           email=None) -> int:
    with db() as con:
        return int(con.execute(
            "INSERT INTO users (username, display_name, email, status,"
            " created_at, last_login_at) VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (name, display_name or name, email, status, ts_cp,
             ts_cp)).fetchone()["id"])


op_cp = mkuser(uname("op"), "samelabs")
provider = mkuser(uname("prov"))
caller = mkuser(uname("caller"))
print(f"fixtures: op={op_cp} provider={provider} caller={caller}")


def set_channel(uid: int, ch: str):
    with db() as con:
        con.execute("UPDATE users SET payout_channel=%s WHERE id=%s",
                    (ch, uid))


def seed_earn(uid: int, amount_millis: int):
    """Direct provider_earn seed (test fixture; not the business writer)."""
    with db() as con:
        base = con.execute(
            "SELECT COALESCE(SUM(delta),0) s FROM ledger WHERE user_id=%s"
            " AND kind='provider_earn'", (uid,)).fetchone()["s"]
        from repo.ledger import insert_row
        import random as _rnd
        insert_row(con, uid, amount_millis, base + amount_millis,
                   "provider_earn", "tool_call", "999999",
                   f"s4seed:{uid}:{ts_cp}:{_rnd.randrange(1 << 30)}",
                   "s4 seed earn", ts_cp)


# ═══ W3: empty payout channel rejects request ═══
seed_earn(provider, 50000)
try:
    earnings.request_withdrawal(provider, 20000, note="w3")
    check("W3 empty channel → payout_channel_required", False, "no raise")
except ValueError as e:
    check("W3 empty channel → payout_channel_required",
          str(e) == "payout_channel_required", str(e))
with db() as con:
    n = con.execute("SELECT COUNT(*) n FROM withdrawals WHERE user_id=%s",
                    (provider,)).fetchone()["n"]
check("W3b zero withdrawal rows created", int(n) == 0, n)

# ═══ W1: request freezes payout destination ═══
_CH1 = f"USDT-TRC20:TOriginal{_RUN}"
set_channel(provider, _CH1)
w1 = earnings.request_withdrawal(provider, 20000, note="w1")
check("W1 request created pending",
      w1["status"] == "pending" and w1["amount"] == 20000, w1)
check("W1b payout_channel_snapshot frozen",
      w1["payout_channel_snapshot"] == _CH1, w1)

# ═══ W2: later profile edit does not change existing request ═══
set_channel(provider, "BANK:CH-NEW-456")
with db() as con:
    row = con.execute("SELECT payout_channel_snapshot FROM withdrawals"
                      " WHERE id=%s", (w1["id"],)).fetchone()
check("W2 profile edit ≠ request change",
      row["payout_channel_snapshot"] == _CH1, row)
# next request uses the NEW channel
w2 = earnings.request_withdrawal(provider, 10000, note="w2")
check("W2b new request uses new channel",
      w2["payout_channel_snapshot"] == "BANK:CH-NEW-456", w2)

# ═══ W4: pending→approved (exactly one debit) ═══
app1 = earnings.approve_withdrawal(w1["id"], admin_user_id=op_cp)
check("W4 approve → approved + reviewed fields",
      app1["status"] == "approved" and app1["reviewed_by"] == op_cp
      and app1["paid_at"] is None, app1)


def ledger_rows(wid: int, uid: int):
    with db() as con:
        return con.execute(
            "SELECT * FROM ledger WHERE kind='withdraw' AND ref_type="
            "'withdrawal' AND ref_id=%s AND user_id=%s",
            (str(wid), uid)).fetchall()


lr = ledger_rows(w1["id"], provider)
check("W4b exactly ONE withdraw ledger row",
      len(lr) == 1 and int(lr[0]["delta"]) == -20000, [dict(r) for r in lr])
check("W4c idem key = withdrawal:{id}",
      lr[0]["idem_key"] == f"withdrawal:{w1['id']}", lr[0]["idem_key"])

# ═══ W10: approval replay ═══
rp = earnings.approve_withdrawal(w1["id"], admin_user_id=op_cp)
check("W10 approve replay → success, stays approved",
      rp["status"] == "approved", rp)
check("W10b replay → zero second ledger row",
      len(ledger_rows(w1["id"], provider)) == 1)

# ═══ W6: approved→paid (zero ledger mutation) ═══
before_n = len(ledger_rows(w1["id"], provider))
paid = earnings.mark_withdrawal_paid(w1["id"], "TXN-ABC-001",
                                     admin_user_id=op_cp)
check("W6 paid fields + payout_ref stored verbatim",
      paid["status"] == "paid" and paid["payout_ref"] == "TXN-ABC-001"
      and paid["paid_by"] == op_cp and paid["paid_at"] is not None, paid)
check("W6b paid → ZERO new ledger rows",
      len(ledger_rows(w1["id"], provider)) == before_n)

# W12 same-ref replay; W13 different-ref conflict
rp2 = earnings.mark_withdrawal_paid(w1["id"], "TXN-ABC-001",
                                    admin_user_id=op_cp)
check("W12 paid same-ref replay → success, ref unchanged",
      rp2["payout_ref"] == "TXN-ABC-001", rp2)
try:
    earnings.mark_withdrawal_paid(w1["id"], "TXN-OTHER",
                                  admin_user_id=op_cp)
    check("W13 different ref → payout_ref_conflict", False, "no raise")
except ValueError as e:
    check("W13 different ref → payout_ref_conflict",
          str(e) == "payout_ref_conflict", str(e))
with db() as con:
    rr = con.execute("SELECT payout_ref FROM withdrawals WHERE id=%s",
                     (w1["id"],)).fetchone()
check("W13b original evidence not overwritten",
      rr["payout_ref"] == "TXN-ABC-001", rr)

# ═══ W5: pending→rejected (zero ledger) ═══
rej = earnings.reject_withdrawal(w2["id"], admin_user_id=op_cp)
check("W5 reject → rejected + reviewed fields",
      rej["status"] == "rejected" and rej["reviewed_by"] == op_cp, rej)
check("W5b rejected → ZERO withdraw ledger rows",
      len(ledger_rows(w2["id"], provider)) == 0)
# W11 reject replay
rp3 = earnings.reject_withdrawal(w2["id"], admin_user_id=op_cp)
check("W11 reject replay → success stays rejected",
      rp3["status"] == "rejected", rp3)

# ═══ W7: pending→paid forbidden ═══
w3 = earnings.request_withdrawal(provider, 10000, note="w7")
try:
    earnings.mark_withdrawal_paid(w3["id"], "X", admin_user_id=op_cp)
    check("W7 pending→paid forbidden", False, "no raise")
except ValueError as e:
    check("W7 pending→paid forbidden",
          str(e) == "transition_conflict", str(e))
# reject on approved / approve on rejected → conflict
try:
    earnings.reject_withdrawal(w1["id"], admin_user_id=op_cp)
    check("W7b reject on paid → conflict", False, "no raise")
except ValueError as e:
    check("W7b reject on paid → conflict",
          str(e) == "transition_conflict", str(e))
try:
    earnings.approve_withdrawal(w2["id"], admin_user_id=op_cp)
    check("W7c approve on rejected → conflict", False, "no raise")
except ValueError as e:
    check("W7c approve on rejected → conflict",
          str(e) == "transition_conflict", str(e))
# cleanup w3 for reconciliation clarity
earnings.reject_withdrawal(w3["id"], admin_user_id=op_cp)

# W8/W9 proved structurally by W4b (exactly one) + W6b (no second on paid)

# ═══ W16: reconciliation invariant per status ═══
with db() as con:
    rows = con.execute(
        "SELECT w.id, w.status, w.user_id, w.amount,"
        " (SELECT COUNT(*) FROM ledger l WHERE l.kind='withdraw'"
        "   AND l.ref_type='withdrawal' AND l.ref_id=w.id::text) AS n"
        " FROM withdrawals w WHERE w.user_id=%s", (provider,)).fetchall()
expect = {"pending": 0, "rejected": 0, "approved": 1, "paid": 1}
seen = {}
ok_all = True
for r in rows:
    seen.setdefault(r["status"], []).append(int(r["n"]))
    if int(r["n"]) != expect.get(r["status"]):
        ok_all = False
check("W16 reconciliation: every pending/rejected row=0 ledger,"
      " every approved/paid row=exactly 1",
      ok_all and "paid" in seen,
      {k: v for k, v in seen.items()})

# ═══ W14: concurrent same-provider approvals ═══
p2 = mkuser(uname("prov2"))
seed_earn(p2, 40000)
set_channel(p2, "USDT-TRC20:P2")
# (a) request-time reservation: an overcommitting second request is
#     refused AT REQUEST TIME (pending reserved), never entering the
#     approval path
earnings.request_withdrawal(p2, 25000, note="c0")
try:
    earnings.request_withdrawal(p2, 25000, note="c1")
    check("W14a overcommit second request refused", False, "accepted")
except ValueError as e:
    check("W14a overcommit second request refused",
          str(e) == "withdrawal_insufficient", str(e))
with db() as con:
    widA = int(con.execute(
        "SELECT id FROM withdrawals WHERE user_id=%s AND"
        " status='pending' ORDER BY id LIMIT 1", (p2,)).fetchone()["id"])
# (b) concurrent approve of the SAME withdrawal (double-click / two
#     operator tabs): idempotent — exactly one ledger row
res = []


def _ap(wid):
    try:
        r = earnings.approve_withdrawal(wid, admin_user_id=op_cp)
        res.append(r["status"])
    except ValueError as e:
        res.append(str(e))


t1 = threading.Thread(target=_ap, args=(widA,))
t2 = threading.Thread(target=_ap, args=(widA,))
t1.start(); t2.start(); t1.join(); t2.join()
with db() as con:
    nA = int(con.execute(
        "SELECT COUNT(*) n FROM ledger WHERE kind='withdraw' AND"
        " ref_id=%s", (str(widA),)).fetchone()["n"])
    aud_n = int(con.execute(
        "SELECT COUNT(*) n FROM operator_audit WHERE"
        " action='withdrawal.approve' AND target_id=%s",
        (str(widA),)).fetchone()["n"])
check("W14b concurrent same-id approvals → both success (idempotent)",
      sorted(res) == ["approved", "approved"], res)
check("W14c exactly ONE debit row for the withdrawal", nA == 1, nA)
check("W14d exactly ONE audit fact (no duplicate on replay)",
      aud_n == 1, aud_n)

# ═══ W15: earning settlement vs withdrawal share the user-row anchor ═══
# structural proof: gate writer and request/approve all lock_user first
import inspect  # noqa: E402
from execution import gate as _gate_mod  # noqa: E402
from repo import ledger as _ledger  # noqa: E402
from economy.billing import EARN_POOL_KINDS as _EPK  # noqa: E402
_gate_src = inspect.getsource(_gate_mod)
_earn_src = inspect.getsource(earnings)
_w15_sorted = 'for uid in sorted((int(user["id"]), int(owner_id))):' in _gate_src
# order: the ascending user-row locks precede the uploads landing call
# (the import line at function top is not the landing itself)
_w15_order = (_gate_src.find("for uid in sorted(") != -1
              and _gate_src.find("for uid in sorted(")
                  < _gate_src.find("land_items_result("))
check("W15 gate provider_earn writer locks user row",
      _w15_sorted and _w15_order
      and "ledger_repo.lock_user(con, uid)" in _gate_src, None)
check("W15b request_withdrawal locks user row",
      "ledger_repo.lock_user(con, user_id)" in _earn_src, None)
check("W15c approve_withdrawal locks user row",
      "ledger_repo.lock_user(con, int(row[\"user_id\"]))" in _earn_src
      or 'ledger_repo.lock_user(con, int(row["user_id"]))' in _earn_src,
      None)

# live interleaving proof: settlement racing an approval cannot lose the
# pool read (both serialize on the same users row)
p3 = mkuser(uname("prov3"))
seed_earn(p3, 30000)
set_channel(p3, "USDT-TRC20:P3")
wid3_cp = earnings.request_withdrawal(p3, 20000, note="w15")["id"]
errors = []


def _settle():
    # mimic the settlement writer shape: lock user, read pool, insert
    try:
        with db() as con:
            _ledger.lock_user(con, p3)
            base = _ledger.pool_running_balance(con, p3, _EPK)
            _ledger.insert_row(con, p3, 5000, base + 5000,
                               "provider_earn", "tool_call", "888888",
                               f"s4race:{p3}", "race earn", ts_cp)
    except Exception as exc:  # pragma: no cover
        errors.append(str(exc))


def _approve3():
    try:
        earnings.approve_withdrawal(wid3_cp, admin_user_id=op_cp)
    except ValueError as exc:
        errors.append("approve:" + str(exc))


t1 = threading.Thread(target=_settle)
t2 = threading.Thread(target=_approve3)
t1.start(); t2.start(); t1.join(); t2.join()
with db() as con:
    bal = _ledger.pool_running_balance(con, p3, _EPK)
    n3 = int(con.execute(
        "SELECT COUNT(*) n FROM ledger WHERE kind='withdraw' AND"
        " ref_id=%s", (str(wid3_cp),)).fetchone()["n"])
check("W15d race: both committed, pool consistent",
      not errors and n3 == 1 and bal == 30000 + 5000 - 20000,
      (errors, n3, bal))

# ═══ S1: fresh-schema CHECK invariants ═══
violations = []
cases = [
    # (status, reviewed, paid fields, payout_ref, should_pass)
    ("pending", False, False, None, True),
    ("pending", True, False, None, False),      # pending with review = bad
    ("approved", True, False, None, True),
    ("approved", True, True, "R", False),       # approved with paid = bad
    ("rejected", True, False, None, True),
    ("rejected", False, False, None, False),
    ("paid", True, True, "R", True),
    ("paid", True, True, None, False),          # paid without ref = bad
    ("paid", True, False, None, False),
]
for i, (st, rev, paid_f, ref, want) in enumerate(cases):
    with db() as con:
        try:
            con.execute(
                "INSERT INTO withdrawals (user_id, amount, status,"
                " created_at, payout_channel_snapshot, reviewed_at,"
                " reviewed_by, paid_at, paid_by, payout_ref)"
                " VALUES (%s, 1000, %s, %s, 'S1', %s, %s, %s, %s, %s)",
                (caller, st, ts_cp,
                 ts_cp if rev else None, op_cp if rev else None,
                 ts_cp if paid_f else None, op_cp if paid_f else None, ref))
            ok = True
        except Exception:
            ok = False
    if ok != want:
        violations.append((i, st, rev, paid_f, ref, want, ok))
check("S1 fresh-schema CHECK matrix (9 cases)",
      not violations, violations)
try:
    with db() as con:
        con.execute(
            "INSERT INTO withdrawals (user_id, amount, status, created_at,"
            " payout_channel_snapshot) VALUES (%s, 1000, 'pending', %s,"
            " '   ')", (caller, ts_cp))
    check("S1b blank snapshot rejected", False, "accepted blank")
except Exception:
    check("S1b blank snapshot rejected", True)

# ═══ CP1: operator gate (session vs anonymous vs Bearer) ═══
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app as _app  # noqa: E402
from app.web_auth import create_browser_session, COOKIE_NAME  # noqa: E402

client_cp = TestClient(_app)
sess = create_browser_session(op_cp)
r = client_cp.get("/samelabs", cookies={COOKIE_NAME: sess})
check("CP1a session operator enters Overview",
      r.status_code == 200 and "/samelabs/users" in r.text, r.status_code)
r = client_cp.get("/samelabs", follow_redirects=False)
check("CP1b anonymous redirected", r.status_code in (302, 307, 401),
      r.status_code)
r = client_cp.get("/samelabs/users", follow_redirects=False)
check("CP1c anonymous users page redirected",
      r.status_code in (302, 307, 401), r.status_code)

# Bearer capability token cannot enter — tokens are stored hashed, so
# forge the header shape directly; the gate requires auth_via='session'
# regardless of the token's validity (control plane ≠ token surface)
r = client_cp.get("/samelabs", follow_redirects=False,
               headers={"Authorization": "Bearer fs_test_token_value"})
check("CP1d Bearer token cannot enter control plane",
      r.status_code in (302, 307, 401), r.status_code)

# ═══ CP2: Overview asset separation ═══
with db() as con:
    prod = dict(operator_views.overview_product(con))
    cons = dict(operator_views.overview_consumer(con, SPENDABLE_KINDS, ts_cp))
    prov = dict(operator_views.overview_provider(con))
    plat = dict(operator_views.overview_platform(con))
r = client_cp.get("/samelabs", cookies={COOKIE_NAME: sess})
body_cp = r.text
check("CP2a Overview renders 4 asset cards",
      all(k in body_cp for k in ("产品", "消费积分",
                              "开发者收益", "平台收入")),
      None)
check("CP2b no mixed SUM(ledger) symbol on Overview",
      "SUM(delta)" not in body_cp, None)
check("CP2c USD formatting via money authority (millis_to_usd_str)",
      millis_to_usd_str(int(prov["provider_earn_millis"])) in body_cp,
      prov)
check("CP2d provider earnings accrue as USD-millis facts",
      int(prov["provider_earn_millis"]) > 0, prov)
check("CP2e platform income separated kinds",
      int(plat["commission_millis"]) >= 0
      and int(plat["revenue_millis"]) >= 0, plat)

# ═══ CP3: Users Credits != Earnings, separated columns ═══
grant(caller, 5000, "grant", note="s4 grant", idem_key="s4:g1")
seed_earn(provider, 1000)   # provider has earnings, caller has credits
r = client_cp.get("/samelabs/users", cookies={COOKIE_NAME: sess})
body_cp = r.text
check("CP3a Users page renders separated columns",
      "可花积分" in body_cp and "可提现" in body_cp, None)
with db() as con:
    urows, _tot = operator_views.users_page(con, _RUN, 50, 0,
                                            SPENDABLE_KINDS, now())
u_by = {u["username"]: u for u in urows}
check("CP3b caller spendable=5000, provider available separate",
      int(u_by[uname("caller")]["spendable_credits"]) == 5000
      and int(u_by[uname("prov")]["spendable_credits"]) == 0, None)
check("CP3c provider earnings NOT in Credits column",
      int(u_by[uname("prov")]["spendable_credits"]) == 0
      and u_by[uname("prov")]["provider_available_millis"] is not None,
      dict(u_by[uname("prov")]))

# ═══ CP4: generic mixed ledger retired ═══
r = client_cp.get("/samelabs/ledger", cookies={COOKIE_NAME: sess},
               follow_redirects=False)
check("CP4 /samelabs/ledger retired (404/redirect)",
      r.status_code in (404, 302, 307), r.status_code)
_src_all = ""
for root, _d, files in os.walk(os.path.join(REPO, "app")):
    for f in files:
        if f.endswith((".py", ".html")) and "__pycache__" not in root:
            _src_all += open(os.path.join(root, f),
                             encoding="utf-8", errors="ignore").read()
check("CP4b no runtime reference to /samelabs/ledger",
      "/samelabs/ledger" not in _src_all.replace(
          "economy/ledger", ""), None)

# ═══ CP5: Tool Supply facts ═══
_T_LOCAL, _T_HTTP = tkey("local"), tkey("http")
_T_PROV = f"{uname('prov')}_s4prov"      # provider key: owner/tool
with db() as con:
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, enabled, featured, sort_order, created_at, updated_at,"
        " label_json, endpoint_json, accept_json, format_family)"
        " VALUES (%s, %s, 'local', 'public', TRUE, FALSE,"
        " 0, %s, 0, '{\"zh\":\"S4L\",\"en\":\"S4L\"}', '{}', '[\".png\"]', 'image')",
        (_T_LOCAL, _T_LOCAL, ts_cp))
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, enabled, featured, sort_order, owner_id, created_at,"
        " updated_at, label_json, endpoint_json, accept_json, format_family)"
        " VALUES (%s, %s, 'http', 'public', TRUE, FALSE, 0,"
        " NULL, %s, 0, '{\"zh\":\"S4H\",\"en\":\"S4H\"}',"
        " '{\"url\":\"https://x.test\"}', '[\".png\"]', 'image')",
        (_T_HTTP, _T_HTTP, ts_cp))
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, enabled, featured, sort_order, owner_id, created_at,"
        " updated_at, label_json, endpoint_json, accept_json, format_family)"
        " VALUES (%s, 's4prov', 'http', 'public', TRUE, FALSE, 0,"
        " %s, %s, 0, '{\"zh\":\"S4P\",\"en\":\"S4P\"}',"
        " '{\"url\":\"https://y.test\"}', '[\".png\"]', 'image')",
        (_T_PROV, provider, ts_cp))
    for k in (_T_LOCAL, _T_HTTP, _T_PROV):
        con.execute("INSERT INTO pricing (tool_id, cost, enabled, updated_at)"
                    " VALUES (%s, 10, TRUE, %s)", (k, ts_cp))
r = client_cp.get("/samelabs/tools", cookies={COOKIE_NAME: sess})
body_cp = r.text
check("CP5a Tool Supply renders 3 classes",
      all(c in body_cp for c in ("Official local", "Operator HTTP",
                              "Provider Tool")), None)
check("CP5b price facts from pricing overlay",
      _T_LOCAL in body_cp and _T_HTTP in body_cp and _T_PROV in body_cp, None)

# ═══ CP6: Orders read-only ═══
with db() as con:
    # find a real package id (orders_kind_snapshot_ck: package rows must
    # carry credits_snapshot > 0)
    pkg = con.execute("SELECT id, credits FROM packages ORDER BY id"
                      " LIMIT 1").fetchone()
    _pkg_id, _pkg_credits = (int(pkg["id"]), int(pkg["credits"])) if pkg         else (1, 100)
    con.execute(
        "INSERT INTO orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " credits_snapshot, granted, created_at, paid_at)"
        " VALUES (%s, 'package', %s, 'creem', %s, 'paid', 500,"
        " 'USD', %s, TRUE, %s, %s)",
        (caller, _pkg_id, f"s4ord_{_RUN}", _pkg_credits, ts_cp, ts_cp))
r = client_cp.get("/samelabs/commerce/orders", cookies={COOKIE_NAME: sess})
check("CP6a Orders page renders facts",
      r.status_code == 200 and f"s4ord_{_RUN}" in r.text, r.status_code)
# no operator payment-status mutation endpoint exists
_methods = []
for mod in ("commerce",):
    src = open(os.path.join(REPO, "app", "routes", "samelabs",
                            f"{mod}.py"), encoding="utf-8").read()
    _methods += [l for l in src.splitlines()
                 if "@router." in l and ("post" in l or "patch" in l
                                         or "put" in l or "delete" in l)]
_mutation_ok = all("plans" in m or "packages" in m for m in _methods)
check("CP6b Orders expose zero mutation endpoints (only product edits)",
      _mutation_ok, _methods)

# ═══ CP7: Membership projection from membership row ═══
def _fresh_rank(con) -> int:
    return int(con.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM plans"
        " WHERE code <> 'free'").fetchone()["n"])


_PLAN_CODE = f"s4p{_RUN}"
_SUB = f"creem_sub_s4_{_RUN}"
with db() as con:
    pid = int(con.execute(
        "INSERT INTO plans (code, name, monthly_credits, period_days,"
        " price_cents, active, sort_order, created_at, updated_at)"
        " VALUES (%s, 'S4', 0, 30, 0, FALSE, %s, %s, %s) RETURNING id",
        (_PLAN_CODE, _fresh_rank(con), ts_cp, ts_cp)).fetchone()["id"])
    con.execute(
        "INSERT INTO memberships (user_id, plan_id,"
        " provider_subscription_id, status, started_at, renews_at)"
        " VALUES (%s, %s, %s, 'active', %s, %s)",
        (caller, pid, _SUB, ts_cp, ts_cp + 86400))
r = client_cp.get("/samelabs/commerce/memberships",
               cookies={COOKIE_NAME: sess})
check("CP7 membership row projects (not orders)",
      r.status_code == 200 and _SUB in r.text, r.status_code)

# ═══ CP8: audit page ═══
r = client_cp.get("/samelabs/audit", cookies={COOKIE_NAME: sess})
check("CP8 audit page renders withdrawal.approve facts",
      r.status_code == 200 and "withdrawal.approve" in r.text,
      r.status_code)

# audit snapshot allowlist: no payout channel / ref inside
with db() as con:
    au = con.execute(
        "SELECT before_json, after_json FROM operator_audit WHERE"
        " action='withdrawal.paid' ORDER BY id DESC LIMIT 1").fetchone()
if au:
    blobs = json.dumps(au["before_json"]) + json.dumps(au["after_json"])
    check("CP8b audit snapshots exclude payout channel/ref",
          "payout" not in blobs.lower(), au)
else:
    check("CP8b audit snapshots exclude payout channel/ref (no paid row)",
          True)

# ═══ economy pages render ═══
for path, needle in (
        ("/samelabs/economy/credits", "积分流水"),
        ("/samelabs/economy/earnings", "开发者收益"),
        ("/samelabs/economy/platform", "平台收入"),
        ("/samelabs/economy/withdrawals", "提现队列"),
        ("/samelabs/commerce/products", None),
        ("/samelabs/commerce/memberships", "会员")):
    r = client_cp.get(path, cookies={COOKIE_NAME: sess})
    ok = r.status_code == 200 and (needle is None or needle in r.text)
    check(f"CP9 {path} renders", ok, r.status_code)

# queue shows snapshot, not live channel
_LIVE = f"LIVE-CHANGED-{_RUN}"
set_channel(provider, _LIVE)
r = client_cp.get("/samelabs/economy/withdrawals?status=paid",
               cookies={COOKIE_NAME: sess})
check("CP9b queue shows request-time snapshot, not live channel",
      _CH1 in r.text and _LIVE not in r.text, None)

# old process endpoint retired
r = client_cp.post(f"/samelabs/withdrawals/{w1['id']}/process",
                json={"approve": True},
                cookies={COOKIE_NAME: sess},
                follow_redirects=False)
check("CP10 old /process bool endpoint retired",
      r.status_code in (404, 405, 302), r.status_code)

# ═══ PM-A: /api/earnings user S4 projection ═══
_json = json
prov_sess = create_browser_session(provider)
set_channel(provider, "USDT-TRC20:UserFace")
r = client_cp.get("/api/earnings", cookies={COOKIE_NAME: prov_sess})
_d = r.json() if r.status_code == 200 else {}
check("U-A1 /api/earnings exposes payout_channel",
      _d.get("payout_channel") == "USDT-TRC20:UserFace", str(_d.get("payout_channel")))
_hist = _d.get("withdrawals") or []
_snap_row = next((w for w in _hist if w.get("payout_channel_snapshot")), None)
check("U-A2 withdrawal history carries row snapshot",
      _snap_row is not None
      and _snap_row["payout_channel_snapshot"] != "USDT-TRC20:UserFace",
      None)

# ═══ PM-B: typed-input fail closed (operator commands) ═══
def _post_json(path, payload):
    return client_cp.post(path, json=payload, cookies={COOKIE_NAME: sess},
                       follow_redirects=False)

# payout_ref strict types at the paid endpoint (needs an approved row:
# seed a fresh pending → approve)
_wb = earnings.request_withdrawal(provider, 20000, "pm-b")
client_cp.post(f"/samelabs/economy/withdrawals/{_wb['id']}/approve", json={},
            cookies={COOKIE_NAME: sess})
for _bad in (123, True, None, ["TX"]):
    _r = _post_json(f"/samelabs/economy/withdrawals/{_wb['id']}/paid",
                    {"payout_ref": _bad})
    check(f"PM-B1 paid rejects payout_ref={_bad!r}",
          _r.status_code == 422, _r.status_code)
_r = _post_json(f"/samelabs/economy/withdrawals/{_wb['id']}/paid",
                {"payout_ref": "TX-123"})
check("PM-B1e valid string payout_ref passes",
      _r.status_code == 200, _r.status_code)

# credits amount strict types
for _bad in (True, 12.5, "300"):
    _r = _post_json(f"/samelabs/users/{provider}/credits",
                    {"amount": _bad, "action_id": f"pm-b{_bad!r}"[:40]})
    check(f"PM-B2 credits rejects amount={_bad!r}",
          _r.status_code == 422, _r.status_code)
_r = _post_json(f"/samelabs/users/{provider}/credits",
                {"amount": 0, "action_id": "pm-b-zero"})
check("PM-B2e credits rejects zero", _r.status_code == 422, _r.status_code)

# pricing strict types (cost bool/float/str; enabled str)
_pt = tkey("strict")
with db() as con:
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, label_json, enabled, created_at, updated_at, accept_json, format_family)"
        " VALUES (%s, %s, 'local', 'public', %s, TRUE, %s, %s, '[\".png\"]', 'image')",
        (_pt, _pt, _json.dumps({"en": "PM"}), now(), now()))
for _bad in (True, 100.9, "100"):
    _r = _post_json(f"/samelabs/tools/{_pt}/pricing", {"cost": _bad})
    check(f"PM-B3 pricing rejects cost={_bad!r}", _r.status_code == 422,
          _r.status_code)
_r = _post_json(f"/samelabs/tools/{_pt}/pricing", {"cost": 10, "enabled": "false"})
check("PM-B3e pricing rejects enabled='false' (str)", _r.status_code == 422,
      _r.status_code)
_r = _post_json(f"/samelabs/tools/{_pt}/pricing", {"cost": 10, "enabled": True})
check("PM-B3f valid pricing passes", _r.status_code == 200, _r.status_code)

# presentation strict (featured str / sort_order bool/float)
for _bad in (("featured", "yes"), ("sort_order", True), ("sort_order", 1.5)):
    _r = _post_json(f"/samelabs/tools/{_pt}/presentation", {_bad[0]: _bad[1]})
    check(f"PM-B4 presentation rejects {_bad[0]}={_bad[1]!r}",
          _r.status_code == 422, _r.status_code)

# ═══ PM-C: reporting truth ═══
from repo import operator_views as _ovm0
with db() as con:
    _base_prod = _ovm0.overview_product(con)
_BASE_OK = _base_prod["calls_ok"]
_BASE_FAILED = _base_prod["calls_failed"]
with db() as con:
    _u2 = con.execute(
        "INSERT INTO users (username, display_name, status, created_at,"
        " last_login_at) VALUES (%s, %s, 'active', %s, %s)"
        " RETURNING id", (uname("pmc"), uname("pmc"), now(),
                          now())).fetchone()["id"]
    for _st in ("ok", "running", "insufficient", "failed"):
        # running keeps the row OPEN (no finished_at) + snapshot; terminal
        # rows are closed — matching the live CHECK constraints
        if _st == "running":
            con.execute(
                "INSERT INTO tool_calls (tool_id, user_id, status,"
                " client_ref, invocation_hash, cost_snapshot,"
                " commission_rate_snapshot, started_at)"
                " VALUES ('pm-retired-tool', %s, 'running', %s, %s, 0, 0, %s)",
                (_u2, f"pmc-{_st}", "f" * 64, now()))
        else:
            con.execute(
                "INSERT INTO tool_calls (tool_id, user_id, status,"
                " client_ref, invocation_hash, cost_snapshot, started_at,"
                " finished_at) VALUES ('pm-retired-tool', %s, %s, %s, %s,"
                " 0, %s, %s)",
                (_u2, _st, f"pmc-{_st}", "f" * 64, now(), now()))
# retired tool: 1 ok (counts), 1 failed (counts), running+insufficient not Failed
# direct projection proof (shared-DB run: compare against a pre-insert
# baseline taken just before the pm-retired-tool rows went in)
from repo import operator_views as _ovm
with db() as con:
    _prod = _ovm.overview_product(con)
check("PM-C1 successful counts retired-tool ok (no catalog join)",
      int(_prod["calls_ok"]) - int(_BASE_OK) == 1,
      f"{_BASE_OK}→{_prod['calls_ok']}")
check("PM-C2 failed = status='failed' only (running/insufficient excluded)",
      int(_prod["calls_failed"]) - int(_BASE_FAILED) == 1,
      f"{_BASE_FAILED}→{_prod['calls_failed']}")

# ═════════════ B1: back-office closure ═════════════
from urllib.parse import quote as _q  # noqa: E402
from economy import creem as _creem_mod  # noqa: E402
from economy.billing import consume as _consume, refund as _refund  # noqa: E402
from repo import operator_audit as _oa  # noqa: E402
from repo.ledger import insert_row as _insert_row  # noqa: E402


def _get(path):
    return client_cp.get(path, cookies={COOKIE_NAME: sess},
                      follow_redirects=False)


def _patch_json(path, payload):
    return client_cp.patch(path, json=payload, cookies={COOKIE_NAME: sess},
                        follow_redirects=False)


def _jv(v):
    return v if isinstance(v, (dict, type(None))) else json.loads(v)


def _enc(key):
    return _q(key, safe="")


# ═══ K1: provider tool keys 'owner/tool' ═══
r = _post_json(f"/samelabs/tools/{_enc(_T_PROV)}/pricing",
               {"cost": 17, "enabled": True})
check("K1a pricing on owner/tool key (encoded %2F) → 200",
      r.status_code == 200
      and r.json().get("pricing", {}).get("tool_id") == _T_PROV,
      (r.status_code, r.text[:200]))
r = _post_json(f"/samelabs/tools/{_T_PROV}/pricing",
               {"cost": 18, "enabled": True})
with db() as con:
    _pc = con.execute("SELECT cost FROM pricing WHERE tool_id=%s",
                      (_T_PROV,)).fetchone()
check("K1b pricing on owner/tool key (raw slash) → 200 + persisted",
      r.status_code == 200 and _pc is not None and int(_pc["cost"]) == 18,
      (r.status_code, _pc))
r = _post_json(f"/samelabs/tools/{_enc(_T_PROV)}/presentation",
               {"featured": True, "sort_order": 3})
with db() as con:
    _pr = con.execute("SELECT featured, sort_order FROM platform_tools"
                      " WHERE tool_key=%s", (_T_PROV,)).fetchone()
check("K1c presentation on owner/tool key → 200 + persisted",
      r.status_code == 200 and _pr["featured"] is True
      and int(_pr["sort_order"]) == 3, (r.status_code, _pr))
r = _post_json(f"/samelabs/tools/{_enc('nobody_' + _RUN + '/none')}/pricing",
               {"cost": 1, "enabled": True})
check("K1d unknown owner/tool key → 404", r.status_code == 404,
      r.status_code)
_js_tools = open(os.path.join(REPO, "static", "pages", "samelabs_tools.js"),
                 encoding="utf-8").read()
check("K1e tools JS builds every tool URL via encodeURIComponent",
      "encodeURIComponent(key)" in _js_tools
      and "'/samelabs/tools/' + tool" not in _js_tools
      and "'/samelabs/tools/' + b.dataset" not in _js_tools, None)

# ═══ E1: operator delist switch (system / operator / provider rows) ═══
for _k, _cls in ((_T_LOCAL, "system"), (_T_HTTP, "operator"),
                 (_T_PROV, "provider")):
    r = _post_json(f"/samelabs/tools/{_enc(_k)}/delist",
                   {"delisted": True})
    with db() as con:
        _en = con.execute("SELECT delisted FROM platform_tools"
                          " WHERE tool_key=%s", (_k,)).fetchone()["delisted"]
        _au = con.execute(
            "SELECT before_json, after_json, actor_user_id FROM"
            " operator_audit WHERE action='tool.delisted.update'"
            " AND target_type='tool' AND target_id=%s ORDER BY id DESC"
            " LIMIT 1", (_k,)).fetchone()
    check(f"E1a {_cls} row delist → delisted=true + audit before/after",
          r.status_code == 200 and _en is True and _au is not None
          and _jv(_au["before_json"]) == {"tool_key": _k, "delisted": False}
          and _jv(_au["after_json"]) == {"tool_key": _k, "delisted": True}
          and int(_au["actor_user_id"]) == op_cp,
          (r.status_code, _en, _au and dict(_au)))
r = _post_json(f"/samelabs/tools/{_enc(_T_PROV)}/delist",
               {"delisted": False})
with db() as con:
    _en = con.execute("SELECT delisted FROM platform_tools WHERE tool_key=%s",
                      (_T_PROV,)).fetchone()["delisted"]
check("E1b restore → delisted=false", r.status_code == 200 and _en is False,
      (r.status_code, _en))
for _bad in ("false", 0, None):
    r = _post_json(f"/samelabs/tools/{_enc(_T_PROV)}/delist",
                   {"delisted": _bad})
    check(f"E1c delisted={_bad!r} rejected 422", r.status_code == 422,
          r.status_code)
r = _post_json(f"/samelabs/tools/{_enc('nobody_' + _RUN)}/delist",
                {"delisted": True})
check("E1d unknown key → 404", r.status_code == 404, r.status_code)
# one transaction: forced audit failure rolls the switch back
_orig_record = _oa.record


def _boom(*a, **k):
    raise RuntimeError("forced audit failure")


_oa.record = _boom
try:
    try:
        r = _post_json(f"/samelabs/tools/{_enc(_T_PROV)}/delist",
                       {"delisted": True})
        _st = r.status_code
    except Exception:
        _st = 500
finally:
    _oa.record = _orig_record
with db() as con:
    _en = con.execute("SELECT delisted FROM platform_tools WHERE tool_key=%s",
                      (_T_PROV,)).fetchone()["delisted"]
check("E1e audit failure → switch rolled back (same transaction)",
      _st == 500 and _en is False, (_st, _en))
r = _get("/samelabs/tools")
check("E1f Tool Supply renders a registry switch per row (incl. provider"
      " key, escaped)",
      r.status_code == 200
      and f'data-toggle-delisted="{_T_PROV}"' in r.text
      and 'data-toggle-delisted="' in r.text, r.status_code)

# ═══ PL1: PATCH plan DB conflicts → typed 409 ═══
with db() as con:
    _rank = _fresh_rank(con)
_ra = _post_json("/samelabs/commerce/products/plans",
                 {"code": f"{_PLAN_CODE}a", "name": "S4A",
                  "monthly_credits": 0, "price_cents": 0,
                  "sort_order": _rank})
_rb = _post_json("/samelabs/commerce/products/plans",
                 {"code": f"{_PLAN_CODE}b", "name": "S4B",
                  "monthly_credits": 0, "price_cents": 0,
                  "sort_order": _rank + 1})
check("PL1 fixtures: two plans created",
      _ra.status_code == 200 and _rb.status_code == 200,
      (_ra.status_code, _ra.text[:200], _rb.status_code, _rb.text[:200]))
_pa = int(_ra.json()["plan"]["id"]) if _ra.status_code == 200 else 0
_pb = int(_rb.json()["plan"]["id"]) if _rb.status_code == 200 else 0
r = _patch_json(f"/samelabs/commerce/products/plans/{_pb}",
                {"sort_order": _rank})
check("PL1a PATCH duplicate sort_order → 409 sort_order_taken",
      r.status_code == 409 and r.json().get("error") == "sort_order_taken",
      (r.status_code, r.text[:200]))
_prod = f"prod_s4_{_RUN}"
with db() as con:
    con.execute("UPDATE plans SET creem_product_id=%s WHERE id=%s",
                (_prod, _pa))
r = _patch_json(f"/samelabs/commerce/products/plans/{_pb}",
                {"creem_product_id": _prod})
with db() as con:
    _bp = con.execute("SELECT creem_product_id, sort_order FROM plans"
                      " WHERE id=%s", (_pb,)).fetchone()
check("PL1b PATCH already-bound product → 409 product_already_bound",
      r.status_code == 409
      and r.json().get("error") == "product_already_bound",
      (r.status_code, r.text[:200]))
check("PL1c conflicting PATCHes left the row unchanged",
      _bp["creem_product_id"] is None and int(_bp["sort_order"]) == _rank + 1,
      dict(_bp))

# ═══ PF1: price floor (USD only, credits <= price_cents * 10) ═══
def _pk(n):
    return f"s4k{_RUN}{n}"


_PKG_URL = "/samelabs/commerce/products/packages"
r = _post_json(_PKG_URL, {"code": _pk("a"), "name": "A", "credits": 1001,
                          "price_cents": 100})
check("PF1a package 1001 Credits @ $1 → 422 credits_per_usd_exceeded",
      r.status_code == 422
      and r.json().get("error") == "credits_per_usd_exceeded",
      (r.status_code, r.text[:200]))
r = _post_json(_PKG_URL, {"code": _pk("z"), "name": "Z", "credits": 1,
                          "price_cents": 0})
check("PF1b package with Credits @ $0 → 422",
      r.status_code == 422
      and r.json().get("error") == "credits_per_usd_exceeded", r.status_code)
r = _post_json(_PKG_URL, {"code": _pk("a"), "name": "A", "credits": 1000,
                          "price_cents": 100})
check("PF1c package exactly 1000 Credits/USD → 200", r.status_code == 200,
      (r.status_code, r.text[:200]))
_pkid = int(r.json()["package"]["id"]) if r.status_code == 200 else 0
r = _patch_json(f"{_PKG_URL}/{_pkid}", {"credits": 1001})
with db() as con:
    _pkr = con.execute("SELECT credits, price_cents FROM packages WHERE"
                       " id=%s", (_pkid,)).fetchone()
check("PF1d package PATCH above floor → 422, row unchanged",
      r.status_code == 422 and int(_pkr["credits"]) == 1000,
      (r.status_code, dict(_pkr)))
r = _patch_json(f"{_PKG_URL}/{_pkid}", {"name": "A2"})
check("PF1e package name-only PATCH → 200", r.status_code == 200,
      r.status_code)
r = _patch_json(f"{_PKG_URL}/{_pkid}", {"credits": 2000,
                                        "price_cents": 200})
check("PF1f package PATCH 2000 @ $2 → 200", r.status_code == 200,
      (r.status_code, r.text[:200]))
_orig_cur = _creem_mod.CREEM_CREDITS_CURRENCY
try:
    _creem_mod.CREEM_CREDITS_CURRENCY = "EUR"
    r = _post_json(_PKG_URL, {"code": _pk("b"), "name": "B", "credits": 10,
                              "price_cents": 100})
    with db() as con:
        _nb = con.execute("SELECT COUNT(*) n FROM packages WHERE code=%s",
                          (_pk("b"),)).fetchone()["n"]
    check("PF1g configured non-USD package currency → create 422"
          " currency_not_usd, zero rows",
          r.status_code == 422 and r.json().get("error") == "currency_not_usd"
          and int(_nb) == 0, (r.status_code, r.text[:200], _nb))
    r = _patch_json(f"{_PKG_URL}/{_pkid}", {"price_cents": 300})
    check("PF1h configured non-USD currency → price PATCH 422",
          r.status_code == 422
          and r.json().get("error") == "currency_not_usd", r.status_code)
finally:
    _creem_mod.CREEM_CREDITS_CURRENCY = _orig_cur
with db() as con:
    con.execute("UPDATE packages SET currency='EUR' WHERE id=%s", (_pkid,))
r = _patch_json(f"{_PKG_URL}/{_pkid}", {"price_cents": 300})
check("PF1i non-USD package row → price PATCH 422 currency_not_usd",
      r.status_code == 422 and r.json().get("error") == "currency_not_usd",
      r.status_code)
_PLAN_URL = "/samelabs/commerce/products/plans"
r = _post_json(_PLAN_URL, {"code": f"{_PLAN_CODE}c", "name": "S4C",
                           "monthly_credits": 5000, "price_cents": 100,
                           "sort_order": _rank + 2})
check("PF1j plan 5000 Credits @ $1 → 422 credits_per_usd_exceeded",
      r.status_code == 422
      and r.json().get("error") == "credits_per_usd_exceeded",
      (r.status_code, r.text[:200]))
r = _post_json(_PLAN_URL, {"code": f"{_PLAN_CODE}c", "name": "S4C",
                           "monthly_credits": 1000, "price_cents": 100,
                           "sort_order": _rank + 2})
check("PF1k plan 1000 Credits @ $1 → 200", r.status_code == 200,
      (r.status_code, r.text[:200]))
_pc_id = int(r.json()["plan"]["id"]) if r.status_code == 200 else 0
r = _patch_json(f"{_PLAN_URL}/{_pc_id}", {"monthly_credits": 1001})
check("PF1l plan PATCH above floor → 422", r.status_code == 422
      and r.json().get("error") == "credits_per_usd_exceeded", r.status_code)
r = _patch_json(f"{_PLAN_URL}/{_pc_id}", {"name": "S4C2"})
check("PF1m plan name-only PATCH → 200", r.status_code == 200,
      r.status_code)
with db() as con:
    con.execute("UPDATE plans SET currency='EUR' WHERE id=%s", (_pc_id,))
r = _patch_json(f"{_PLAN_URL}/{_pc_id}", {"price_cents": 200})
check("PF1n non-USD plan row → price PATCH 422 currency_not_usd",
      r.status_code == 422 and r.json().get("error") == "currency_not_usd",
      r.status_code)
with db() as con:
    con.execute("UPDATE plans SET currency='USD' WHERE id=%s", (_pc_id,))
r = _get("/samelabs/commerce/products")
check("PF1o products page injects plan/package data for the editors",
      r.status_code == 200 and "window.__SL_PLANS__ = [" in r.text
      and "window.__SL_PKGS__ = [" in r.text and f"{_PLAN_CODE}c" in r.text,
      r.status_code)

# ═══ CR1: credits adjust typed errors + action-id conflict ═══
_CR = "/samelabs/users/{}/credits"
r = _post_json(_CR.format(1 << 62), {"amount": 5, "action_id": f"cr{_RUN}a"})
check("CR1a nonexistent user → 404 user_not_found",
      r.status_code == 404 and r.json().get("error") == "user_not_found",
      (r.status_code, r.text[:200]))
r = _post_json(_CR.format("99999999999999999999"),
               {"amount": 5, "action_id": f"cr{_RUN}a"})
check("CR1b out-of-BIGINT user id → 404", r.status_code == 404,
      r.status_code)
for _amt in (10 ** 12, -(10 ** 12), 10 ** 30):
    r = _post_json(_CR.format(caller), {"amount": _amt,
                                        "action_id": f"cr{_RUN}big"})
    check(f"CR1c amount={_amt} → 422 amount_out_of_range",
          r.status_code == 422
          and r.json().get("error") == "amount_out_of_range",
          (r.status_code, r.text[:200]))
_aid = f"cr{_RUN}b"
r1 = _post_json(_CR.format(caller), {"amount": 40, "action_id": _aid})
r2 = _post_json(_CR.format(caller), {"amount": 40, "action_id": _aid})
r3 = _post_json(_CR.format(caller), {"amount": 400, "action_id": _aid})
with db() as con:
    _lr = con.execute("SELECT id, delta FROM ledger WHERE user_id=%s AND"
                      " idem_key=%s", (caller, f"adjust:{op_cp}:{_aid}")
                      ).fetchall()
    _an = con.execute("SELECT COUNT(*) n FROM operator_audit WHERE"
                      " action='credits.adjust' AND target_id=%s AND"
                      " (after_json::jsonb->>'ledger_id')::bigint = %s",
                      (str(caller), int(_lr[0]["id"]) if _lr else 0)
                      ).fetchone()["n"]
check("CR1d first apply → 200 replayed=false",
      r1.status_code == 200 and r1.json().get("replayed") is False,
      r1.text[:200])
check("CR1e same id + same amount → 200 replayed=true, same balance",
      r2.status_code == 200 and r2.json().get("replayed") is True
      and r2.json().get("balance_after") == r1.json().get("balance_after"),
      r2.text[:200])
check("CR1f same id + different amount → 409 action_id_conflict",
      r3.status_code == 409
      and r3.json().get("error") == "action_id_conflict", r3.text[:200])
check("CR1g exactly one ledger row (+40) and one audit fact",
      len(_lr) == 1 and int(_lr[0]["delta"]) == 40 and int(_an) == 1,
      ([dict(x) for x in _lr], _an))
_js_users = open(os.path.join(REPO, "static", "pages", "samelabs_users.js"),
                 encoding="utf-8").read()
check("CR1h users JS handles action_id_conflict + replayed distinctly",
      "action_id_conflict" in _js_users and "r.data.replayed" in _js_users,
      None)

# ═══ US1: user search + read-only detail ═══
_sx = mkuser(uname("srch"), display_name=f"Dn{_RUN}Zed",
             email=f"s4{_RUN}@example.test")
for _qv, _lbl in ((f"dn{_RUN}z", "display name (ci)"),
                  (f"S4{_RUN}@EXAMPLE", "email (ci)"),
                  (str(_sx), "numeric id"),
                  (uname("srch"), "username")):
    r = _get(f"/samelabs/users?q={_q(_qv)}")
    check(f"US1a search by {_lbl}",
          r.status_code == 200 and uname("srch") in r.text, r.status_code)
with db() as con:
    _all, _n_all = operator_views.users_page(con, None, 1, 0,
                                             SPENDABLE_KINDS, now())
    _pct, _n_pct = operator_views.users_page(con, "%", 1, 0,
                                             SPENDABLE_KINDS, now())
    _real_pct = con.execute(
        "SELECT COUNT(*) n FROM users WHERE strpos(username, chr(37)) > 0"
        " OR strpos(display_name, chr(37)) > 0"
        " OR strpos(COALESCE(email, ''), chr(37)) > 0").fetchone()["n"]
check("US1b LIKE metacharacters in q match literally",
      _n_pct == int(_real_pct) and _n_pct < _n_all, (_n_pct, _real_pct))
# expired + never-paid memberships on the suite's own plan
_ex = mkuser(uname("exp"))
_ns = mkuser(uname("nostart"))
with db() as con:
    _base_eff = int(operator_views.overview_consumer(
        con, SPENDABLE_KINDS, now())["memberships_effective"])
    con.execute("INSERT INTO memberships (user_id, plan_id,"
                " provider_subscription_id, status, started_at, renews_at)"
                " VALUES (%s, %s, %s, 'active', %s, %s)",
                (_ex, pid, f"{_SUB}_exp", ts_cp - 90000, ts_cp - 100))
    con.execute("INSERT INTO memberships (user_id, plan_id,"
                " provider_subscription_id, status, started_at)"
                " VALUES (%s, %s, %s, 'active', %s)",
                (_ns, pid, f"{_SUB}_ns", ts_cp))
r = _get(f"/samelabs/users/{_ex}")
check("US1c detail: lapsed membership shows effective status 'expired'",
      r.status_code == 200 and ">expired<" in r.text
      and "（记录态：active）" in r.text, r.status_code)
with db() as con:
    _cp = operator_views.user_profile(con, caller, SPENDABLE_KINDS, now())
    _pp = operator_views.user_profile(con, provider, SPENDABLE_KINDS, now())
r = _get(f"/samelabs/users/{caller}")
check("US1d detail (caller): spendable, membership, orders, ledger",
      r.status_code == 200
      and f"{int(_cp['spendable_credits']):,}" in r.text
      and _SUB in r.text and f"s4ord_{_RUN}" in r.text
      and "adjust" in r.text and _cp["membership_effective"] is True,
      r.status_code)
r = _get(f"/samelabs/users/{provider}")
check("US1e detail (provider): USD balances + withdrawal snapshots +"
      " provider_earn rows",
      r.status_code == 200
      and millis_to_usd_str(int(_pp["provider_available_millis"])) in r.text
      and _CH1 in r.text and "provider_earn" in r.text, r.status_code)
for _bad in ((1 << 62), "99999999999999999999"):
    r = _get(f"/samelabs/users/{_bad}")
    check(f"US1f detail unknown id {str(_bad)[:8]}… → 404",
          r.status_code == 404, r.status_code)

# ═══ RT1: reporting label truth ═══
with db() as con:
    _eff_now = int(operator_views.overview_consumer(
        con, SPENDABLE_KINDS, now())["memberships_effective"])
check("RT1a lapsed / never-paid memberships not counted as effective",
      _eff_now == _base_eff, (_base_eff, _eff_now))
_ef = mkuser(uname("eff"))
with db() as con:
    con.execute("INSERT INTO memberships (user_id, plan_id,"
                " provider_subscription_id, status, started_at, renews_at)"
                " VALUES (%s, %s, %s, 'canceled', %s, %s)",
                (_ef, pid, f"{_SUB}_eff", ts_cp, ts_cp + 86400))
    _eff2 = int(operator_views.overview_consumer(
        con, SPENDABLE_KINDS, now())["memberships_effective"])
check("RT1b canceled-but-paid-through membership counts as effective",
      _eff2 == _base_eff + 1, (_base_eff, _eff2))
with db() as con:
    _urows, _ = operator_views.users_page(con, _RUN, 100, 0,
                                          SPENDABLE_KINDS, now())
_ub = {u["username"]: u for u in _urows}
check("RT1c users projection: lapsed → not effective, status expired",
      _ub[uname("exp")]["membership_effective"] is False
      and _ub[uname("exp")]["membership_status"] == "expired"
      and _ub[uname("nostart")]["membership_status"] == "not_started"
      and _ub[uname("caller")]["membership_effective"] is True, None)
r = _get(f"/samelabs/users?q={_q(uname('exp'))}")
check("RT1d Users 'Plan' column never shows a lapsed plan as current",
      f"<td>{_PLAN_CODE}</td>" not in r.text
      and f"（{_PLAN_CODE} expired）" in r.text, None)
r = _get(f"/samelabs/users?q={_q(uname('caller'))}")
check("RT1e Users 'Plan' column shows an effective plan",
      f"<td>{_PLAN_CODE}</td>" in r.text, None)
with db() as con:
    _c0 = int(operator_views.overview_consumer(
        con, SPENDABLE_KINDS, now())["credits_charged_net"])
_ch1 = _consume(caller, 30, "consume", ref_type="tool_call",
                ref_id=f"s4rt1{_RUN}", idem_key=f"call:s4rt1:{_RUN}")
_refund(caller, 30, ref_type="ledger", ref_id=str(_ch1["id"]),
        idem_key=f"refund:ledger:{_ch1['id']}")
_consume(caller, 20, "consume", ref_type="tool_call",
         ref_id=f"s4rt2{_RUN}", idem_key=f"call:s4rt2:{_RUN}")
with db() as con:
    _c1 = int(operator_views.overview_consumer(
        con, SPENDABLE_KINDS, now())["credits_charged_net"])
    _pv = dict(operator_views.overview_provider(con))
    _sums = {st: int(con.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE status=%s",
        (st,)).fetchone()["s"]) for st in ("approved", "paid")}
check("RT1f Tool Credits charged is net of refunds (+30 −30 +20 = +20)",
      _c1 - _c0 == 20, (_c0, _c1))
check("RT1g approved-unpaid and paid withdrawals are separate facts",
      int(_pv["wd_approved_unpaid_millis"]) == _sums["approved"]
      and int(_pv["wd_paid_millis"]) == _sums["paid"], (_pv, _sums))
r = _get("/samelabs")
check("RT1h Overview labels: net-of-refunds, approved-unpaid vs paid,"
      " effective memberships",
      r.status_code == 200
      and "净消耗（扣除退款）" in r.text
      and "已批准未打款" in r.text
      and "已打款" in r.text
      and "有效会员" in r.text
      and millis_to_usd_str(int(_pv["wd_paid_millis"])) in r.text, None)

# ═══ WQ1: withdrawal queue paging + status filter ═══
_wp = earnings.request_withdrawal(provider, 10000, note="wq1")
with db() as con:
    _q1, _t1 = economy_repo.withdrawals_queue(con, 2, 0, "rejected")
    _q2, _t2 = economy_repo.withdrawals_queue(con, 2, 2, "rejected")
    _tr = int(con.execute("SELECT COUNT(*) n FROM withdrawals WHERE"
                          " status='rejected'").fetchone()["n"])
check("WQ1a repo paging: status filter exact, total = filtered count,"
      " pages disjoint",
      len(_q1) <= 2 and all(x["status"] == "rejected" for x in _q1 + _q2)
      and _t1 == _tr == _t2 and _tr >= 2
      and not ({x["id"] for x in _q1} & {x["id"] for x in _q2}),
      (_t1, _tr))
r = _get("/samelabs/economy/withdrawals?status=pending")
check("WQ1b status=pending shows pending rows only",
      r.status_code == 200 and f'data-wid="{_wp["id"]}"' in r.text
      and f'data-wid="{w1["id"]}"' not in r.text, r.status_code)
r = _get("/samelabs/economy/withdrawals?status=paid")
check("WQ1c status=paid shows the paid row, not pending",
      r.status_code == 200 and f'data-wid="{w1["id"]}"' in r.text
      and f'data-wid="{_wp["id"]}"' not in r.text, r.status_code)
r = _get("/samelabs/economy/withdrawals?status=bogus&page=2")
check("WQ1d unknown status → unfiltered view, page 2 renders",
      r.status_code == 200, r.status_code)
earnings.reject_withdrawal(_wp["id"], admin_user_id=op_cp)

# ═══ X1: hostile values render escaped on every operator page ═══
_X = f"s4x{_RUN}"
_PAY = f'"><script>{_X}</script>'
_IMG = f"<img src=x onerror=alert('{_X}')>"
_RAW = (f"<script>{_X}", f"<img src=x onerror=alert('{_X}')",
        f'"><b>{_X}')
_xu = mkuser(_PAY, display_name=_IMG, email=f"{_X}@x.test")
_EXTRA_USERS.append(_xu)
set_channel(_xu, _IMG + "CH")
seed_earn(_xu, 50000)
grant(_xu, 10, "grant", note=_IMG, idem_key=f"x:g:{_RUN}")
with db() as con:
    _insert_row(con, _xu, 5, 5, "platform_commission", None, None,
                f"x:pc:{_RUN}", _IMG, ts_cp)
    _insert_row(con, _xu, 1000, 51000, "provider_earn", "tool_call",
                _PAY, f"x:pe:{_RUN}", _IMG, ts_cp)
_xw = earnings.request_withdrawal(_xu, 10000, note="x1")
earnings.approve_withdrawal(_xw["id"], admin_user_id=op_cp)
earnings.mark_withdrawal_paid(_xw["id"], _IMG, admin_user_id=op_cp)
with db() as con:
    con.execute(
        "INSERT INTO orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " credits_snapshot, granted, created_at, paid_at)"
        " VALUES (%s, 'package', %s, 'creem', %s, 'paid', 500,"
        " 'USD', %s, TRUE, %s, %s)",
        (_xu, _pkg_id, _PAY, _pkg_credits, ts_cp, ts_cp))
    con.execute(
        "INSERT INTO memberships (user_id, plan_id,"
        " provider_subscription_id, status, started_at, renews_at)"
        " VALUES (%s, %s, %s, 'active', %s, %s)",
        (_xu, pid, _IMG, ts_cp, ts_cp + 86400))
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, label_json, accept_json, format_family, enabled, owner_id, created_at,"
        " updated_at) VALUES (%s, 'xt', 'http', 'public', %s, '[\".png\"]', 'image', TRUE,"
        " %s, %s, %s)",
        (f'{_X}/"><b>{_X}', _json.dumps({"en": "X"}), _xu, ts_cp, ts_cp))
    con.execute(
        "INSERT INTO tool_calls (tool_id, user_id, status, client_ref,"
        " invocation_hash, cost_snapshot, started_at, finished_at)"
        " VALUES (%s, %s, 'ok', %s, %s, 0, %s, %s)",
        (_PAY, _xu, f"x-{_RUN}", "f" * 64, now(), now()))
r = _post_json(_PLAN_URL, {"code": f"{_PLAN_CODE}x", "name": _PAY[:64],
                           "monthly_credits": 0, "price_cents": 0,
                           "sort_order": _rank + 3})
check("X1 fixture: hostile-named plan created (audit carries the name)",
      r.status_code == 200, (r.status_code, r.text[:200]))
_pages = [
    "/samelabs",
    f"/samelabs/users?q={_q(_PAY)}",
    f"/samelabs/users?q={_q(_RUN)}",
    f"/samelabs/users/{_xu}",
    "/samelabs/tools",
    "/samelabs/commerce/orders",
    "/samelabs/commerce/memberships",
    "/samelabs/commerce/products",
    f"/samelabs/economy/credits?user_id={_xu}",
    f"/samelabs/economy/credits?user_id={_xu}&kind={_q(_PAY)}",
    "/samelabs/economy/earnings",
    "/samelabs/economy/platform",
    f"/samelabs/economy/withdrawals?status={_q(_PAY)}",
    "/samelabs/economy/withdrawals?status=paid",
    "/samelabs/audit",
]
for _pth in _pages:
    r = _get(_pth)
    _hit = [p for p in _RAW if p in r.text]
    check(f"X1 {_pth.split('?')[0]}{'?…' if '?' in _pth else ''}"
          f" renders hostile values escaped",
          r.status_code == 200 and not _hit, (r.status_code, _hit))
r = _get(f"/samelabs/users?q={_q(_PAY)}")
check("X1b reflected q is present only in escaped form",
      "&#34;&gt;&lt;script&gt;" in r.text, None)
r = _get(f"/samelabs/users?q={_q(_RUN)}")
check("X1c hostile username listed (escaped) — check is not vacuous",
      f"&lt;script&gt;{_X}&lt;/script&gt;" in r.text, None)
r = _get("/samelabs/audit")
check("X1d audit JSON with hostile plan name is escaped",
      "plan.create" in r.text and f"&lt;script&gt;{_X}" in r.text, None)

# ═══ NC1: 调用净扣费投影 (CALL_NET_CHARGE_SQL) — 五态语义 + 投影一致 ═══
# 余额不足=0 / 扣款前失败=0 / 成功=价格 / 失败已退款=0 / running=价格;
# 四个后台投影 + 账户调用页同一数值。
import io as _io_nc
import uuid as _uuid_nc
from repo.ledger import CALL_NET_CHARGE_SQL as _NC_SQL
_nc_tag = _uuid_nc.uuid4().hex[:6]
_nc_tool = f"nc{_RUN}{_nc_tag}"
_nc_user = None
with db() as con:
    _nc_user = con.execute(
        "insert into users (username, email, password_hash, display_name,"
        " status, created_at, last_login_at) values (%s,%s,'x','NC',"
        "'active',%s,%s) returning id",
        (f"ncu{_nc_tag}", f"ncu{_nc_tag}@t.co", ts_cp, ts_cp)).fetchone()["id"]
    con.execute(
        "insert into platform_tools (tool_key, tool_id, execution,"
        " visibility, label_json, accept_json, format_family, enabled,"
        " created_at, updated_at) values (%s, %s, 'local', 'public',"
        " %s, '[\".png\"]', 'image', TRUE, %s, %s)",
        (_nc_tool, _nc_tool, '{"en": "NC"}', ts_cp, ts_cp))

def _nc_call(status):
    with db() as con:
        if status == "running":
            return con.execute(
                "insert into tool_calls (tool_id, user_id, status,"
                " client_ref, invocation_hash, cost_snapshot,"
                " commission_rate_snapshot, started_at)"
                " values (%s, %s, 'running', %s, %s, 40, 30, %s)"
                " returning id",
                (_nc_tool, _nc_user, f"nc-{status}-{_nc_tag}",
                 "e" * 64, ts_cp)).fetchone()["id"]
        return con.execute(
            "insert into tool_calls (tool_id, user_id, status, client_ref,"
            " invocation_hash, cost_snapshot, started_at, finished_at)"
            " values (%s, %s, %s, %s, %s, 40, %s, %s) returning id",
            (_nc_tool, _nc_user, status, f"nc-{status}-{_nc_tag}",
             "e" * 64, ts_cp, ts_cp)).fetchone()["id"]

def _nc_ledger(kind, delta, ref_type, ref_id):
    with db() as con:
        con.execute(
            "insert into ledger (user_id, delta, balance_after, kind,"
            " ref_type, ref_id, created_at) values (%s, %s, 0, %s, %s, %s,"
            " %s)", (_nc_user, delta, kind, ref_type, ref_id, ts_cp))

_c_ins = _nc_call("insufficient")            # 余额不足: 无 consume
_c_pre = _nc_call("failed")                  # 扣款前失败: 无 consume
_c_ok = _nc_call("ok")
_nc_ledger("consume", -40, "tool_call", str(_c_ok))
_c_ref = _nc_call("failed")
with db() as con:
    _chg = con.execute(
        "insert into ledger (user_id, delta, balance_after, kind, ref_type,"
        " ref_id, created_at) values (%s, -40, 0, 'consume', 'tool_call',"
        " %s, %s) returning id", (_nc_user, str(_c_ref), ts_cp)).fetchone()["id"]
    con.execute(
        "insert into ledger (user_id, delta, balance_after, kind, ref_type,"
        " ref_id, created_at) values (%s, 40, 0, 'refund', 'ledger', %s, %s)",
        (_nc_user, str(_chg), ts_cp))
_c_run = _nc_call("running")
with db() as con:
    con.execute(
        "insert into ledger (user_id, delta, balance_after, kind, ref_type,"
        " ref_id, created_at) values (%s, -40, 0, 'consume', 'tool_call',"
        " %s, %s)", (_nc_user, str(_c_run), ts_cp))

_expect = {str(_c_ins): 0, str(_c_pre): 0, str(_c_ok): 40,
           str(_c_ref): 0, str(_c_run): 40}
_ok_direct = True
with db() as con:
    for _cid, _want in _expect.items():
        _got = con.execute(
            "select ({_NC_SQL}) as n from tool_calls tc where tc.id = %s"
            .replace("{_NC_SQL}", _NC_SQL), (_cid,)).fetchone()["n"]
        _ok_direct = _ok_direct and int(_got) == _want
check("NC1 net-charge SQL semantics (insufficient/pre-fail/ok/refunded/running)",
      _ok_direct, str(_expect))

_proj_vals = {}
with db() as con:
    for r_ in operator_views.user_recent_tool_calls(con, _nc_user, 20):
        _proj_vals[str(r_["id"])] = int(r_["net_charge"])
    _trows = operator_views.tool_recent_calls(con, _nc_tool, 20)
    for r_ in _trows:
        _proj_vals.setdefault(str(r_["id"]), int(r_["net_charge"]))
    _cprows, _cptot = operator_views.calls_page(
        con, _nc_tool, None, None, None, None, None, 50, 0)
    for r_ in _cprows:
        if r_["user_id"] == _nc_user:
            _proj_vals.setdefault(str(r_["id"]), int(r_["net_charge"]))
    _crrows, _crtot = operator_views.content_results(
        con, None, f"ncu{_nc_tag}", _nc_tool, 50, 0)
    for r_ in _crrows:
        _proj_vals.setdefault(str(r_["id"]), int(r_["net_charge"]))
check("NC2 four operator projections agree on net_charge",
      all(_proj_vals.get(k) == v for k, v in _expect.items())
      and len(_proj_vals) == 5, str(_proj_vals))

_nc_sess = create_browser_session(_nc_user)
_nc_page = client_cp.get("/account/calls", cookies={COOKIE_NAME: _nc_sess})
import re as _re_nc
_nc_cells = [int(x.replace(",", ""))
             for x in _re_nc.findall(r'ta-r mono">([\d,]+)',
                                     _nc_page.text or "")]
check("NC3 account calls page renders the same net values (desc order)",
      _nc_page.status_code == 200
      and _nc_cells[:5] == [40, 0, 40, 0, 0],
      f"{_nc_page.status_code} cells={_nc_cells[:6]}")

# ═══ NC4 (F17): 后台内容页 downloads-remaining 与 fake Gokapi 同步 ═══
from storage import write as _nc_write
_gk_nc = _nc_write(_io_nc.BytesIO(b"nc-live"), "nc.txt", "text/plain",
                   7, 50, 7)
_gid_nc = (_gk_nc.get("FileInfo", {}) or {}).get("Id")
with db() as con:
    from utils import generate_public_id as _gpid
    _pid_nc = _gpid(con)
    _udl_nc = (_gk_nc.get("FileInfo", {}) or {}).get("UrlDownload") or ""
    con.execute(
        "insert into uploads (user_id, ip, public_id, gokapi_id, file_name,"
        " content_type, size_bytes, expiry_days, allowed_downloads,"
        " expire_at, url_download, created_at) values"
        " (%s, '127.0.0.1', %s, %s, 'nc.txt', 'text/plain', 7, 7, 50, %s,"
        " %s, %s)",
        (_nc_user, _pid_nc, _gid_nc, ts_cp + 7 * 86400, _udl_nc, ts_cp))
from sharing.uploads import live_status_for as _lsf_nc
def _lsm_fresh_nc():
    return _lsf_nc([_gid_nc])
_pre_rem = _lsm_fresh_nc().get(_gid_nc)
# the counted channel is the storage's own /downloadFile (production
# Nginx fronts it; the test stack hits the fake directly)
import urllib.request as _ur_nc
import os as _os_nc
try:
    _ur_nc.urlopen(
        _os_nc.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
        + f"/downloadFile?id={_ur_nc.quote(_gid_nc)}", timeout=10).read()
except Exception:
    pass
_post_rem = _lsm_fresh_nc().get(_gid_nc)
_cp = client_cp.get(f"/samelabs/content?q_share={_pid_nc}",
                 cookies={COOKIE_NAME: sess})
def _rem_nc(m):
    return int(m["DownloadsRemaining"]) if m else None
check("NC4 content page remaining syncs with the live storage counter",
      _rem_nc(_pre_rem) == 50 and _rem_nc(_post_rem) == 49
      and "49" in (_cp.text or ""),
      f"pre={_rem_nc(_pre_rem)} post={_rem_nc(_post_rem)}"
      f" status={_cp.status_code}")

# ═══ NC5 (S7): operator 工具创建的可选绑定字段类型口径 ═══
def _op_tool_create(payload):
    return _post_json("/samelabs/tools/create", payload)

_s7 = f"s7tool{_RUN}{_nc_tag}"
r = _op_tool_create({
    "tool_id": _s7, "label": "S7", "format_families": "image",
    "accept": ".png", "url": "https://93.184.216.34/s7",
    "timeout_s": 30, "files_min": 1, "files_max": 2,
    "outputs": ["text"], "cost": 0,
    "auth_header": None, "auth_token": None})
check("NC5 null auth binding → accepted as empty",
      getattr(r, "status_code", 500) == 200, getattr(r, "status_code", 0))
r = _op_tool_create({
    "tool_id": _s7 + "b", "label": "S7", "format_families": "image",
    "accept": ".png", "url": "https://93.184.216.34/s7",
    "timeout_s": 30, "files_min": 1, "files_max": 2,
    "outputs": ["text"], "cost": 0,
    "auth_header": 123})
check("NC5b non-string auth_header → 422 bad_binding",
      getattr(r, "status_code", 500) == 422
      and r.json().get("error") == "bad_binding",
      getattr(r, "status_code", 0))
r = _op_tool_create({
    "tool_id": _s7 + "c", "label": "S7", "format_families": "image",
    "accept": 123, "url": "https://93.184.216.34/s7",
    "timeout_s": 30, "files_min": 1, "files_max": 2,
    "outputs": ["text"], "cost": 0})
check("NC5c non-string accept → 422 bad_accept",
      getattr(r, "status_code", 500) == 422
      and r.json().get("error") == "bad_accept",
      getattr(r, "status_code", 0))

# NC cleanup (own fixtures, before H1)
with db() as con:
    con.execute("delete from tool_calls where user_id=%s", (_nc_user,))
    con.execute("delete from ledger where user_id=%s", (_nc_user,))
    con.execute("delete from uploads where user_id=%s", (_nc_user,))
    con.execute("delete from pricing where tool_id=%s", (_nc_tool,))
    con.execute("delete from platform_tools where tool_key=%s", (_nc_tool,))
    con.execute("delete from platform_tools where tool_key like %s",
                (_s7 + "%",))
    con.execute("delete from pricing where tool_id like %s", (_s7 + "%",))
    con.execute("delete from operator_audit where actor_user_id=%s"
                " and target_id like %s", (op_cp, _s7 + "%"))
    purge_users(con, [_nc_user])

# ═══ H1: hygiene — every row this suite created is removed ═══
_cleanup_own_rows()
with db() as con:
    _left = {
        "users": int(con.execute(
            "SELECT COUNT(*) n FROM users WHERE username ~ %s OR id = ANY(%s)",
            (_USER_RE, list(_EXTRA_USERS))).fetchone()["n"]),
        "tools": int(con.execute(
            "SELECT COUNT(*) n FROM platform_tools WHERE tool_key ~ %s"
            " OR tool_key LIKE %s", (_TOOL_RE, f"%{_RUN}%")).fetchone()["n"]),
        "pricing": int(con.execute(
            "SELECT COUNT(*) n FROM pricing WHERE tool_id LIKE %s",
            (f"%{_RUN}%",)).fetchone()["n"]),
        "plans": int(con.execute("SELECT COUNT(*) n FROM plans WHERE code ~ %s",
                                 (_PLAN_RE,)).fetchone()["n"]),
        "packages": int(con.execute(
            "SELECT COUNT(*) n FROM packages WHERE code ~ %s",
            (_PKG_RE,)).fetchone()["n"]),
        "audit": int(con.execute(
            "SELECT COUNT(*) n FROM operator_audit WHERE actor_user_id=%s",
            (op_cp,)).fetchone()["n"]),
        "running": int(con.execute(
            "SELECT COUNT(*) n FROM tool_calls WHERE user_id = ANY(%s)",
            ([op_cp, provider, caller, _u2, _xu],)).fetchone()["n"]),
        "withdrawals": int(con.execute(
            "SELECT COUNT(*) n FROM withdrawals WHERE user_id = ANY(%s)",
            ([provider, p2, p3, _xu, caller],)).fetchone()["n"]),
    }


check("H1 cleanup leaves zero own rows", not any(_left.values()), _left)


finish()
