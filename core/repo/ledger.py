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


def earn_pool_balance(con, user_id: int) -> int:
    """Earn-pool balance = provider_earn − withdraw movements. This is the
    ONLY basis a provider_earning writer may use for balance_after — the
    old total_balance() basis mixed spendable kinds into an earning row's
    after-balance (wrong semantics on the same column)."""
    row = con.execute(
        "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger "
        "WHERE user_id = %s AND kind IN ('provider_earn', 'withdraw')",
        (user_id,)).fetchone()
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

def claim_call(con, client_ref: str, user_id: int, tool_id: str,
               cost: int, params_json: str, started_at: int) -> tuple[dict, bool]:
    """Claim one logical invocation attempt before charging/executing.

    The partial unique index on (user_id, tool_id, client_ref) for
    running|ok rows makes concurrent retries converge here. Failed attempts do
    not block a later retry with the same logical client_ref.

    Returns (row, claimed). claimed=False means an existing running/ok anchor
    already owns this logical invocation.
    """
    row = con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
           cost_snapshot, params_json, started_at)
        VALUES (%s, %s, %s, 'running', %s, %s, %s)
        ON CONFLICT (user_id, tool_id, client_ref)
          WHERE status IN ('running','ok')
        DO NOTHING
        RETURNING *
        """,
        (client_ref, user_id, tool_id, cost, params_json, started_at)).fetchone()
    if row:
        return dict(row), True
    prior = con.execute(
        """
        SELECT * FROM tool_calls
        WHERE user_id=%s AND tool_id=%s AND client_ref=%s
          AND status IN ('running','ok')
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, tool_id, client_ref)).fetchone()
    return (dict(prior) if prior else {}), False


def finalize_call_ok(con, call_id: int, upload_id: int | None,
                     result_public_id,
                     result_json: str, finished_at: int) -> dict:
    """Success finalize — 唯一 tool_calls.upload_id writer: durable result
    binding 与 status/result 字段同一次 UPDATE 写入。"""
    row = con.execute(
        """
        UPDATE tool_calls
        SET status='ok', upload_id=%s, result_public_id=%s, result_json=%s,
            error=NULL, finished_at=%s
        WHERE id=%s AND status='running'
        RETURNING *
        """,
        (upload_id, result_public_id, result_json, finished_at,
         call_id)).fetchone()
    if not row:
        raise RuntimeError("tool_call_not_running")
    return dict(row)


def finalize_call_failed(con, call_id: int, status: str,
                         error, finished_at: int) -> dict | None:
    row = con.execute(
        """
        UPDATE tool_calls
        SET status=%s, error=%s, finished_at=%s
        WHERE id=%s AND status='running'
        RETURNING *
        """,
        (status, str(error)[:500], finished_at, call_id)).fetchone()
    return dict(row) if row else None


def call_by_tool_client_ref(con, user_id: int, tool_id: str,
                            client_ref: str) -> Optional[dict]:
    """Successful replay anchor scoped to one user + one tool."""
    row = con.execute(
        """
        SELECT * FROM tool_calls
        WHERE user_id=%s AND tool_id=%s AND client_ref=%s AND status='ok'
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, tool_id, client_ref)).fetchone()
    return dict(row) if row else None


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


def ok_calls_by_tools(con, tool_keys) -> dict:
    """批量公开计数: {tool_id: ok_count}。口径与 call_stats_by_tool 一致
    (成功调用 = status='ok'); 无记录的 tool 不出现在结果里 → 调用方按 0 处理。
    一次查询覆盖整页, 消除市场页 N+1。"""
    keys = list(tool_keys)
    if not keys:
        return {}
    rows = con.execute(
        """
        SELECT tool_id, COUNT(*) FILTER (WHERE status = 'ok') AS ok
        FROM tool_calls WHERE tool_id = ANY(%s)
        GROUP BY tool_id
        """,
        (keys,)).fetchall()
    return {r["tool_id"]: int(r["ok"]) for r in rows}


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
