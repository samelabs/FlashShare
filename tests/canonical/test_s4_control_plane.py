#!/usr/bin/env python3
"""S4 canonical: Samelabs Control Plane replacement + Withdrawal workflow
+ operator reporting truth.

Covers (per batch A spec §24):
Control Plane:
  CP1  session operator PASS / anonymous / Bearer token cannot enter
  CP2  Overview asset separation (no cross-asset SUM; USD via money)
  CP3  Users: Spendable Credits != Provider Earnings (separate columns)
  CP4  generic mixed ledger retired (no /samelabs/ledger)
  CP5  Tool Supply uses catalog/pricing facts (classification)
  CP6  Orders read-only (no payment-status mutation endpoint)
  CP7  Membership projection from membership row (not orders)
  CP8  operator audit page renders
Withdrawal:
  W1   request freezes payout destination (snapshot)
  W2   later profile edit does not change existing request
  W3   empty payout destination rejects request (payout_channel_required)
  W4   pending→approved / W5 pending→rejected / W6 approved→paid
  W7   pending→paid rejected
  W8   approve exactly one debit / W9 paid zero second debit
  W10  approval replay / W11 reject replay / W12 paid same-ref replay
  W13  paid different-ref conflict
  W14  concurrent same-provider approvals
  W15  earning settlement vs withdrawal = same user-row serialization
  W16  reconciliation invariant (per-status ledger count)
Schema:
  S1   fresh-schema CHECK invariants (status × fields)
Back-office closure (B1):
  X1   hostile values (query params, usernames, notes, audit JSON, payout
       snapshots/refs, provider ids, plan names) render escaped on every
       operator page
  K1   provider tool keys 'owner/tool': pricing / presentation / enabled
       routes match (encoded and raw), JS builds encoded URLs
  E1   registry enabled toggle (system/operator/provider rows) = one tx
       with operator_audit
  PL1  PATCH plan DB conflicts → 409 sort_order_taken / product_already_bound
  CR1  credits adjust: unknown user 404, out-of-range 422, same action id
       + different amount → 409 action_id_conflict (no movement)
  PF1  price floor: USD only, credits <= price_cents * 10 (packages+plans)
  US1  user search (username/display name/email/id) + read-only detail
  WQ1  withdrawal queue paging + status filter
  RT1  reporting labels: effective Plan column / effective membership
       count / approved-unpaid vs paid / Tool Credits net of refunds
Hygiene: unique per-run names, own plan (never 'free'), full cleanup of
every row created (re-runnable; order-independent vs other suites).
"""
import json
import os
import sys
import threading
import time
import urllib.request

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "core"))
sys.path.insert(0, os.path.join(_REPO, "app"))
from tests.support.cleanup import purge_users  # noqa: E402
os.chdir(_REPO)

FAIL = []
COUNT = 0


def check(name, ok, extra=""):
    global COUNT
    COUNT += 1
    print(("  ✓ " if ok else "  ✗ ") + name + (f"  {extra}" if extra and not ok else ""))
    if not ok:
        FAIL.append(name)


if not os.environ.get("FLASHSHARE_TEST_PORT"):
    sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n")
    sys.exit(2)
_port = os.environ.get("FLASHSHARE_TEST_PORT")
if _port == "8000" or not _port.isdigit():
    # production port (or non-integer) must NEVER be used by a canonical suite
    sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be a non-production"
                     " integer port (8000 is forbidden).\n")
    sys.exit(2)
if not os.environ.get("TEST_DB_URL"):
    sys.stderr.write("ERROR: TEST_DB_URL is required.\n")
    sys.exit(2)
# DATABASE_URL is ALWAYS derived from TEST_DB_URL (assignment, not
# setdefault): an ambient DATABASE_URL must never redirect this suite.
os.environ["DATABASE_URL"] = os.environ["TEST_DB_URL"].replace(
    "postgresql://", "postgresql+psycopg://")
os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")

from database import db, init_db  # noqa: E402

init_db()

from economy import earnings  # noqa: E402
from economy.billing import grant, SPENDABLE_KINDS  # noqa: E402
from economy.money import parse_usd_decimal, millis_to_usd_str  # noqa: E402
from repo import operator_views  # noqa: E402
from repo import economy as economy_repo  # noqa: E402
from utils import now  # noqa: E402

ts = now()

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
            (name, display_name or name, email, status, ts,
             ts)).fetchone()["id"])


