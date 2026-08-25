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

import time

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

    The prior-activity check is kind='grant_signup' — NOT "any row".
    Daily-login grants land at register/login time and must NOT short-
    circuit the signup bonus (bug fixed 2026-08-25: fresh users never
    received the 100-credit signup grant because the daily row existed).

    Returns the new ledger row, or None if the signup grant already
    landed, or when amount <= 0 (activity disabled → no-op, not error).
    """
    prior = con.execute(
        "SELECT 1 FROM ledger WHERE user_id = %s AND kind = 'grant_signup' LIMIT 1",
        (user_id,)).fetchone()
    if prior:
        return None
    if amount <= 0:
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


def grant_daily_login(user_id: int, amount: int) -> dict | None:
    """Once-per-UTC-day login bonus. Idem key daily:{uid}:{yyyymmdd}.

    Thin compatibility shim over activities.apply_activity('daily_login'):
    the activities table is the amount source of truth; the `amount`
    argument is only a pre-startup fallback. Callers should prefer
    apply_activity directly.

    Returns the new ledger row, or None if already granted today.
    """
    try:
        from activities import apply_activity
        row = apply_activity("daily_login", user_id)
        if row is not None:
            return row
    except Exception:
        pass
    day = time.strftime("%Y%m%d", time.gmtime())
    idem = f"daily:{user_id}:{day}"
    ts = now()
    with db() as con:
        prior = con.execute(
            "SELECT * FROM ledger WHERE user_id = %s AND idem_key = %s",
            (user_id, idem),
        ).fetchone()
        if prior:
            return None
        bal = _balance_for_update(con, user_id)
        row = con.execute(
            """
            INSERT INTO ledger (user_id, delta, balance_after, kind,
                                ref_type, ref_id, idem_key, note, created_at)
            VALUES (%s, %s, %s, 'grant_daily', NULL, NULL, %s, %s, %s)
            RETURNING *
            """,
            (user_id, amount, bal + amount, idem, None, ts),
        ).fetchone()
    return dict(row)


def daily_granted_today(user_id: int) -> bool:
    """True if the daily login bonus already landed for this UTC day."""
    day = time.strftime("%Y%m%d", time.gmtime())
    with db() as con:
        prior = con.execute(
            "SELECT 1 FROM ledger WHERE user_id = %s AND idem_key = %s",
            (user_id, f"daily:{user_id}:{day}"),
        ).fetchone()
    return prior is not None


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


def history_paged(user_id: int, page: int = 1, size: int = 20) -> tuple[list, int]:
    """(rows_newest_first, total_count) for the account credits page."""
    with db() as con:
        total = con.execute(
            "SELECT COUNT(*) AS n FROM ledger WHERE user_id = %s", (user_id,)
        ).fetchone()["n"]
        rows = con.execute(
            """
            SELECT * FROM ledger WHERE user_id = %s
            ORDER BY id DESC LIMIT %s OFFSET %s
            """,
            (user_id, size, max(0, (page - 1) * size)),
        ).fetchall()
    return [dict(r) for r in rows], total


# Config defaults — HISTORICAL migration values only. The live source of
# truth is the activities table (seeded from these at first boot); the
# metering point reads the table. Kept for pre-startup fallback paths.
SIGNUP_GRANT_CREDITS = 100
DAILY_LOGIN_CREDITS = 5


def signup_amount() -> int:
    """Live signup grant amount from the activities table (0 = disabled)."""
    try:
        from activities import get_activity
        act = get_activity("signup")
        if act and act["active"]:
            return int(act["amount"])
    except Exception:
        pass
    return SIGNUP_GRANT_CREDITS
