"""repo: economy — activities/withdrawals 数据访问 (批2a: 经济域专用).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
"""

from typing import Optional, Sequence


# ── activities ──

def activity_by_code(con, code: str) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM activities WHERE code = %s", (code,)).fetchone()


def all_activities(con) -> list:
    return con.execute("SELECT * FROM activities ORDER BY code").fetchall()


def seed_activity(con, code: str, activity_type: str, amount: int,
                  kind: str, starts_at, ends_at, label_json, ts: int) -> int:
    cur = con.execute(
        """
        INSERT INTO activities (code, activity_type, amount, kind,
                                active, starts_at, ends_at, label_json,
                                updated_at)
        VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s)
        ON CONFLICT (code) DO NOTHING
        """,
        (code, activity_type, amount, kind, starts_at, ends_at,
         label_json, ts))
    return cur.rowcount


def update_activity(con, code: str, amount, active, starts_at, ends_at,
                    starts_clear: bool, ends_clear: bool,
                    ts: int, admin_user_id) -> dict:
    return con.execute(
        """
        UPDATE activities SET
          amount     = COALESCE(%s, amount),
          active     = COALESCE(%s, active),
          starts_at  = CASE WHEN %s THEN NULL ELSE COALESCE(%s, starts_at) END,
          ends_at    = CASE WHEN %s THEN NULL ELSE COALESCE(%s, ends_at) END,
          updated_at = %s,
          updated_by = %s
        WHERE code = %s
        RETURNING *
        """,
        (amount, active,
         starts_clear, starts_at, ends_clear, ends_at,
         ts, admin_user_id, code)).fetchone()


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