op = mkuser(uname("op"), "samelabs")
provider = mkuser(uname("prov"))
caller = mkuser(uname("caller"))
print(f"fixtures: op={op} provider={provider} caller={caller}")


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
                   f"s4seed:{uid}:{ts}:{_rnd.randrange(1 << 30)}",
                   "s4 seed earn", ts)


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
app1 = earnings.approve_withdrawal(w1["id"], admin_user_id=op)
check("W4 approve → approved + reviewed fields",
      app1["status"] == "approved" and app1["reviewed_by"] == op
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
rp = earnings.approve_withdrawal(w1["id"], admin_user_id=op)
check("W10 approve replay → success, stays approved",
      rp["status"] == "approved", rp)
check("W10b replay → zero second ledger row",
      len(ledger_rows(w1["id"], provider)) == 1)

# ═══ W6: approved→paid (zero ledger mutation) ═══
before_n = len(ledger_rows(w1["id"], provider))
paid = earnings.mark_withdrawal_paid(w1["id"], "TXN-ABC-001",
                                     admin_user_id=op)
check("W6 paid fields + payout_ref stored verbatim",
      paid["status"] == "paid" and paid["payout_ref"] == "TXN-ABC-001"
      and paid["paid_by"] == op and paid["paid_at"] is not None, paid)
check("W6b paid → ZERO new ledger rows",
      len(ledger_rows(w1["id"], provider)) == before_n)

# W12 same-ref replay; W13 different-ref conflict
rp2 = earnings.mark_withdrawal_paid(w1["id"], "TXN-ABC-001",
                                    admin_user_id=op)
check("W12 paid same-ref replay → success, ref unchanged",
      rp2["payout_ref"] == "TXN-ABC-001", rp2)
try:
    earnings.mark_withdrawal_paid(w1["id"], "TXN-OTHER",
                                  admin_user_id=op)
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
rej = earnings.reject_withdrawal(w2["id"], admin_user_id=op)
check("W5 reject → rejected + reviewed fields",
      rej["status"] == "rejected" and rej["reviewed_by"] == op, rej)
check("W5b rejected → ZERO withdraw ledger rows",
      len(ledger_rows(w2["id"], provider)) == 0)
# W11 reject replay
rp3 = earnings.reject_withdrawal(w2["id"], admin_user_id=op)
check("W11 reject replay → success stays rejected",
      rp3["status"] == "rejected", rp3)

# ═══ W7: pending→paid forbidden ═══
w3 = earnings.request_withdrawal(provider, 10000, note="w7")
try:
    earnings.mark_withdrawal_paid(w3["id"], "X", admin_user_id=op)
    check("W7 pending→paid forbidden", False, "no raise")
except ValueError as e:
    check("W7 pending→paid forbidden",
          str(e) == "transition_conflict", str(e))
# reject on approved / approve on rejected → conflict
try:
    earnings.reject_withdrawal(w1["id"], admin_user_id=op)
    check("W7b reject on paid → conflict", False, "no raise")
except ValueError as e:
    check("W7b reject on paid → conflict",
          str(e) == "transition_conflict", str(e))
try:
    earnings.approve_withdrawal(w2["id"], admin_user_id=op)
    check("W7c approve on rejected → conflict", False, "no raise")
except ValueError as e:
    check("W7c approve on rejected → conflict",
          str(e) == "transition_conflict", str(e))
# cleanup w3 for reconciliation clarity
earnings.reject_withdrawal(w3["id"], admin_user_id=op)

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
        r = earnings.approve_withdrawal(wid, admin_user_id=op)
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
wid3 = earnings.request_withdrawal(p3, 20000, note="w15")["id"]
errors = []


def _settle():
    # mimic the settlement writer shape: lock user, read pool, insert
    try:
        with db() as con:
            _ledger.lock_user(con, p3)
            base = _ledger.earn_pool_balance(con, p3)
            _ledger.insert_row(con, p3, 5000, base + 5000,
                               "provider_earn", "tool_call", "888888",
                               f"s4race:{p3}", "race earn", ts)
    except Exception as exc:  # pragma: no cover
        errors.append(str(exc))


def _approve3():
    try:
        earnings.approve_withdrawal(wid3, admin_user_id=op)
    except ValueError as exc:
        errors.append("approve:" + str(exc))


t1 = threading.Thread(target=_settle)
t2 = threading.Thread(target=_approve3)
t1.start(); t2.start(); t1.join(); t2.join()
with db() as con:
    bal = _ledger.earn_pool_balance(con, p3)
    n3 = int(con.execute(
        "SELECT COUNT(*) n FROM ledger WHERE kind='withdraw' AND"
        " ref_id=%s", (str(wid3),)).fetchone()["n"])
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
                (caller, st, ts,
                 ts if rev else None, op if rev else None,
                 ts if paid_f else None, op if paid_f else None, ref))
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
            " '   ')", (caller, ts))
    check("S1b blank snapshot rejected", False, "accepted blank")
