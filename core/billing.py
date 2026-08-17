"""Credits service — the economics core (batch B).

Single-track credits, never expire. The ledger is append-only: every
mutation is a new row; balance is always derivable as SUM(delta). The
authoritative in-transaction balance is carried on each row
(balance_after) so auditing a single row tells you the state at that
moment.

Idempotency: every mutation accepts an idem_key; a unique partial index
on (user_id, idem_key) makes replays a no-op that returns the ORIGINAL
result instead of double-charging.

All functions open their own transaction (with db() as con:) and use
SELECT ... FOR UPDATE on the user row to serialize concurrent mutations
per user. Cross-user concurrency is unaffected.
"""

import psycopg2

from database import db
from utils import now


class InsufficientCredits(Exception):
    """Raised when a consume would drive the balance negative."""

    def __init__(self, balance: int, cost: int):
        super().__init__("err_insufficient_credits")
        self.balance = balance
        self.cost = cost


def get_balance(user_id: int) -> int:
    """Current balance = sum of all ledger deltas. 0 for fresh users."""
    with db() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(delta), 0) AS bal FROM ledger WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    return int(row["bal"])


def _balance_for_update(con, user_id: int) -> int:
    """Serialize on the users row; returns the current balance."""
    con.execute(
        "SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
    row = con.execute(
        "SELECT COALESCE(SUM(delta), 0) AS bal FROM ledger WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return int(row["bal"])


def grant(user_id: int, amount: int, kind: str, *,
          ref_type: str | None = None, ref_id: str | None = None,
          idem_key: str | None = None, note: str | None = None) -> dict:
    """Add credits. Replays (same user_id+idem_key) return the original row.

    kind: grant_signup | grant_plan | grant_package | grant_referral | adjust
    """
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    ts = now()
    with db() as con:
        if idem_key:
            prior = con.execute(
                "SELECT * FROM ledger WHERE user_id = %s AND idem_key = %s",
                (user_id, idem_key),
            ).fetchone()
            if prior:
                return dict(prior)
        bal = _balance_for_update(con, user_id)
        row = con.execute(
            """
            INSERT INTO ledger (user_id, delta, balance_after, kind,
                                ref_type, ref_id, idem_key, note, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, amount, bal + amount, kind, ref_type, ref_id,
             idem_key, note, ts),
        ).fetchone()
    return dict(row)


def consume(user_id: int, cost: int, kind: str, *,
            ref_type: str | None = None, ref_id: str | None = None,
            idem_key: str | None = None, note: str | None = None) -> dict:
    """Deduct credits. Raises InsufficientCredits if balance < cost.

    kind: consume | adjust (negative adjustments go through consume)
    Replays with the same idem_key return the original row (no double charge).
    """
    if cost <= 0:
        raise ValueError("consume cost must be positive")
    ts = now()
    with db() as con:
        if idem_key:
            prior = con.execute(
                "SELECT * FROM ledger WHERE user_id = %s AND idem_key = %s",
                (user_id, idem_key),
            ).fetchone()
            if prior:
                return dict(prior)
        bal = _balance_for_update(con, user_id)
        if bal < cost:
            raise InsufficientCredits(bal, cost)
        row = con.execute(
            """
            INSERT INTO ledger (user_id, delta, balance_after, kind,
                                ref_type, ref_id, idem_key, note, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, -cost, bal - cost, kind, ref_type, ref_id,
             idem_key, note, ts),
        ).fetchone()
    return dict(row)


def refund(user_id: int, amount: int, *,
           ref_type: str | None = None, ref_id: str | None = None,
           idem_key: str | None = None, note: str | None = None) -> dict:
    """Refund a prior charge — a NEW ledger row (delta > 0), never an UPDATE.

    kind is fixed to 'refund'. idem_key semantics identical to grant().
    """
    if amount <= 0:
        raise ValueError("refund amount must be positive")
    ts = now()
    with db() as con:
        if idem_key:
            prior = con.execute(
                "SELECT * FROM ledger WHERE user_id = %s AND idem_key = %s",
                (user_id, idem_key),
            ).fetchone()
            if prior:
                return dict(prior)
        bal = _balance_for_update(con, user_id)
        row = con.execute(
            """
            INSERT INTO ledger (user_id, delta, balance_after, kind,
                                ref_type, ref_id, idem_key, note, created_at)
            VALUES (%s, %s, %s, 'refund', %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, amount, bal + amount, ref_type, ref_id,
             idem_key, note, ts),
        ).fetchone()
    return dict(row)


def lazy_signup_grant(con, user_id: int, amount: int) -> dict | None:
    """Grant the signup bonus lazily INSIDE an existing transaction.

    Called at the single metering point when a user has no ledger rows
    yet (fresh user, first billable event) — the free-tier welcome
    credits land at first need, not at registration, so registration
    stays zero-write for economics.

    Returns the new ledger row, or None if the user already has any
    ledger activity (already granted).
    """
    prior = con.execute(
        "SELECT 1 FROM ledger WHERE user_id = %s LIMIT 1", (user_id,)
    ).fetchone()
    if prior:
        return None
    bal = _balance_for_update(con, user_id)
    row = con.execute(
        """
        INSERT INTO ledger (user_id, delta, balance_after, kind,
                            ref_type, ref_id, idem_key, note, created_at)
        VALUES (%s, %s, %s, 'grant_signup', NULL, NULL, %s, NULL, %s)
        RETURNING *
        """,
        (user_id, amount, bal + amount, f"signup:{user_id}", now()),
    ).fetchone()
    return dict(row)


def history(user_id: int, limit: int = 50, offset: int = 0) -> list:
    """Ledger rows newest-first (for profile + /samelabs)."""
    with db() as con:
        rows = con.execute(
            """
            SELECT * FROM ledger WHERE user_id = %s
            ORDER BY id DESC LIMIT %s OFFSET %s
            """,
            (user_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


# Config: signup grant size. Single source; /samelabs settings can override
# at the metering point (batch C reads settings; this default is the floor).
SIGNUP_GRANT_CREDITS = 100
