"""repo: ledger — 账本/计量事实表数据访问 (批1b).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
双池口径常量 SPENDABLE_KINDS 属 economy 域服务, 由调用方传入 kinds。
"""

from typing import Optional, Sequence


def insert_row(con, user_id: int, delta: int, balance_after: int, kind: str,
               ref_type: Optional[str], ref_id: Optional[str],
               idem_key: Optional[str], note: Optional[str],
               created_at: int) -> dict:
    return con.execute(
        """
        INSERT INTO ledger (user_id, delta, balance_after, kind,
                            ref_type, ref_id, idem_key, note, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (user_id, delta, balance_after, kind, ref_type, ref_id,
         idem_key, note, created_at)).fetchone()


def by_idem_key(con, user_id: int, idem_key: str) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM ledger WHERE user_id = %s AND idem_key = %s",
        (user_id, idem_key)).fetchone()


def idem_key_exists(con, user_id: int, idem_key: str) -> bool:
    return con.execute(
        "SELECT 1 FROM ledger WHERE user_id = %s AND idem_key = %s LIMIT 1",
        (user_id, idem_key)).fetchone() is not None


def total_balance(con, user_id: int) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(delta), 0) AS bal FROM ledger WHERE user_id = %s",
        (user_id,)).fetchone()
    return int(row["bal"])


def pool_balance(con, user_id: int, kinds: Sequence[str]) -> int:
    row = con.execute(
        """
        SELECT COALESCE(SUM(delta), 0) AS bal FROM ledger
        WHERE user_id = %s AND kind = ANY(%s)
        """,
        (user_id, list(kinds))).fetchone()
    return int(row["bal"])


def sum_kind(con, user_id: int, kind: str) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger "
        "WHERE user_id = %s AND kind = %s",
        (user_id, kind)).fetchone()
    return int(row["s"])


def lock_user(con, user_id: int) -> None:
    """串行化锚点: 锁用户行, 使同用户并发变更排队 (调用方在事务内先调本函数)。"""
    con.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))


def history_paged(con, user_id: int, kinds: Sequence[str],
                  size: int, offset: int) -> tuple:
    total = con.execute(
        "SELECT COUNT(*) AS n FROM ledger WHERE user_id = %s AND kind = ANY(%s)",
        (user_id, list(kinds))).fetchone()["n"]
    rows = con.execute(
        """
        SELECT * FROM ledger WHERE user_id = %s AND kind = ANY(%s)
        ORDER BY id DESC LIMIT %s OFFSET %s
        """,
        (user_id, list(kinds), size, offset)).fetchall()
    return [dict(r) for r in rows], total


# ── provider_earn 投影 (收益页) ──

def earn_by_tool(con, user_id: int, limit: int = 100) -> list:
    return con.execute(
        """
        SELECT l.note AS tool_key, SUM(l.delta) AS earned, COUNT(*) AS payouts
        FROM ledger l
        WHERE l.user_id = %s AND l.kind = 'provider_earn'
        GROUP BY l.note
        ORDER BY earned DESC LIMIT %s
        """,
        (user_id, limit)).fetchall()


def earn_lines_paged(con, user_id: int, limit: int, offset: int) -> tuple:
    total = con.execute(
        "SELECT COUNT(*) AS n FROM ledger WHERE user_id = %s AND kind = 'provider_earn'",
        (user_id,)).fetchone()["n"]
    rows = con.execute(
        """
        SELECT l.delta, l.ref_type, l.ref_id, l.note, l.created_at
        FROM ledger l
        WHERE l.user_id = %s AND l.kind = 'provider_earn'
        ORDER BY l.id DESC LIMIT %s OFFSET %s
        """,
        (user_id, limit, offset)).fetchall()
    return rows, total


def earn_sum_joined_calls(con, user_id: int, tool_key: str) -> int:
    row = con.execute(
        """
        SELECT COALESCE(SUM(l.delta), 0) AS earned
        FROM ledger l
        JOIN tool_calls tc ON l.ref_id = tc.id::text
        WHERE l.user_id = %s AND l.kind = 'provider_earn'
          AND tc.tool_id = %s
        """,
        (user_id, tool_key)).fetchone()
    return int(row["earned"])


# ── 结算 (execution 域闸门在同一事务内调用) ──

def insert_provider_earn_if_absent(con, owner_id: int, payout: int,
                                   balance_after: int, call_id,
                                   idem_key: str, note: str,
                                   created_at: int) -> None:
    """分账行 (kind=provider_earn)。幂等由唯一索引保证, NOT EXISTS 防冲突。"""
    con.execute(
        """
        INSERT INTO ledger (user_id, delta, balance_after, kind,
           ref_type, ref_id, idem_key, note, created_at)
           SELECT %s, %s, %s,
                  'provider_earn', 'tool_call', %s,
                  %s, %s, %s
           WHERE NOT EXISTS (
             SELECT 1 FROM ledger
             WHERE user_id = %s AND idem_key = %s
           )
        """,
        (owner_id, payout, balance_after, str(call_id),
         idem_key, note, created_at,
         owner_id, idem_key))


# ── tool_calls (计量事实) ──

def insert_call_ok(con, client_ref: str, user_id: int, tool_id: str,
                   cost: int, upload_id, result_public_id,
                   result_json, started_at: int, finished_at: int) -> dict:
    return con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
           cost_snapshot, upload_id, result_public_id, params_json,
           result_json, started_at, finished_at)
           VALUES (%s, %s, %s, 'ok', %s, %s, %s, %s, %s, %s, %s)
           RETURNING id
        """,
        (client_ref, user_id, tool_id, cost, upload_id, result_public_id,
         None, result_json, started_at, finished_at)).fetchone()