except Exception:
    check("S1b blank snapshot rejected", True)

# ═══ CP1: operator gate (session vs anonymous vs Bearer) ═══
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app as _app  # noqa: E402
from app.web_auth import create_browser_session, COOKIE_NAME  # noqa: E402

client = TestClient(_app)
sess = create_browser_session(op)
r = client.get("/samelabs", cookies={COOKIE_NAME: sess})
check("CP1a session operator enters Overview",
      r.status_code == 200 and "/samelabs/users" in r.text, r.status_code)
r = client.get("/samelabs", follow_redirects=False)
check("CP1b anonymous redirected", r.status_code in (302, 307, 401),
      r.status_code)
r = client.get("/samelabs/users", follow_redirects=False)
check("CP1c anonymous users page redirected",
      r.status_code in (302, 307, 401), r.status_code)

# Bearer capability token cannot enter — tokens are stored hashed, so
# forge the header shape directly; the gate requires auth_via='session'
# regardless of the token's validity (control plane ≠ token surface)
r = client.get("/samelabs", follow_redirects=False,
               headers={"Authorization": "Bearer fs_test_token_value"})
check("CP1d Bearer token cannot enter control plane",
      r.status_code in (302, 307, 401), r.status_code)

# ═══ CP2: Overview asset separation ═══
with db() as con:
    prod = dict(operator_views.overview_product(con))
    cons = dict(operator_views.overview_consumer(con, SPENDABLE_KINDS, ts))
    prov = dict(operator_views.overview_provider(con))
    plat = dict(operator_views.overview_platform(con))
r = client.get("/samelabs", cookies={COOKIE_NAME: sess})
body = r.text
check("CP2a Overview renders 4 asset cards",
      all(k in body for k in ("产品", "消费积分",
                              "开发者收益", "平台收入")),
      None)
check("CP2b no mixed SUM(ledger) symbol on Overview",
      "SUM(delta)" not in body, None)
check("CP2c USD formatting via money authority (millis_to_usd_str)",
      millis_to_usd_str(int(prov["provider_earn_millis"])) in body,
      prov)
check("CP2d provider earnings accrue as USD-millis facts",
      int(prov["provider_earn_millis"]) > 0, prov)
check("CP2e platform income separated kinds",
      int(plat["commission_millis"]) >= 0
      and int(plat["revenue_millis"]) >= 0, plat)

# ═══ CP3: Users Credits != Earnings, separated columns ═══
grant(caller, 5000, "grant", note="s4 grant", idem_key="s4:g1")
seed_earn(provider, 1000)   # provider has earnings, caller has credits
r = client.get("/samelabs/users", cookies={COOKIE_NAME: sess})
body = r.text
check("CP3a Users page renders separated columns",
      "可花积分" in body and "可提现" in body, None)
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
r = client.get("/samelabs/ledger", cookies={COOKIE_NAME: sess},
               follow_redirects=False)
check("CP4 /samelabs/ledger retired (404/redirect)",
      r.status_code in (404, 302, 307), r.status_code)
_src_all = ""
for root, _d, files in os.walk(os.path.join(_REPO, "app")):
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
        (_T_LOCAL, _T_LOCAL, ts))
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, enabled, featured, sort_order, owner_id, created_at,"
        " updated_at, label_json, endpoint_json, accept_json, format_family)"
        " VALUES (%s, %s, 'http', 'public', TRUE, FALSE, 0,"
        " NULL, %s, 0, '{\"zh\":\"S4H\",\"en\":\"S4H\"}',"
        " '{\"url\":\"https://x.test\"}', '[\".png\"]', 'image')",
        (_T_HTTP, _T_HTTP, ts))
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, enabled, featured, sort_order, owner_id, created_at,"
        " updated_at, label_json, endpoint_json, accept_json, format_family)"
        " VALUES (%s, 's4prov', 'http', 'public', TRUE, FALSE, 0,"
        " %s, %s, 0, '{\"zh\":\"S4P\",\"en\":\"S4P\"}',"
        " '{\"url\":\"https://y.test\"}', '[\".png\"]', 'image')",
        (_T_PROV, provider, ts))
    for k in (_T_LOCAL, _T_HTTP, _T_PROV):
        con.execute("INSERT INTO pricing (tool_id, cost, enabled, updated_at)"
                    " VALUES (%s, 10, TRUE, %s)", (k, ts))
