"""repo: economy — withdrawals 数据访问 (经济域专用).

S4: withdrawal lifecycle 数据访问 — 参数化 SQL, 无事务/无业务判断。
列名为 final authority (payout_channel_snapshot / reviewed_* / paid_* /
payout_ref); 旧 processed_at/processed_by/txn_ref 不再存在。
"""

from typing import Optional, Sequence


# ── withdrawals ──

def insert_withdrawal(con, user_id: int, amount: int, note, ts: int,
                      payout_channel_snapshot: str) -> dict:
    return con.execute(
        """
        INSERT INTO withdrawals
           (user_id, amount, status, note, payout_channel_snapshot,
            created_at)
           VALUES (%s, %s, 'pending', %s, %s, %s) RETURNING *
        """,
        (user_id, amount, note, payout_channel_snapshot, ts)).fetchone()


def withdrawal_for_update(con, withdrawal_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM withdrawals WHERE id = %s FOR UPDATE",
        (withdrawal_id,)).fetchone()


def mark_reviewed(con, withdrawal_id: int, status: str, ts: int,
                  admin_user_id: int) -> dict:
    """pending → approved | rejected: review fields only, one UPDATE."""
    return con.execute(
        """
        UPDATE withdrawals SET status = %s, reviewed_at = %s,
           reviewed_by = %s
        WHERE id = %s RETURNING *
        """,
        (status, ts, admin_user_id, withdrawal_id)).fetchone()


def mark_paid(con, withdrawal_id: int, ts: int, admin_user_id: int,
              payout_ref: str) -> dict:
    """approved → paid: paid fields + external settlement evidence."""
    return con.execute(
        """
        UPDATE withdrawals SET status = 'paid', paid_at = %s,
           paid_by = %s, payout_ref = %s
        WHERE id = %s RETURNING *
        """,
        (ts, admin_user_id, payout_ref, withdrawal_id)).fetchone()


def mark_payout_failed(con, withdrawal_id: int) -> dict:
    """approved → payout_failed : status flip ONLY — review fields
    stay (they were set at approval), paid fields stay NULL. The earn-pool
    return is a ledger row written by the caller in the SAME transaction."""
    return con.execute(
        """
        UPDATE withdrawals SET status = 'payout_failed'
        WHERE id = %s RETURNING *
        """,
        (withdrawal_id,)).fetchone()


def withdrawals_by_user(con, user_id: int, limit: int, offset: int) -> list:
    return con.execute(
        "SELECT * FROM withdrawals WHERE user_id = %s "
        "ORDER BY id DESC LIMIT %s OFFSET %s",
        (user_id, limit, offset)).fetchall()


def withdrawal_count(con, user_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) AS n FROM withdrawals WHERE user_id = %s",
        (user_id,)).fetchone()["n"]


def withdrawal_sum(con, user_id: int, statuses: Sequence[str]) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM withdrawals "
        "WHERE user_id = %s AND status = ANY(%s)",
        (user_id, list(statuses))).fetchone()
    return int(row["s"])


def withdrawal_sum_excluding(con, user_id: int, statuses: Sequence[str],
                             exclude_id: int) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM withdrawals "
        "WHERE user_id = %s AND status = ANY(%s) AND id != %s",
        (user_id, list(statuses), exclude_id)).fetchone()
    return int(row["s"])


def withdrawals_queue(con, limit: int = 50, offset: int = 0,
                      status: str | None = None) -> tuple[list, int]:
    """Operator queue (paged; optional exact status filter): payout
    destination comes from the REQUEST-TIME snapshot column, never a
    live users.payout_channel join. Returns (rows, total)."""
    rows = con.execute(
        """
        SELECT w.*, u.username FROM withdrawals w
        JOIN users u ON u.id = w.user_id
        WHERE (%s::text IS NULL OR w.status = %s::text)
        ORDER BY (w.status = 'pending') DESC, w.id DESC LIMIT %s OFFSET %s
        """,
        (status, status, limit, offset)).fetchall()
    total = con.execute(
        "SELECT COUNT(*) AS n FROM withdrawals w"
        " WHERE (%s::text IS NULL OR w.status = %s::text)",
        (status, status)).fetchone()["n"]
    return rows, int(total)
