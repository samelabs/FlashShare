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
    _mc_sort = int(con.execute(
        "select coalesce(max(sort_order), 0) + 1 s from plans"
    ).fetchone()["s"])
    _pl = con.execute(
        "insert into plans (code, name, monthly_credits, price_cents,"
        " currency, active, creem_product_id, sort_order, period_days,"
        " ingress_files_24h, ingress_bytes_24h, created_at, updated_at)"
        " values (%s, 'MC', 100, 1000, 'USD', TRUE, %s, %s, 30, 100,"
        " 104857600, %s, %s) returning *",
        (f"mcplan_{_u}", "prod_mc_x", _mc_sort, ts, ts)).fetchone()
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
        "sort_order": _mc_sort + 1, "period_days": 30,
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

# ── G-series: provider-before proof — UNKNOWN never becomes a
#    compensable state. Inject the initial _fetch_product() returning
#    None (GET failure) and prove: zero PATCH, zero restore, local
#    rollback, no fabricated (0, "") before. ──
_ops_sequential = dict(_patches)   # snapshot counters
_nb_create = len(_patches)
_nb_update = len(_patches)

# G1: update path — initial GET unprovable → fail closed BEFORE any PATCH
_orig_fetch_g = _creem._fetch_product
def _fetch_none_first_update(pid):
    if pid == "prod_mc_x" and not _fetch_none_first_update.done:
        _fetch_none_first_update.done = True
        return None            # provider GET failure → unprovable before
    return _orig_fetch_g(pid)
# arm only at G1 time: the FIRST prod_mc_x fetch from here on returns None
class _Armable:
    def __init__(self):
        self.armed = False
        self.none_ids = set()   # per-product one-shot None
    def __call__(self, pid):
        if self.armed and pid in self.none_ids:
            self.none_ids.discard(pid)   # one shot per product
            return None
        return _orig_fetch_g(pid)