r = client.get("/samelabs/tools", cookies={COOKIE_NAME: sess})
body = r.text
check("CP5a Tool Supply renders 3 classes",
      all(c in body for c in ("Official local", "Operator HTTP",
                              "Provider Tool")), None)
check("CP5b price facts from pricing overlay",
      _T_LOCAL in body and _T_HTTP in body and _T_PROV in body, None)

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
        (caller, _pkg_id, f"s4ord_{_RUN}", _pkg_credits, ts, ts))
r = client.get("/samelabs/commerce/orders", cookies={COOKIE_NAME: sess})
check("CP6a Orders page renders facts",
      r.status_code == 200 and f"s4ord_{_RUN}" in r.text, r.status_code)
# no operator payment-status mutation endpoint exists
_methods = []
for mod in ("commerce",):
    src = open(os.path.join(_REPO, "app", "routes", "samelabs",
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
        (_PLAN_CODE, _fresh_rank(con), ts, ts)).fetchone()["id"])
    con.execute(
        "INSERT INTO memberships (user_id, plan_id,"
        " provider_subscription_id, status, started_at, renews_at)"
        " VALUES (%s, %s, %s, 'active', %s, %s)",
        (caller, pid, _SUB, ts, ts + 86400))
r = client.get("/samelabs/commerce/memberships",
               cookies={COOKIE_NAME: sess})
check("CP7 membership row projects (not orders)",
      r.status_code == 200 and _SUB in r.text, r.status_code)

# ═══ CP8: audit page ═══
r = client.get("/samelabs/audit", cookies={COOKIE_NAME: sess})
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
    r = client.get(path, cookies={COOKIE_NAME: sess})
    ok = r.status_code == 200 and (needle is None or needle in r.text)
    check(f"CP9 {path} renders", ok, r.status_code)

# queue shows snapshot, not live channel
_LIVE = f"LIVE-CHANGED-{_RUN}"
set_channel(provider, _LIVE)
r = client.get("/samelabs/economy/withdrawals?status=paid",
               cookies={COOKIE_NAME: sess})
check("CP9b queue shows request-time snapshot, not live channel",
      _CH1 in r.text and _LIVE not in r.text, None)

# old process endpoint retired
r = client.post(f"/samelabs/withdrawals/{w1['id']}/process",
                json={"approve": True},
                cookies={COOKIE_NAME: sess},
                follow_redirects=False)
check("CP10 old /process bool endpoint retired",
      r.status_code in (404, 405, 302), r.status_code)

# ═══ PM-A: /api/earnings user S4 projection ═══
_json = json
prov_sess = create_browser_session(provider)
set_channel(provider, "USDT-TRC20:UserFace")
r = client.get("/api/earnings", cookies={COOKIE_NAME: prov_sess})
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
    return client.post(path, json=payload, cookies={COOKIE_NAME: sess},
                       follow_redirects=False)

# payout_ref strict types at the paid endpoint (needs an approved row:
# seed a fresh pending → approve)
_wb = earnings.request_withdrawal(provider, 20000, "pm-b")
client.post(f"/samelabs/economy/withdrawals/{_wb['id']}/approve", json={},
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
        " visibility, label_json, enabled, trusted, created_at, updated_at, accept_json, format_family)"
        " VALUES (%s, %s, 'local', 'public', %s, TRUE, FALSE, %s, %s, '[\".png\"]', 'image')",
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

# trust projection: NULL owner ⇒ trusted regardless of flag; provider row ⇒ flag
with db() as con:
    _rows = {r_["tool_key"]: r_["trusted"]
             for r_ in _ovm.tools_page(con)[0]}
_PU = f"{uname('prov')}/untrusted"   # provider keys: owner/tool
_PT = f"{uname('prov')}/trusted"
check("PM-C3 first-party (owner NULL) effective trusted",
      _rows.get(_pt) is True, str(_rows.get(_pt)))
with db() as con:
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, label_json, enabled, trusted, owner_id, created_at,"
        " updated_at, accept_json, format_family) VALUES (%s, 'untrusted',"
        " 'http', 'public', %s, TRUE, FALSE, %s, %s, %s, '[\".png\"]', 'image')",
        (_PU, _json.dumps({"en": "P1"}), provider, now(), now()))
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, label_json, accept_json, format_family, enabled, trusted, owner_id, created_at,"
        " updated_at) VALUES (%s, 'trusted',"
        " 'http', 'public', %s, '[\".png\"]', 'image', TRUE, TRUE, %s, %s, %s)",
        (_PT, _json.dumps({"en": "P2"}), provider, now(), now()))