def insert_call_failed(con, client_ref: str, user_id: int, tool_id: str,
                       status: str, cost: int, error,
                       started_at: int, finished_at: int) -> None:
    con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
           cost_snapshot, error, started_at, finished_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (client_ref, user_id, tool_id, status, cost, error,
         started_at, finished_at))


def call_by_client_ref(con, user_id: int, client_ref: str) -> Optional[dict]:
    """replay 锚点行: 该 ref 的 ok 行 (收口1 部分唯一索引保证至多一行);
    无 ok 行 (全部失败/无历史) → None, 允许重试。"""
    return con.execute(
        "SELECT * FROM tool_calls WHERE user_id = %s AND client_ref = %s "
        "AND status = 'ok' LIMIT 1",
        (user_id, client_ref)).fetchone()


def call_count_by_ref(con, user_id: int, client_ref: str) -> int:
    return con.execute(
        "SELECT COUNT(*) AS n FROM tool_calls WHERE user_id = %s AND client_ref = %s",
        (user_id, client_ref)).fetchone()["n"]


def call_stats_by_tool(con, tool_key: str) -> dict:
    return con.execute(
        """
        SELECT COUNT(*) FILTER (WHERE status = 'ok') AS ok,
               COUNT(*) AS total
        FROM tool_calls WHERE tool_id = %s
        """,
        (tool_key,)).fetchone()


def calls_by_tool_excluding_owner(con, tool_key: str, owner_id: int,
                                  limit: int = 20) -> list:
    return con.execute(
        """
        SELECT status, error, started_at, finished_at, cost_snapshot
        FROM tool_calls WHERE tool_id = %s AND user_id != %s
        ORDER BY id DESC LIMIT %s
        """,
        (tool_key, owner_id, limit)).fetchall()


def ok_calls_by_owner(con, owner_id: int, tool_keys: list) -> int:
    """用户页作者统计: 一组工具键的成功调用总数 (tool_calls.tool_id=tool_key)。"""
    if not tool_keys:
        return 0
    return con.execute(
        """
        SELECT COUNT(*) AS n FROM tool_calls
        WHERE status = 'ok' AND tool_id = ANY(%s) AND user_id != %s
        """,
        (tool_keys, owner_id)).fetchone()["n"]
