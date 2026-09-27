"""Test-only user purge helper (B2b-2).

Production has NO delete-user surface, and the financial/audit fact
tables now RESTRICT user deletion. Canonical suites that clean up their
random users must drop the RESTRICT-first rows explicitly — this module
is that single ordering, so suites never hand-roll it.
"""
from typing import Iterable

# order matters only for readability: none of these reference each other
_RESTRICT_TABLES = (
    "tool_calls", "ledger", "withdrawals", "orders", "memberships",
    "payment_reviews", "subscription_period_payments", "operator_audit",
    "platform_tools",
)
# column naming exceptions on the users FK
_FK_COLUMN = {"operator_audit": "actor_user_id",
              "platform_tools": "owner_id"}


def purge_users(con, user_ids: Iterable[int]) -> None:
    """Delete the RESTRICT fact rows for these users, then the users
    rows. CASCADE tables (sessions, api_keys, uploads, ...) follow the
    users delete automatically."""
    ids = [int(i) for i in user_ids]
    if not ids:
        return
    for tbl in _RESTRICT_TABLES:
        col = _FK_COLUMN.get(tbl, "user_id")
        con.execute(
            f"delete from {tbl} where {col} = ANY(%s)", (ids,))
    con.execute("delete from users where id = ANY(%s)", (ids,))