with db() as con:
    _rows = {r_["tool_key"]: r_["trusted"]
             for r_ in _ovm.tools_page(con)[0]}
check("PM-C3b provider trusted=false → false",
      _rows.get(_PU) is False, str(_rows.get(_PU)))
check("PM-C3c provider trusted=true → true",
      _rows.get(_PT) is True, str(_rows.get(_PT)))
r = client.get("/samelabs/tools", cookies={COOKIE_NAME: sess})
_t = r.text
_ri = _t.find(_PU)
_seg = _t[_ri:_ri+1200] if _ri >= 0 else ""
check("PM-C4 template consumes projected trust (untrusted row shows ✕)",
      "✕" in _seg, None)

# ═════════════ B1: back-office closure ═════════════
from urllib.parse import quote as _q  # noqa: E402
from economy import creem as _creem_mod  # noqa: E402
from economy.billing import consume as _consume, refund as _refund  # noqa: E402
from repo import operator_audit as _oa  # noqa: E402
from repo.ledger import insert_row as _insert_row  # noqa: E402


def _get(path):
    return client.get(path, cookies={COOKIE_NAME: sess},
                      follow_redirects=False)


def _patch_json(path, payload):
    return client.patch(path, json=payload, cookies={COOKIE_NAME: sess},
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
_js_tools = open(os.path.join(_REPO, "static", "pages", "samelabs_tools.js"),
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
          and int(_au["actor_user_id"]) == op,
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
                      " idem_key=%s", (caller, f"adjust:{op}:{_aid}")
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
_js_users = open(os.path.join(_REPO, "static", "pages", "samelabs_users.js"),
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
                (_ex, pid, f"{_SUB}_exp", ts - 90000, ts - 100))
    con.execute("INSERT INTO memberships (user_id, plan_id,"
                " provider_subscription_id, status, started_at)"
                " VALUES (%s, %s, %s, 'active', %s)",
                (_ns, pid, f"{_SUB}_ns", ts))
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
                (_ef, pid, f"{_SUB}_eff", ts, ts + 86400))
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
earnings.reject_withdrawal(_wp["id"], admin_user_id=op)

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
                f"x:pc:{_RUN}", _IMG, ts)
    _insert_row(con, _xu, 1000, 51000, "provider_earn", "tool_call",
                _PAY, f"x:pe:{_RUN}", _IMG, ts)
_xw = earnings.request_withdrawal(_xu, 10000, note="x1")
earnings.approve_withdrawal(_xw["id"], admin_user_id=op)
earnings.mark_withdrawal_paid(_xw["id"], _IMG, admin_user_id=op)
with db() as con:
    con.execute(
        "INSERT INTO orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency,"
        " credits_snapshot, granted, created_at, paid_at)"
        " VALUES (%s, 'package', %s, 'creem', %s, 'paid', 500,"
        " 'USD', %s, TRUE, %s, %s)",
        (_xu, _pkg_id, _PAY, _pkg_credits, ts, ts))
    con.execute(
        "INSERT INTO memberships (user_id, plan_id,"
        " provider_subscription_id, status, started_at, renews_at)"
        " VALUES (%s, %s, %s, 'active', %s, %s)",
        (_xu, pid, _IMG, ts, ts + 86400))
    con.execute(
        "INSERT INTO platform_tools (tool_key, tool_id, execution,"
        " visibility, label_json, accept_json, format_family, enabled, trusted, owner_id, created_at,"
        " updated_at) VALUES (%s, 'xt', 'http', 'public', %s, '[\".png\"]', 'image', TRUE, FALSE,"
        " %s, %s, %s)",
        (f'{_X}/"><b>{_X}', _json.dumps({"en": "X"}), _xu, ts, ts))
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
            (op,)).fetchone()["n"]),
        "running": int(con.execute(
            "SELECT COUNT(*) n FROM tool_calls WHERE user_id = ANY(%s)",
            ([op, provider, caller, _u2, _xu],)).fetchone()["n"]),
        "withdrawals": int(con.execute(
            "SELECT COUNT(*) n FROM withdrawals WHERE user_id = ANY(%s)",
            ([provider, p2, p3, _xu, caller],)).fetchone()["n"]),
    }
check("H1 cleanup leaves zero own rows", not any(_left.values()), _left)

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    sys.exit(1)
print(f"OK — S4 control plane + withdrawal workflow + B1 back-office"
      f" closure ({COUNT} checks)")
