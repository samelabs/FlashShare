"""repo: economy — withdrawals 数据访问 (批2a: 经济域专用).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
(1002 收口: activities 函数已拆 — 每日登录 = economy.daily 模块常量,
不经 repo/表。)
"""

from typing import Optional, Sequence


# ── withdrawals ──

def insert_withdrawal(con, user_id: int, amount: int, note, ts: int) -> dict:
    return con.execute(
        """
        INSERT INTO withdrawals
           (user_id, amount, status, note, created_at, txn_ref)
           VALUES (%s, %s, 'pending', %s, %s, NULL) RETURNING *
        """,
        (user_id, amount, note, ts)).fetchone()


def withdrawal_for_update(con, withdrawal_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM withdrawals WHERE id = %s FOR UPDATE",
        (withdrawal_id,)).fetchone()


def finish_withdrawal(con, withdrawal_id: int, status: str, ts: int,
                      admin_user_id, txn_ref) -> dict:
    return con.execute(
        """
        UPDATE withdrawals SET status = %s, processed_at = %s,
           processed_by = %s, txn_ref = COALESCE(%s, txn_ref)
        WHERE id = %s RETURNING *
        """,
        (status, ts, admin_user_id, txn_ref, withdrawal_id)).fetchone()


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


def withdrawals_queue(con, limit: int = 50) -> list:
    return con.execute(
        """
        SELECT w.*, u.username, u.payout_channel FROM withdrawals w
        JOIN users u ON u.id = w.user_id
        ORDER BY (w.status = 'pending') DESC, w.id DESC LIMIT %s
        """,
        (limit,)).fetchall()
