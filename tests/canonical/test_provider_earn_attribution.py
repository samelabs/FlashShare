#!/usr/bin/env python3
"""S2 canonical — provider-earn tool attribution authority.

The ONLY attribution path: ledger(kind='provider_earn',
ref_type='tool_call', ref_id) → tool_calls.id → tool_calls.tool_id.
note is human/audit text and is never parsed for tool identity.

Checks (real DB, fresh rows, consecutive runs stay green):
  1. same tool, two payouts, different notes → one aggregated row
  2. note naming the WRONG tool → attribution unchanged
  3. two tools, identical notes → still attributed separately
  4. revenue line tool identity comes from tc.tool_id (note scrambled)
  5. provider_earn with ref_type != 'tool_call' whose ref_id happens to
     equal a real tool_calls.id → NOT attributed to that tool
  6. earnings amounts / summary totals unchanged by this projection
  7. unresolvable ref_id → unattributed (not in per-tool rows)
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
_u = "s2p" + _os.urandom(4).hex()

from database import db
from repo import ledger as ledger_repo
from economy import earnings


def _user(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])


def _tool_call(con, uid, tool_id):
    row = con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, started_at, finished_at, invocation_hash)"
        " values (%s, %s, %s, 'ok', 500, %s, %s, %s) returning id",
        (f"{_u}-{tool_id}-{_os.urandom(3).hex()}", uid, tool_id, ts, ts,
         _os.urandom(8).hex())).fetchone()
    return int(row["id"])


def _earn(con, uid, delta, ref_type, ref_id, note):
    bal = ledger_repo.earn_pool_balance(con, uid) + delta
    con.execute(
        "insert into ledger (user_id, delta, balance_after, kind, ref_type,"
        " ref_id, idem_key, note, created_at) values (%s, %s, %s,"
        " 'provider_earn', %s, %s, %s, %s, %s)",
        (uid, delta, bal, ref_type, ref_id, f"{_u}-{_os.urandom(4).hex()}",
         note, ts))

# ── fixture: provider with two tools ──
uid = _user(_u + "prov")
T1, T2 = "s2tool-alpha", "s2tool-beta"
with db() as con:
    # 1+2: same tool, two payouts, notes differ; second note names the WRONG tool
    c1 = _tool_call(con, uid, T1)
    c2 = _tool_call(con, uid, T1)
    _earn(con, uid, 300, "tool_call", str(c1), f"{T1} payout")
    _earn(con, uid, 200, "tool_call", str(c2), f"{T2} PAYOUT (wrong name in note)")
    # 3: two tools, identical notes
    c3 = _tool_call(con, uid, T2)
    _earn(con, uid, 150, "tool_call", str(c3), "identical note")
    _earn(con, uid, 250, "tool_call", str(c2), "identical note")
    # 5: provider_earn with ref_type != tool_call, ref_id == c1 (collision bait)
    _earn(con, uid, 999, "withdraw", str(c1), f"{T1} payout")
    # 7: ref_id that resolves to nothing
    _earn(con, uid, 111, "tool_call", "999999999999", f"{T1} payout")

# 1+2: per-tool aggregation is note-independent; wrong-note row stays in T1
rows = earnings.per_tool_revenue(uid)
by_key = {r["tool_key"]: r for r in rows}
check("1 same tool/diff notes → one row", T1 in by_key and by_key[T1]["payouts"] == 3,
      rows)
check("2 wrong-tool note → attribution unchanged",
      by_key.get(T1, {}).get("earned") == 300 + 200 + 250, by_key.get(T1))

# 3: identical notes do not merge tools
check("3 same note/two tools → separate rows",
      T2 in by_key and by_key[T2]["earned"] == 150 and len(by_key) == 2, rows)

# 4: revenue line tool identity = tc.tool_id even with scrambled notes
lines, total = earnings.revenue_lines(uid, limit=50, offset=0)
ok4 = all(
    (ln["call_id"] in (str(c1), str(c2), str(c3)) and ln["tool"] in (T1, T2))
    or ln["tool"] == "?"            # unresolvable-ref line is unattributed
    for ln in lines)
t1_lines = [ln for ln in lines if ln["tool"] == T1]
check("4 revenue line tool ← tool_calls.tool_id",
      ok4 and len(t1_lines) == 3 and total == 6, (lines, total))

# 5: ref_type='withdraw' colliding with a real call id → not attributed
check("5 colliding ref_type ≠ tool_call → excluded from tool sums",
      by_key[T1]["earned"] == 750 and by_key[T2]["earned"] == 150,
      (by_key[T1], by_key[T2]))

# 6: totals come from sum_kind (all six rows), independent of attribution
s = earnings.earnings_summary(uid)
check("6 summary totals unchanged by projection",
      s["earned"] == 300 + 200 + 150 + 250 + 999 + 111 and s["available"] == s["earned"],
      s)

# 7: unresolvable ref AND wrong-ref_type row → both unattributed ('?')
unattr = [ln for ln in lines if ln["tool"] == "?"]
check("7 unresolvable refs → unattributed",
      len(unattr) == 2 and {u["delta"] for u in unattr} == {111, 999},
      unattr)

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — provider earn attribution authority ({COUNT} checks)")