_fetch_none_first_update = _Armable()
_fetch_none_first_update.armed = True
_fetch_none_first_update.none_ids.add("prod_mc_x")
_creem._fetch_product = _fetch_none_first_update
_p_before_g = len(_patches)
_r_g1 = client.patch(f"/samelabs/plans/{_pl['id']}",
                     json={"price_cents": 3456},
                     cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g1 = con.execute("select price_cents from plans where id=%s",
                          (_pl["id"],)).fetchone()
check("G1 update: initial GET None → fail closed / 零 PATCH / local 不变",
      _r_g1.status_code == 502
      and str(_r_g1.json().get("error", "")).startswith("price_mirror")
      and len(_patches) == _p_before_g
      and int(_row_g1["price_cents"]) != 3456,
      f"{_r_g1.status_code} {str(_r_g1.json())[:80]}"
      f" patches={len(_patches)}/{_p_before_g} row={dict(_row_g1)}")
_fetch_none_first_update.armed = False   # disarm for later sections

# G2: create path — initial GET unprovable → fail closed BEFORE any PATCH
_prod_state["prod_mc_g2"] = {"price": 4321, "currency": "USD",
                             "billing_type": "recurring",
                             "billing_period": "every-month"}
# arm the one-shot None for the create path's initial GET
_fetch_none_first_update.armed = True
_fetch_none_first_update.none_ids.add("prod_mc_g2")
_p_before_g2 = len(_patches)
_r_g2 = client.post("/samelabs/plans", json={
    "code": f"mcg2_{_u}", "name": "MC G2", "monthly_credits": 60,
    "price_cents": 555, "creem_product_id": "prod_mc_g2",
    "sort_order": _mc_sort + 5, "period_days": 30,
    "ingress_files_24h": 50, "ingress_bytes_24h": 104857600},
    cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g2 = con.execute("select 1 from plans where code=%s",
                          (f"mcg2_{_u}",)).fetchone()
check("G2 create: initial GET None → fail closed / 零 PATCH / row 未落",
      _r_g2.status_code == 502
      and str(_r_g2.json().get("error", "")).startswith("price_mirror")
      and len(_patches) == _p_before_g2
      and _row_g2 is None,
      f"{_r_g2.status_code} patches={len(_patches)}/{_p_before_g2}"
      f" row={_row_g2}")
_fetch_none_first_update.armed = False
_creem._fetch_product = _orig_fetch_g

# ── G3/G4/G5: provider-before PRICE must be explicit (missing/None →
#    fail closed; explicit legitimate 0 is a real fact) ──
class _PartialHook:
    """One-shot partial provider snapshot: valid id + currency but the
    price key MISSING (or an explicit 0 for the positive case)."""
    def __init__(self):
        self.armed = False
        self.pid = None
        self.override = None
    def __call__(self, pid):
        if self.armed and pid == self.pid and self.override is not None:
            self.armed = False
            return dict(self.override)   # partial provider snapshot
        return _orig_fetch_g(pid)
_partial_hook = _PartialHook()
_creem._fetch_product = _partial_hook

# G3: update — id/currency present, price key missing → fail closed
_p_before_g3 = len(_patches)
_partial_hook.armed = True
_partial_hook.pid = "prod_mc_x"
_partial_hook.override = {"id": "prod_mc_x", "currency": "USD",
                          "billing_type": "recurring",
                          "billing_period": "every-month"}   # no price key
_r_g3 = client.patch(f"/samelabs/plans/{_pl['id']}",
                     json={"price_cents": 3777},
                     cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g3 = con.execute("select price_cents from plans where id=%s",
                          (_pl["id"],)).fetchone()
check("G3 update: snapshot missing price → 502 / 零 PATCH / local 不变",
      _r_g3.status_code == 502
      and str(_r_g3.json().get("error", "")).startswith("price_mirror")
      and len(_patches) == _p_before_g3
      and int(_row_g3["price_cents"]) != 3777,
      f"{_r_g3.status_code} patches={len(_patches)}/{_p_before_g3}"
      f" row={dict(_row_g3)}")

# G4: create — id/currency present, price=None → fail closed, row absent
_p_before_g4 = len(_patches)
_partial_hook.armed = True
_partial_hook.pid = "prod_mc_g4"
_partial_hook.override = {"id": "prod_mc_g4", "currency": "USD",
                          "price": None,
                          "billing_type": "recurring",
                          "billing_period": "every-month"}
_r_g4 = client.post("/samelabs/plans", json={
    "code": f"mcg4_{_u}", "name": "MC G4", "monthly_credits": 70,
    "price_cents": 888, "creem_product_id": "prod_mc_g4",
    "sort_order": _mc_sort + 6, "period_days": 30,
    "ingress_files_24h": 50, "ingress_bytes_24h": 104857600},
    cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g4 = con.execute("select 1 from plans where code=%s",
                          (f"mcg4_{_u}",)).fetchone()
check("G4 create: snapshot price=None → 502 / 零 PATCH / row 未落",
      _r_g4.status_code == 502
      and str(_r_g4.json().get("error", "")).startswith("price_mirror")
      and len(_patches) == _p_before_g4
      and _row_g4 is None,
      f"{_r_g4.status_code} patches={len(_patches)}/{_p_before_g4}"
      f" row={_row_g4}")

# G5: explicit legitimate price=0 is a PROVEN fact — not treated as
#     missing; the update proceeds normally on the real (0, USD) before
_prod_state["prod_mc_x"]["price"] = 0   # provider carries explicit 0
_p_before_g5 = len(_patches)
_r_g5 = client.patch(f"/samelabs/plans/{_pl['id']}",
                     json={"price_cents": 150},
                     cookies={COOKIE_NAME: op_ck})
with db() as con:
    _row_g5 = con.execute("select price_cents from plans where id=%s",
                          (_pl["id"],)).fetchone()
check("G5 显式合法 price=0 是真实 fact → 更新正常 (不误判 missing)",
      _r_g5.status_code == 200 and int(_row_g5["price_cents"]) == 150
      and len(_patches) == _p_before_g5 + 1
      and _prod_state["prod_mc_x"]["price"] == 150,
      f"{_r_g5.status_code} patches={len(_patches)}/{_p_before_g5}"
      f" row={dict(_row_g5)} mirror={_prod_state['prod_mc_x']}")
# restore provider/local price for the sections that follow
_prod_state["prod_mc_x"]["price"] = 150

# ── G6/G7: non-canonical price TYPES fail closed — no int() coercion
#    may fabricate a before (float 150.7 → 150, bool True → 1) ──
def _noncanon_case(tag, bad_price, is_create):
    _p_before = len(_patches)
    _partial_hook.armed = True
    _partial_hook.pid = "prod_mc_x" if not is_create else f"prod_nc_{tag}"
    _partial_hook.override = {"id": _partial_hook.pid, "currency": "USD",
                              "price": bad_price,
                              "billing_type": "recurring",
                              "billing_period": "every-month"}
    if is_create:
        r = client.post("/samelabs/plans", json={
            "code": f"mcnc{tag}_{_u}", "name": "MC NC", "monthly_credits": 90,
            "price_cents": 1234, "creem_product_id": _partial_hook.pid,
            "sort_order": _mc_sort + 10, "period_days": 30,
            "ingress_files_24h": 50, "ingress_bytes_24h": 104857600},
            cookies={COOKIE_NAME: op_ck})
        with db() as con:
            row = con.execute("select 1 from plans where code=%s",
                              (f"mcnc{tag}_{_u}",)).fetchone()
        return (r.status_code == 502
                and str(r.json().get("error", "")).startswith("price_mirror")
                and len(_patches) == _p_before and row is None,
                f"{r.status_code} patches={len(_patches)}/{_p_before} row={row}")
    r = client.patch(f"/samelabs/plans/{_pl['id']}",
                     json={"price_cents": 1990},
                     cookies={COOKIE_NAME: op_ck})
    with db() as con:
        row = con.execute("select price_cents from plans where id=%s",
                          (_pl["id"],)).fetchone()
    return (r.status_code == 502
            and str(r.json().get("error", "")).startswith("price_mirror")
            and len(_patches) == _p_before
            and int(row["price_cents"]) != 1990,
            f"{r.status_code} patches={len(_patches)}/{_p_before} row={dict(row)}")

_ok6a, _d6a = _noncanon_case("f", 150.7, False)     # update: float
check("G6a update: provider price=150.7 (float) → fail closed / 零 PATCH / local 不变",
      _ok6a, _d6a)
_ok6b, _d6b = _noncanon_case("fc", 150.7, True)     # create: float
check("G6b create: provider price=150.7 (float) → fail closed / 零 PATCH / row 未落",
      _ok6b, _d6b)
_ok7a, _d7a = _noncanon_case("b", True, False)      # update: bool
check("G7a update: provider price=True (bool) → fail closed / 零 PATCH",
      _ok7a, _d7a)
_ok7b, _d7b = _noncanon_case("bc", True, True)      # create: bool
check("G7b create: provider price=True (bool) → fail closed / row 未落",
      _ok7b, _d7b)
_ok7c, _d7c = _noncanon_case("s", "1500", False)    # update: string
check("G7c update: provider price='1500' (string) → fail closed / 零 coercion",
      _ok7c, _d7c)

# ── H-series: EXACT snapshot / audit correlation (mixed-field ops) ──
# H1 (deterministic): a stale audit INSIDE this operation's correlation
# window (created_at >= the route's frozen tx_started) with the SAME
# after price but a DIFFERENT full before→after pair must NEVER let a
# mixed-field rollback classify as success. The route's now() is
# frozen via injection — no wall-clock race.
from repo import operator_audit as _oa
with db() as con:
    # the plan currently carries price 150 (G5); the current operation
    # is "price unchanged + name change"
    _row_h = con.execute("select name, price_cents from plans where id=%s",
                         (_pl["id"],)).fetchone()
    _name_before_h = _row_h["name"]
# freeze the route's clock AFTER recording the stale audit with an
# earlier-or-equal timestamp, so the stale audit is provably inside
# the boundary (stale_created >= frozen_tx_ts):
import time as _time_h
_frozen_now = int(_time_h.time())
_orig_now_h = _sml.now
_sml.now = lambda: _frozen_now
try:
    with db() as con:
        # stale audit: SAME actor/plan/action, created_at == the frozen
        # tx boundary (>= tx_started), after price == intended price,
        # but a FULL snapshot pair of a DIFFERENT operation
        _oa.record(con, int(_op_id), "plan.update", "plan",
                   str(_pl["id"]),
                   {"name": _name_before_h, "price_cents": 1000},
                   {"name": _name_before_h + "_STALE", "price_cents": 150},
                   _frozen_now)
    # inject: commit rollback + the route's mirror runs (provider mutated)
    _p_before_h1 = len(_patches)
    _dbmod.db = _db_rollback_then_raise
    _sml.db = _db_rollback_then_raise
    try:
        _r_h1 = client.patch(f"/samelabs/plans/{_pl['id']}",
                             json={"price_cents": 150,
                                   "name": _name_before_h + "_CURRENT"},
                             cookies={COOKIE_NAME: op_ck})
    except Exception as exc:
        _r_h1 = type("R", (), {"status_code": 0, "text": str(exc)})()
    finally:
        _dbmod.db = _orig_db
        _sml.db = _orig_sml_db
finally:
    _sml.now = _orig_now_h
with db() as con:
    _row_h1 = con.execute("select name, price_cents from plans"
                          " where id=%s", (_pl["id"],)).fetchone()
    # prove the stale audit is inside the boundary the classifier used
    _stale_in_window = con.execute(
        "select count(*) c from operator_audit"
        " where target_type='plan' and target_id=%s"
        "   and after_json->>'price_cents'='150'"
        "   and created_at >= %s",
        (str(_pl["id"]), _frozen_now)).fetchone()["c"]
_h1_was_success = _r_h1.status_code == 200
check("H1d precondition: stale same-price audit 确在 frozen tx boundary 内",
      _stale_in_window >= 1, f"in_window={_stale_in_window}")
check("H1d stale in-window same-price audit + mixed-field rollback → 绝不 SUCCESS",
      (not _h1_was_success) and _row_h1["name"] == _name_before_h,
      f"st={_r_h1.status_code} name={_row_h1['name']!r}"
      f" (before={_name_before_h!r}) was_success={_h1_was_success}")

# ── B-series: strict exact-before three-way classifier ──
import app.routes.samelabs as _smlmod
from repo import operator_audit as _oa

# B1: update commit unknown + DB third-state (row neither before nor
#     intended) + no matching audit → UNRESOLVED, never rollback/restore
with db() as con:
    con.execute("update plans set price_cents=2099 where id=%s", (_pl["id"],))
    con.execute("delete from operator_audit where target_type='plan'"
                " and target_id=%s", (str(_pl["id"]),))
_p_before_b = len(_patches)
_v1 = _smlmod._classify_db_commit_outcome(
    int(_pl["id"]),
    {"price_cents": 2499, "currency": "USD",
     "creem_product_id": "prod_mc_x"},
    {"price_cents": 1699, "currency": "USD",
     "creem_product_id": "prod_mc_x"},   # exact before
    int(_op_id), int(ts) - 3600, "plan.update")
check("B1 update third-state row + no audit → UNRESOLVED (never rollback)",
      _v1[0] == "unresolved", str(_v1))
check("B1b classifier 零 provider mutation",
      len(_patches) == _p_before_b,
      f"patches={len(_patches)} before={_p_before_b}")

# B2: create commit unknown + unexpected row exists + no audit → UNRESOLVED
#     (row absent stays the only PROVEN_ROLLBACK shape for create)
_v2 = _smlmod._classify_db_commit_outcome(
    999999,
    {"price_cents": 777, "currency": "USD",
     "creem_product_id": "prod_mc_new"},
    None, int(_op_id), int(ts) - 3600, "plan.create")
_v2b = _smlmod._classify_db_commit_outcome(
    int(_pl["id"]),   # an EXISTING row where create expected none
    {"price_cents": 777, "currency": "USD",
     "creem_product_id": "prod_mc_new"},
    None, int(_op_id), int(ts) - 3600, "plan.create")
check("B2 create unexpected row exists → UNRESOLVED (absent 才 rollback)",
      _v2[0] == "rollback" and _v2b[0] == "unresolved",
      f"absent={_v2[0]} existing={_v2b[0]}")

# B3: durable commit identified across a second boundary — tx_started
#     fixed before the provider call, audit written seconds later.
#     Full-snapshot proof: row == intended, audit == exact pair.
with db() as con:
    con.execute("update plans set price_cents=1699 where id=%s", (_pl["id"],))
with db() as con:
    _row_b3 = con.execute("select * from plans where id=%s",
                          (_pl["id"],)).fetchone()
    _snap_b3 = _smlmod._snap_plan(_row_b3)               # == intended
    _before_b3 = dict(_snap_b3, price_cents=_snap_b3["price_cents"] - 200)
    _oa.record(con, int(_op_id), "plan.update", "plan", str(_pl["id"]),
               _before_b3, _snap_b3, int(ts) + 5)
_v3 = _smlmod._classify_db_commit_outcome(
    int(_pl["id"]), _snap_b3, _before_b3,
    int(_op_id), int(ts) - 3, "plan.update")
check("B3 durable commit 跨秒后仍识别 matching audit → SUCCESS",
      _v3[0] == "success", str(_v3))

# B3b: historical same-price audit BEFORE this operation's boundary
#      must never be mistaken for this operation's durable audit
with db() as con:
    con.execute("delete from operator_audit where target_type='plan'"
                " and target_id=%s", (str(_pl["id"]),))
    _oa.record(con, int(_op_id), "plan.update", "plan", str(_pl["id"]),
               _before_b3, dict(_snap_b3, name="OLD"), int(ts) - 7200)
_v3b = _smlmod._classify_db_commit_outcome(
    int(_pl["id"]), _snap_b3, _before_b3,
    int(_op_id), int(ts) - 3, "plan.update")
check("B3b 历史同价旧 audit (boundary 之前) 不误认 → 非 SUCCESS",
      _v3b[0] != "success", str(_v3b))

# B4: compensation fires ONLY on exact PROVEN_ROLLBACK —
#     exact-before row + no matching audit is the ONLY rollback shape
with db() as con:
    con.execute("delete from operator_audit where target_type='plan'"
                " and target_id=%s", (str(_pl["id"]),))
# (a real update always has before != intended; row==before here)
with db() as con:
    _row_b4 = con.execute("select * from plans where id=%s",
                          (_pl["id"],)).fetchone()
    _snap_b4 = _smlmod._snap_plan(_row_b4)               # row == before
    _intended_b4 = dict(_snap_b4, price_cents=1899)       # != row
_v4 = _smlmod._classify_db_commit_outcome(
    int(_pl["id"]), _intended_b4, _snap_b4,
    int(_op_id), int(ts) - 3600, "plan.update")
check("B4 exact-before row + no matching audit → PROVEN_ROLLBACK (仅此可 restore)",
      _v4[0] == "rollback", str(_v4))
check("B4b UNRESOLVED verdict 全程零 provider restore",
      len(_patches) == _p_before_b,
      f"patches={len(_patches)} before={_p_before_b}")

# H2: mixed-field durable commit + ACK-loss → the exact-pair audit
#     proves THIS operation (noncommercial mutation included) → SUCCESS
with db() as con:
    _row_h2 = con.execute("select name from plans where id=%s",
                          (_pl["id"],)).fetchone()
    _name_h2 = _row_h2["name"]
_dbmod.db = _db_commit_durable_then_raise
_sml.db = _db_commit_durable_then_raise
try:
    _r_h2 = client.patch(f"/samelabs/plans/{_pl['id']}",
                         json={"price_cents": 150,
                               "name": _name_h2 + "_H2"},
                         cookies={COOKIE_NAME: op_ck})
except Exception:
    _r_h2 = None
finally:
    _dbmod.db = _orig_db
    _sml.db = _orig_sml_db
with db() as con:
    _row_h2b = con.execute("select name, price_cents from plans"
                           " where id=%s", (_pl["id"],)).fetchone()
check("H2 mixed-field durable commit + ACK-loss → exact-pair audit 证 SUCCESS (name 已落)",
      _r_h2 is not None and _r_h2.status_code == 200
      and _row_h2b["name"] == _name_h2 + "_H2",
      f"st={_r_h2 and _r_h2.status_code} name={_row_h2b['name']!r}"
      f" expect={_name_h2 + '_H2'!r}")

# H3: full third-state row → UNRESOLVED (never rollback/restore)
with db() as con:
    con.execute("update plans set monthly_credits=777 where id=%s",
                (_pl["id"],))
    con.execute("delete from operator_audit where target_type='plan'"
                " and target_id=%s", (str(_pl["id"]),))
_v_h3 = _smlmod._classify_db_commit_outcome(
    int(_pl["id"]), _snap_b3, _before_b3,
    int(_op_id), int(ts) - 3600, "plan.update")
check("H3 third-state row (monthly_credits 变) → UNRESOLVED",
      _v_h3[0] == "unresolved", str(_v_h3))
# restore for the sections that follow
with db() as con:
    con.execute("update plans set monthly_credits=100 where id=%s",
                (_pl["id"],))

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
