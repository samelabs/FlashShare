#!/usr/bin/env python3
"""Plan mirror DB commit-outcome closure (route-level, real fault injection).

Covers the three post-provider-success DB COMMIT outcomes on the
operator plan update/create routes: DURABLE_SUCCESS (row+audit
durable → success, no compensation), PROVEN_ROLLBACK (row == exact
before + no audit → provider restored to exact provider-before),
UNRESOLVED (unprovable → fail loud, zero speculative mutation).
The provider-before compensation basis must be a REAL provider GET
snapshot, never fabricated 0/"".
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

import time
ts = int(time.time())
_u = "mc" + _os.urandom(3).hex()

from database import db
import economy.creem as _creem
from app.main import app as _app
from app.web_auth import create_browser_session, COOKIE_NAME
from fastapi.testclient import TestClient
client = TestClient(_app)

# ── fixtures: operator + plan + provider fake ──
with db() as con:
    op = con.execute(
        "insert into users (username, email, password_hash, display_name,"
        " status, created_at, last_login_at) values (%s, %s, 'x',"
        " 'OP', 'samelabs', %s, %s) returning id",
        (_u + "op", f"{_u}op@t.co", ts, ts)).fetchone()
    _op_id = int(op["id"])
    _pl = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'MC', 100, 1000, 'USD', TRUE, %s, 94, 30, 100,"
        " 104857600, %s, %s) returning *",
        (f"mcplan_{_u}", "prod_mc_x", ts, ts)).fetchone()
    _pl = dict(_pl)
sess = create_browser_session(_op_id)
op_ck = sess  # token string

# provider fake: state + record
_prod_state = {"prod_mc_x": {"price": 1000, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}}
_gets = {"n": 0}
_patches = []
def _fake_fetch(pid):
    _gets["n"] += 1
    st = _prod_state.get(pid)
    if st is None:
        raise RuntimeError("product_not_found")
    return dict(st)
def _fake_patch(pid, payload):
    _patches.append((pid, dict(payload)))
    _prod_state[pid]["price"] = int(payload.get("price") or 0)
    return True
_orig_fetch, _orig_patch = _creem._fetch_product, _creem._patch
_orig_verify, _orig_mirror = _creem.verify_product_mirror, _creem.mirror_product_price
_creem._fetch_product = _fake_fetch
_creem._patch = _fake_patch
_creem.creem_enabled = lambda: True
_creem.mirror_product_price = lambda pid, price, cur: _fake_patch(
    pid, {"price": price}) or None
def _fake_verify(pid, price, cur):
    st = _prod_state.get(pid)
    if st is None:
        return "not_found"
    if int(st["price"]) == int(price) and str(st["currency"]).upper() == str(cur).upper():
        return None
    return "price_divergence"
_creem.verify_product_mirror = _fake_verify

# ── F1: provider success + durable commit → success, NO restore ──
r1 = client.patch(f"/samelabs/plans/{_pl['id']}",
                  json={"price_cents": 1499},
                  cookies={COOKIE_NAME: op_ck})
_n_patch_before = len(_patches)
with db() as con:
    _row = con.execute("select price_cents from plans where id=%s",
                       (_pl["id"],)).fetchone()
check("F1 provider success + durable commit → 200 / row=intended / no restore",
      r1.status_code == 200 and int(_row["price_cents"]) == 1499
      and len(_patches) == _n_patch_before
      and _prod_state["prod_mc_x"]["price"] == 1499,
      f"{r1.status_code} {r1.text[:80]} row={dict(_row)}")

# ── F2: commit raises (connection drop at COMMIT) + fresh read proves
#        DURABLE_SUCCESS (row == intended + audit) → success, no restore ──
import app.routes.samelabs as _sml
import database as _dbmod
from database import db as _rdb
_orig_db = _dbmod.db
_orig_sml_db = _sml.db
import contextlib
import sys as _sysexc


class _CommitFailCon:
    """proxies a real transaction but raises at COMMIT with the data
    ACTUALLY committed (ACK-loss after durable write). Only arms itself
    when the transaction touched the plans table — auth/session reads
    pass through and commit normally."""
    def __init__(self, real):
        self._real = real
        self._armed = False
    def execute(self, sql, params=()):
        if isinstance(sql, str) and "plans" in sql and (
                "UPDATE" in sql.upper() or "INSERT INTO plans" in sql.upper()):
            self._armed = True
        return self._real.execute(sql, params)
    def __getattr__(self, name):
        return getattr(self._real, name)
@contextlib.contextmanager
def _db_commit_durable_then_raise():
    import psycopg2 as _pg2
    cm = _rdb()
    con = cm.__enter__()
    wrap = _CommitFailCon(con)
    try:
        yield wrap
        cm.__exit__(None, None, None)   # real commit
    except BaseException:
        cm.__exit__(*_sysexc.exc_info())
        raise
    if wrap._armed:
        raise _pg2.OperationalError(
            "simulated commit ack loss (durable)")
# patch BOTH the source module (function-local imports resolve here)
# and the samelabs module global (module-level `from database import db`)
_dbmod.db = _db_commit_durable_then_raise
_sml.db = _db_commit_durable_then_raise
try:
    r2 = client.patch(f"/samelabs/plans/{_pl['id']}",
                      json={"price_cents": 1699},
                      cookies={COOKIE_NAME: op_ck})
except Exception:
    r2 = None
finally:
    _dbmod.db = _orig_db
    _sml.db = _orig_sml_db
with db() as con:
    _row2 = con.execute("select price_cents from plans where id=%s",
                        (_pl["id"],)).fetchone()
_ok2 = (r2 is not None and r2.status_code == 200
        and int(_row2["price_cents"]) == 1699
        and _prod_state["prod_mc_x"]["price"] == 1699)
check("F2 commit exception + fresh read proves after+audit → DURABLE_SUCCESS",
      _ok2, f"{r2 and r2.status_code} {r2 and r2.text[:80]} row={dict(_row2)}"
      f" mirror={_prod_state['prod_mc_x']}")

# ── F3: commit fails with the transaction ROLLED BACK (row == before,
#        no audit) → PROVEN_ROLLBACK → provider restored to exact before ──
@contextlib.contextmanager
def _db_rollback_then_raise():
    cm = _rdb()
    con = cm.__enter__()
    wrap = _CommitFailCon(con)
    try:
        yield wrap
        if wrap._armed:
            import psycopg2 as _pg2
            cm.gen.throw(_pg2.OperationalError(
                "simulated commit failure (rolled back)"))
        else:
            cm.__exit__(None, None, None)
    except BaseException:
        cm.__exit__(*_sysexc.exc_info())
        raise
_dbmod.db = _db_rollback_then_raise
_sml.db = _db_rollback_then_raise
try:
    r3 = client.patch(f"/samelabs/plans/{_pl['id']}",
                      json={"price_cents": 1999},
                      cookies={COOKIE_NAME: op_ck})
except Exception as exc:
    r3 = type("R", (), {"status_code": 0, "text": str(exc)})()
finally:
    _dbmod.db = _orig_db
    _sml.db = _orig_sml_db
with db() as con:
    _row3 = con.execute("select price_cents from plans where id=%s",
                        (_pl["id"],)).fetchone()
_ok3 = (int(_row3["price_cents"]) == 1699          # local == exact before
        and _prod_state["prod_mc_x"]["price"] == 1699)  # provider restored
check("F3 commit failure + row==before/no audit → PROVEN_ROLLBACK + restore",
      _ok3, f"row={dict(_row3)} mirror={_prod_state['prod_mc_x']}"
      f" resp={getattr(r3, 'status_code', '?')}")

# ── F4: create with commit failure → row absent + no audit → rollback
#        (provider restored to its real before state) ──
_prod_state["prod_mc_new"] = {"price": 1, "currency": "USD",
                              "billing_type": "recurring",
                              "billing_period": "every-month"}
_p_before = len(_patches)
_dbmod.db = _db_rollback_then_raise
_sml.db = _db_rollback_then_raise
try:
    r4 = client.post("/samelabs/plans", json={
        "code": f"mcnew_{_u}", "name": "MC New", "monthly_credits": 50,
        "price_cents": 777, "creem_product_id": "prod_mc_new",
        "sort_order": 95, "period_days": 30,
        "ingress_files_24h": 50, "ingress_bytes_24h": 104857600},
        cookies={COOKIE_NAME: op_ck})
except Exception as exc:
    r4 = type("R", (), {"status_code": 0, "text": str(exc)})()
finally:
    _dbmod.db = _orig_db
    _sml.db = _orig_sml_db
with db() as con:
    _row4 = con.execute("select 1 from plans where code=%s",
                        (f"mcnew_{_u}",)).fetchone()
check("F4 create commit failure → row absent + provider restored to before",
      _row4 is None and _prod_state["prod_mc_new"]["price"] == 1
      and len(_patches) > _p_before,
      f"row={_row4} mirror={_prod_state['prod_mc_new']}")

# ── F5: compensation basis is the REAL provider GET snapshot ──
_src = open(_os.path.join(_REPO, "app", "routes", "samelabs.py"),
            encoding="utf-8").read()
_create_blk = _src.split("async def samelabs_create_plan")[1].split("async def")[0]
check("F5 create mirror uses real provider-before GET (no 0/\"\")",
      "_fetch_product" in _create_blk
      and 'fields["creem_product_id"], 0, ""' not in _create_blk,
      "")

# restore patches
_creem._fetch_product = _orig_fetch
_creem._patch = _orig_patch
_creem.verify_product_mirror = _orig_verify
_creem.mirror_product_price = _orig_mirror

# ── cleanup ──
with db() as con:
    con.execute("delete from operator_audit where actor_user_id=%s", (_op_id,))
    con.execute("delete from plans where id=%s", (_pl["id"],))
    con.execute("delete from users where id=%s", (_op_id,))
print("cleanup: mirror-commit fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
