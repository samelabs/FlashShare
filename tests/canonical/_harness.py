"""Canonical suite harness — the one fail-closed environment gate and the
check/report helpers every tests/canonical suite imports first.

Canonical suites run ONLY against an explicitly provided isolated app port
(never 8000, the production port) and an isolated TEST_DB_URL. The gate
runs on import and exits 2 before any request or DB write.
"""
import os
import sys
from typing import Iterable


def _refuse(msg: str) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.exit(2)


TEST_PORT = os.environ.get("FLASHSHARE_TEST_PORT") or ""
if not TEST_PORT:
    _refuse("FLASHSHARE_TEST_PORT is required (isolated app port, never the production port).")
try:
    PORT = int(TEST_PORT, 10)
except ValueError:
    _refuse("FLASHSHARE_TEST_PORT must be an integer port number.")
if PORT == 8000:
    _refuse("FLASHSHARE_TEST_PORT=8000 is the production port; canonical suites need an isolated port.")
TEST_DB = os.environ.get("TEST_DB_URL") or ""
if not TEST_DB:
    _refuse("TEST_DB_URL is required (isolated test database DSN).")

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# resolution order app → core → repo root (core/tools shadows the root tools/ scripts dir)
for _p in (REPO, os.path.join(REPO, "core"), os.path.join(REPO, "app")):
    sys.path.insert(0, _p)
PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
# assignment, not setdefault: an ambient DATABASE_URL must never redirect a suite
os.environ["DATABASE_URL"] = PG.replace("postgresql://", "postgresql+psycopg://")
os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")

FAIL: list = []
_count = 0


def check(name, ok, detail=""):
    global _count
    _count += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


def section(title: str) -> None:
    print(f"\n── {title} ──")


def finish() -> None:
    """Summary + exit code. A suite that ran zero checks fails too."""
    print(f"\n{_count - len(FAIL)}/{_count} checks passed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  ")
    sys.exit(1 if FAIL or not _count else 0)


# Production has NO delete-user surface and the financial/audit fact tables
# RESTRICT user deletion, so fixture cleanup drops those rows first.
_RESTRICT_TABLES = (
    "tool_calls", "ledger", "withdrawals", "orders", "memberships",
    "payment_reviews", "subscription_period_payments", "operator_audit",
    "platform_tools",
)
_FK_COLUMN = {"operator_audit": "actor_user_id", "platform_tools": "owner_id"}


def purge_users(con, user_ids: Iterable[int]) -> None:
    """Delete the RESTRICT fact rows for these users, then the users rows.
    CASCADE tables (sessions, api_keys, uploads, ...) follow automatically."""
    ids = [int(i) for i in user_ids]
    if not ids:
        return
    for tbl in _RESTRICT_TABLES:
        con.execute(f"delete from {tbl} where {_FK_COLUMN.get(tbl, 'user_id')} = ANY(%s)", (ids,))
    con.execute("delete from users where id = ANY(%s)", (ids,))
