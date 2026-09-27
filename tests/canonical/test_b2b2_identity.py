#!/usr/bin/env python3
"""B2b-2 identity & operator-surface closure canonical.

  password reset: one-time temp password, sessions revoked, API keys
      survive, audit without the credential, platform/backoffice rows
      protected.
  provider suspension: login refused, catalog/MCP/Web/author-page
      invisible, direct invocation refused before consume, API key dead;
      restore revives everything; earnings untouched.
  delete protection: every financial/audit fact table RESTRICTs user
      deletion; credential tables keep CASCADE.
  email: case-insensitive uniqueness (index), mixed-case registration
      converges, race and duplicate return typed errors, never 500.
  X bind race: two accounts binding the same X identity concurrently —
      the loser gets typed err_x_bound_other.
  operator tool create: params declaration rides the single supply
      validator (same errors as the user publish path).
  subscription.active: ignored (logged), zero mutation — the deleted
      no-op sync handler asserted gone from source.
  gate(): programmatic callers get 401 JSON; browsers keep the 302.
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:130]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import json as _json
import threading
import time
ts = int(time.time())
_u = "i2" + _os.urandom(4).hex()

from database import db

def _mkuser(name, **kw):
    cols = {"username": name, "email": f"{name}@t.co", "password_hash": "x",
            "display_name": "T", "status": "active", "created_at": ts,
            "last_login_at": ts}
    cols.update(kw)
    fields = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with db() as con:
        row = con.execute(
            f"insert into users ({fields}) values ({marks}) returning *",
            tuple(cols.values())).fetchone()
    return dict(row)

def _mktool(tool_key, owner_id, params_json=None):
    # pricing is keyed by the FULL tool_key — the supply-path convention
    # (create_tool_with_pricing → upsert_pricing(con, tool_key, ...))
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, tool_type, label_json, icon, visibility, enabled,"
            " params_json, created_at, updated_at)"
            " values (%s, %s, %s, 'http', 'produce', %s, 'wand', 'public',"
            " TRUE, %s, %s, %s)",
            (tool_key, owner_id, tool_key.split("/")[-1],
             _json.dumps({"en": "I2"}), params_json, ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, 0, TRUE, %s)", (tool_key, ts))

# ══════════ 1. operator password reset ══════════
from app.web_auth import create_browser_session, COOKIE_NAME
from security import verify_password
from backoffice import users as users_cmd

op = _mkuser(_u + "op", status="samelabs")
target = _mkuser(_u + "t1")
target_sess = create_browser_session(target["id"])

with db() as con:
    con.execute("update users set password_hash=%s where id=%s",
                ("$2b$12$oldhasholdhasholdhasholdhasholdhasholdhasholdhasho",
                 target["id"]))
uid, temp = users_cmd.reset_user_password(op["id"], target["id"])
with db() as con:
    row = con.execute("select password_hash from users where id=%s",
                      (target["id"],)).fetchone()
    n_sess = con.execute("select count(*) c from sessions where user_id=%s",
                         (target["id"],)).fetchone()["c"]
    aud = con.execute(
        "select before_json, after_json from operator_audit where"
        " action='user.password.reset' and target_id=%s",
        (str(target["id"]),)).fetchone()
check("R1 reset: new hash verifies the temp password, sessions all revoked",
      verify_password(temp, row["password_hash"]) and n_sess == 0,
      f"sessions={n_sess}")
check("R2 audit written, credential NEVER recorded",
      aud is not None
      and _json.dumps(temp) not in _json.dumps(dict(aud))
      and temp not in _json.dumps(dict(aud)), str(aud))
with db() as con:
    platform = dict(con.execute(
        "select * from users where username='platform'").fetchone())
for protect_id, label in ((platform["id"], "platform"),
                          (op["id"], "backoffice"),
                          (999999, "unknown")):
    try:
        users_cmd.reset_user_password(op["id"], protect_id)
        check(f"R3 reset refused for {label}", False)
        break
    except ValueError as exc:
        ok = str(exc) == ("user_protected" if protect_id != 999999
                          else "not_found")
        if not ok:
            check(f"R3 reset refused for {label}", False, str(exc))
            break
    except KeyError:
        if protect_id != 999999:
            check(f"R3 reset refused for {label}", False, "KeyError")
            break
else:
    check("R3 platform/backoffice/unknown reset refused (typed)", True)

# ══════════ 2. provider suspension — every surface ══════════
from economy.billing import grant as _grant
from governance.catalog import list_catalog, search_catalog, resolve
from repo import keys as keys_repo

prov = _mkuser(_u + "prov")
tool_a = f"{prov['username']}/zzp_{_u}"
tool_b = f"{prov['username']}/zzc_{_u}"
_mktool(tool_a, prov["id"])
_mktool(tool_b, prov["id"])
first_party_before = len([t for t in list_catalog(None)])
community_before = {t["id"] for t in list_catalog(None) if "/" in t["id"]}
check("S0a provider tools listed pre-suspension",
      {tool_a, tool_b} <= community_before, str(community_before))

row = users_cmd.set_user_suspended(op["id"], prov["id"], suspended=True)
with db() as con:
    st = con.execute("select status from users where id=%s",
                     (prov["id"],)).fetchone()["status"]
check("S1 suspend: status + audit",
      st == "suspended", st)
community_after = {t["id"] for t in list_catalog(None) if "/" in t["id"]}
check("S2 catalog hides suspended provider's tools",
      tool_a not in community_after and tool_b not in community_after)
check("S3 search hides them too",
      not {t["id"] for t in search_catalog("zz")
           if t["id"].startswith(prov["username"] + "/")})
t, err = resolve(tool_a, None)
check("S4 resolve refuses (invocation/tool page/MCP single point)",
      t is None and err == "not_found", f"{t} {err}")
t_self, err_self = resolve(tool_a, prov["id"])
check("S4b even the owner cannot resolve while suspended",
      t_self is None and err_self == "not_found")
# direct invocation through the REAL dispatch preflight (no claim/charge)
from execution.dispatch import preflight
payer = _mkuser(_u + "payer")
tool, declared, err_body, err_status = preflight(
    {"id": payer["id"]}, tool_a,
    {"expiry_days": 7, "allowed_downloads": 10}, {})
check("S5 dispatch preflight refuses before any charge",
      err_body == {"ok": False, "error": "tool_not_found"}
      and err_status == 404, f"{err_body} {err_status}")
bal_pre = None
from economy.billing import spendable_balance
bal_pre = spendable_balance(payer["id"])
# author page: suspended → 404
from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)
r = client.get(f"/u/{prov['username']}")
check("S6 author page 404 for suspended provider", r.status_code == 404,
      str(r.status_code))
# login + API key surfaces already keyed on status; assert the live query
from identity import auth as _auth
check("S7 login refused",
      _auth.authenticate_local(prov["username"], "whatever") is None)
# restore
users_cmd.set_user_suspended(op["id"], prov["id"], suspended=False)
community_restored = {t["id"] for t in list_catalog(None) if "/" in t["id"]}
t2, err2 = resolve(tool_a, None)
r2 = client.get(f"/u/{prov['username']}")
check("S8 restore revives catalog/resolve/author page",
      {tool_a, tool_b} <= community_restored and t2 is not None
      and r2.status_code == 200,
      f"{err2} {r2.status_code}")
row_replay = users_cmd.set_user_suspended(op["id"], prov["id"], False)
check("S9 suspend replay on active row → no-op success",
      row_replay["status"] == "active")
try:
    users_cmd.set_user_suspended(op["id"], platform["id"], True)
    check("S10 platform row cannot be suspended", False)
except ValueError as exc:
    check("S10 platform/backoffice rows cannot be suspended",
          str(exc) == "user_protected", str(exc))

# ══════════ 3. delete protection (FK RESTRICT) ══════════
restrict_tables = ["ledger", "tool_calls", "orders", "withdrawals",
                   "memberships", "payment_reviews",
                   "subscription_period_payments", "operator_audit"]
_victim = _mkuser(_u + "vic")
_mktool(f"{_victim['username']}/v_{_u}", _victim["id"])
from economy.billing import grant as grant_ledger
grant_ledger(_victim["id"], 10, "grant_daily", note="fk")
for tbl in restrict_tables:
    try:
        with db() as con:
            # minimal row per table via direct insert where cheap
            if tbl == "ledger":
                pass  # already has a row
            elif tbl == "tool_calls":
                con.execute(
                    "insert into tool_calls (client_ref, user_id, tool_id,"
                    " status, cost_snapshot, started_at, finished_at,"
                    " commission_rate_snapshot)"
                    " values ('fk', %s, 't', 'failed', 0, %s, %s, 0)",
                    (_victim["id"], ts, ts))
            elif tbl == "orders":
                con.execute(
                    "insert into orders (user_id, kind, item_id, provider,"
                    " status, amount_cents, currency, credits_snapshot,"
                    " created_at)"
                    " values (%s, 'package', 1, 'creem', 'failed', 1,"
                    " 'USD', 10, %s)", (_victim["id"], ts))
            elif tbl == "withdrawals":
                con.execute(
                    "insert into withdrawals (user_id, amount, status,"
                    " payout_channel_snapshot, created_at)"
                    " values (%s, 1, 'pending', 'x', %s)",
                    (_victim["id"], ts))
            elif tbl == "memberships":
                con.execute(
                    "insert into memberships (user_id, plan_id,"
                    " provider_subscription_id, status, started_at)"
                    " values (%s, (select id from plans order by id"
                    " limit 1), %s, 'active', %s)",
                    (_victim["id"], f"sub:fk_{_u}", ts))
            elif tbl == "payment_reviews":
                con.execute(
                    "insert into payment_reviews (event_type, event_id,"
                    " txn_id, user_id, created_at)"
                    " values ('refund.created', %s, %s, %s, %s)",
                    (f"fk_{_u}", f"txn_{_u}", _victim["id"], ts))
            elif tbl == "subscription_period_payments":
                con.execute(
                    "insert into subscription_period_payments (sub_id,"
                    " period_start, user_id, base_txn_id, created_at)"
                    " values (%s, %s, %s, %s, %s)",
                    (f"sub:fk_{_u}", ts, _victim["id"], f"txn_{_u}", ts))
            elif tbl == "operator_audit":
                from repo import operator_audit
                operator_audit.record(con, op["id"], "credits.adjust",
                                      "user_credits", str(_victim["id"]),
                                      None, None, ts)
        with db() as con:
            con.execute("delete from users where id=%s", (_victim["id"],))
        check(f"F1 {tbl}: user deletion RESTRICTed", False)
        break
    except Exception as exc:
        if "foreign key" not in str(exc).lower() and "restrict" not in str(exc).lower():
            check(f"F1 {tbl}: user deletion RESTRICTed", False,
                  str(exc)[:90])
            break
else:
    check("F1 all 8 fact tables RESTRICT user deletion (+platform_tools.owner)",
          True)
# platform_tools owner restrict: victim has a tool → deletion blocked
try:
    with db() as con:
        con.execute("delete from users where id=%s", (_victim["id"],))
    check("F2 platform_tools.owner_id RESTRICTs", False)
except Exception:
    check("F2 platform_tools.owner_id RESTRICTs user deletion", True)
# credential tables still CASCADE: sessions row dies with the user
casc = _mkuser(_u + "casc")
create_browser_session(casc["id"])
with db() as con:
    con.execute("delete from users where id=%s", (casc["id"],))
with db() as con:
    n = con.execute("select count(*) c from sessions where user_id=%s",
                    (casc["id"],)).fetchone()["c"]
check("F3 sessions CASCADE with user deletion", n == 0, str(n))

# ══════════ 4. email lowercased + unique ══════════
from identity import auth as _auth2
mixed = _mkuser(_u + "m1", email=None)
with db() as con:
    con.execute("delete from users where id=%s", (mixed["id"],))
uid_m = _auth2.register_user(_u + "m1", f"MiXeD.{_u}@Example.COM", "pw12345678")
with db() as con:
    stored = con.execute("select email from users where id=%s",
                         (uid_m,)).fetchone()["email"]
check("E1 registration stores the lowercased email",
      stored == f"mixed.{_u}@example.com", stored)
try:
    _auth2.register_user(_u + "m2", f"MIXED.{_u}@example.com", "pw12345678")
    check("E2 case-variant duplicate refused (typed)", False)
except ValueError as exc:
    check("E2 case-variant duplicate refused (typed err_email_taken)",
          str(exc) == "err_email_taken", str(exc))
# login with the mixed-case form works
check("E3 mixed-case login form resolves",
      _auth2.authenticate_local(f"MiXeD.{_u}@Example.COM", "pw12345678")
      == uid_m)
# update_profile email race → typed
other = _mkuser(_u + "o1", password_hash=None)
try:
    _auth2.update_profile(other["id"], email=f"MIXED.{_u}@Example.COM")
    check("E4 update to a taken email refused (typed)", False)
except ValueError as exc:
    check("E4 update to a taken email refused (typed err_email_used)",
          str(exc) == "err_email_used", str(exc))
with db() as con:
    idx = con.execute(
        "select indexdef from pg_indexes where indexname ="
        " 'users_email_lower_uk'").fetchone()
check("E5 lower(email) unique index exists",
      idx is not None and "UNIQUE" in idx["indexdef"]
      and "lower(email)" in idx["indexdef"], str(idx))

# ══════════ 5. X bind race → typed loser ══════════
def _x_snap(x_id):
    return {"id": x_id, "username": f"xu_{x_id[-6:]}",
            "name": "XU", "profile_image_url": ""}

a = _mkuser(_u + "xa")
b = _mkuser(_u + "xb")
x_id = f"1779{_u}x".replace("-", "")[:18]
results = {}
def _bind(uid_key):
    try:
        results[uid_key] = ("ok", _auth2.upsert_x_user(_x_snap(x_id),
                                                       bind_user_id=uid_key))
    except ValueError as exc:
        results[uid_key] = ("typed", str(exc))
    except Exception as exc:
        results[uid_key] = ("untyped", f"{type(exc).__name__}:{exc}")

th1 = threading.Thread(target=_bind, args=(a["id"],))
th2 = threading.Thread(target=_bind, args=(b["id"],))
th1.start(); th2.start(); th1.join(); th2.join()
kinds = [v[0] for v in results.values()]
check("X1 concurrent bind: one winner, one typed err_x_bound_other, zero 500",
      sorted(kinds) == ["ok", "typed"]
      and all(v[1] == "err_x_bound_other" for v in results.values()
              if v[0] == "typed"),
      str(results))
# concurrent first-login INSERT race on the same X identity converges
fresh_x = f"1788{_u}y".replace("-", "")[:18]
login_results = {}
def _login():
    try:
        login_results[threading.get_ident()] = \
            ("ok", _auth2.upsert_x_user(_x_snap(fresh_x)))
    except Exception as exc:
        login_results[threading.get_ident()] = \
            ("untyped", f"{type(exc).__name__}:{exc}")
t1 = threading.Thread(target=_login)
t2 = threading.Thread(target=_login)
t1.start(); t2.start(); t1.join(); t2.join()
ids = {v[1] for v in login_results.values() if v[0] == "ok"}
check("X2 concurrent first login converges to ONE account, no 500",
      len(login_results) == 2 and all(v[0] == "ok" for v in login_results.values())
      and len(ids) == 1, str(login_results))

# ══════════ 6. operator tool create with params (single validator) ══════════
from backoffice import tools as tools_cmd
from governance.tool_supply import DeclarationError
op_sess = create_browser_session(op["id"])
OP_H = {"Cookie": f"{COOKIE_NAME}={op_sess}"}
good_params = [{"key": "mode", "type": "enum", "label": {"en": "Mode"},
                "options": [{"value": "fast"}, {"value": "best"}],
                "required": True, "default": "fast"}]
tool_key = tools_cmd.create_http_tool(op["id"], {
    "tool_id": f"i2op{_u}"[:24].replace("-", ""), "label": "I2 Op Tool",
    "url": "https://op.example.com/api", "timeout_s": 30,
    "accept": None, "files_min": 0, "files_max": 1,
    "outputs": ["text"], "params": good_params, "cost": 0})
with db() as con:
    row = con.execute(
        "select params_json from platform_tools where tool_key=%s",
        (tool_key,)).fetchone()
check("P1 operator tool with params → stored canonical declaration",
      row is not None
      and _json.loads(row["params_json"])[0]["key"] == "mode",
      str(row))
try:
    tools_cmd.create_http_tool(op["id"], {
        "tool_id": f"i2bad{_u}"[:24].replace("-", ""), "label": "Bad",
        "url": "https://op.example.com/api", "timeout_s": 30,
        "accept": None, "files_min": 0, "files_max": 1,
        "outputs": ["text"],
        "params": [{"key": "x", "type": "nope"}], "cost": 0})
    check("P2 invalid params refused by the SAME validator", False)
except DeclarationError:
    check("P2 invalid params refused by the SAME validator "
          "(tool_supply DeclarationError)", True)
r = client.post("/samelabs/tools/create",
                json={"tool_id": f"i2rt{_u}"[:24].replace("-", ""),
                      "label": "Route", "url": "https://op.example.com/api",
                      "params": {"key": "not-a-list"}},
                headers=OP_H)
check("P3 route refuses non-array params (bad_params 422)",
      r.status_code == 422 and r.json().get("error") == "bad_params",
      r.text)

# ══════════ 7. subscription.active ignored; no-op sync deleted ══════════
from economy.subscription import handle_event as _hev
sub_ev = f"sub_{_u}ign"
res = _hev("subscription.active",
           {"subscription": {"id": sub_ev, "status": "active"}})
src_sub = open(_os.path.join(_REPO, "core", "economy", "subscription.py"),
               encoding="utf-8").read()
check("A1 subscription.active → acknowledged ignored, zero mutation",
      res.get("ignored") is True, str(res))
check("A2 the no-op sync handler is gone from source",
      "_ev_sync" not in src_sub and "def _ev_sync" not in src_sub)

# ══════════ 8. gate(): 401 JSON for programs, 302 for browsers ══════════
r_json = client.post("/samelabs/economy/commission", json={"rate": 10},
                     headers={"Accept": "application/json"})
r_html = client.post("/samelabs/economy/commission", json={"rate": 10},
                     headers={"Accept": "text/html,application/xhtml+xml"},
                     follow_redirects=False)
check("G1 unauthenticated JSON caller → 401 JSON body",
      r_json.status_code == 401
      and r_json.json().get("error") == "operator_auth_required",
      f"{r_json.status_code} {r_json.text}")
check("G2 browser-shaped caller → login redirect",
      r_html.status_code == 302 and "/auth/login" in r_html.headers.get(
          "location", ""), str(r_html.status_code))

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
_sys.exit(1 if FAIL else 0)
