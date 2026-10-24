"""repo: ledger — 账本/计量事实表数据访问 (批1b).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
双池口径常量 SPENDABLE_KINDS 属 economy 域服务, 由调用方传入 kinds。
"""

from typing import Optional, Sequence

# 单次调用的净扣费 SQL 表达式 (关联别名 tc): −(consume 计费 + 其退款合计)。
# consume: kind='consume', ref_type='tool_call', ref_id=tc.id::text;
# refund: kind='refund', ref_type='ledger', ref_id=consume 行 id。
# 余额不足 (无 consume 行) 与扣款前失败 = 0; 成功/running = 价格;
# 失败已退款 = 0。五个调用记录投影 (账户调用页 + 后台三面) 共用此单点。
CALL_NET_CHARGE_SQL = """-(COALESCE((
        SELECT c.delta FROM ledger c
        WHERE c.kind = 'consume' AND c.ref_type = 'tool_call'
          AND c.ref_id = tc.id::text), 0)
    + COALESCE((
        SELECT SUM(r.delta) FROM ledger r
        WHERE r.kind = 'refund' AND r.ref_type = 'ledger'
          AND r.ref_id = (SELECT c2.id::text FROM ledger c2
                          WHERE c2.kind = 'consume'
                            AND c2.ref_type = 'tool_call'
                            AND c2.ref_id = tc.id::text)), 0))"""


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


def subscription_grants_by_transaction(con, user_id: int,
                                        txn_id: str) -> list:
    """THIS Creem transaction 实际产生的原始 subscription credits grants:
    kind ∈ (grant_plan, grant_plan_upgrade), ref_type='creem_transaction',
    ref_id=txn_id。纯 persistence query — 求和/反转判定属于 economy 层。"""
    return con.execute(
        """
        SELECT id, delta, kind FROM ledger
        WHERE user_id = %s AND kind IN ('grant_plan', 'grant_plan_upgrade')
          AND ref_type = 'creem_transaction' AND ref_id = %s
        """, (int(user_id), str(txn_id))).fetchall()


def by_idem_key(con, user_id: int, idem_key: str) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM ledger WHERE user_id = %s AND idem_key = %s",
        (user_id, idem_key)).fetchone()


def idem_key_exists(con, user_id: int, idem_key: str) -> bool:
    return con.execute(
        "SELECT 1 FROM ledger WHERE user_id = %s AND idem_key = %s LIMIT 1",
        (user_id, idem_key)).fetchone() is not None


def pool_running_balance(con, user_id: int, kinds: Sequence[str]) -> int:
    """A pool's running balance = balance_after of its latest ledger row
    (0 when the pool has no rows yet). Writers MUST call this while
    holding the user's row lock (lock_user) so consecutive writers chain
    balance_after into a running total; read-only display (e.g.
    spendable_balance) may read without the lock. Equal to SUM(delta)
    over the pool by the running-total invariant."""
    row = con.execute(
        """
        SELECT balance_after FROM ledger
        WHERE user_id = %s AND kind = ANY(%s)
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, list(kinds))).fetchone()
    return int(row["balance_after"]) if row else 0


def sum_kind(con, user_id: int, kind: str) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger "
        "WHERE user_id = %s AND kind = %s",
        (user_id, kind)).fetchone()
    return int(row["s"])


def lock_user(con, user_id: int) -> None:
    """串行化锚点: 锁用户行, 使同用户并发变更排队 (调用方在事务内先调本函数)。"""
    con.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))


def history_count(con, user_id: int, kinds: Sequence[str],
                  since_ts: Optional[int] = None) -> int:
    """Row count for the credits history, optionally time-boxed
    (created_at >= since_ts, epoch seconds). The count is the REAL
    filtered total — the account page bounds its page number against
    it, so history past the global MAX_LIST_PAGE stays reachable while
    offsets stay bounded by the true total."""
    sql = ("SELECT COUNT(*) AS n FROM ledger"
           " WHERE user_id = %s AND kind = ANY(%s)")
    params: list = [user_id, list(kinds)]
    if since_ts is not None:
        sql += " AND created_at >= %s"
        params.append(since_ts)
    return int(con.execute(sql, params).fetchone()["n"])


def history_page(con, user_id: int, kinds: Sequence[str],
                 size: int, offset: int,
                 since_ts: Optional[int] = None) -> list:
    """One page of the user's spendable-pool flow, newest first."""
    sql = ("SELECT * FROM ledger WHERE user_id = %s AND kind = ANY(%s)")
    params: list = [user_id, list(kinds)]
    if since_ts is not None:
        sql += " AND created_at >= %s"
        params.append(since_ts)
    sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
    params += [size, offset]
    return [dict(r) for r in con.execute(sql, params).fetchall()]


SPEND_SUMMARY_KINDS = ("consume", "refund")


def spend_summary(con, user_id: int,
                  since_ts: Optional[int] = None) -> dict:
    """Consumption overview for the credits page, DB-aggregated.

    consume = the magnitude of consume deltas (they are negative on the
    ledger), refund = refund deltas; net = consumption − refunds. Grants,
    top-ups and adjustments are neither consumption nor refunds; the
    earn pool never mixes in. Same time box as the page's range filter
    when since_ts is given. n = consume/refund ROW count — the activity
    fact for the trend strip (zero-amount rows still count; the strip
    must not read as empty just because the net is zero or negative)."""
    sql = ("SELECT kind, COUNT(*) AS n, SUM(delta) AS s FROM ledger"
           " WHERE user_id = %s AND kind = ANY(%s)")
    params: list = [user_id, list(SPEND_SUMMARY_KINDS)]
    if since_ts is not None:
        sql += " AND created_at >= %s"
        params.append(since_ts)
    out = {"consume": 0, "refund": 0, "net": 0, "n": 0}
    for r in con.execute(sql + " GROUP BY kind", params).fetchall():
        out["n"] += int(r["n"])
        if r["kind"] == "consume":
            out["consume"] = -int(r["s"])
        else:
            out["refund"] = int(r["s"])
    out["net"] = out["consume"] - out["refund"]
    return out


def spend_daily(con, user_id: int, since_ts: int) -> dict:
    """Daily consume/refund buckets for the trend strip. Day = the UTC
    day (epoch // 86400); returns {day: {"consume": n, "refund": n}}
    sparse, DB-aggregated."""
    rows = con.execute(
        "SELECT (created_at / 86400) AS d, kind, SUM(delta) AS s"
        " FROM ledger WHERE user_id = %s AND kind = ANY(%s)"
        " AND created_at >= %s GROUP BY 1, 2",
        (user_id, list(SPEND_SUMMARY_KINDS), since_ts)).fetchall()
    out: dict = {}
    for r in rows:
        bucket = out.setdefault(int(r["d"]), {"consume": 0, "refund": 0})
        if r["kind"] == "consume":
            bucket["consume"] += -int(r["s"])
        else:
            bucket["refund"] += int(r["s"])
    return out


def history_tool_refs(con, ledger_ids: list) -> dict:
    """Batched tool identity for credits-history rows — RELATION ONLY.

    consume: ref_type='tool_call', ref_id → tool_calls.id → tool_id.
    refund:  ref_type='ledger', ref_id → the consume row → same chain.
    note is human/audit text and is never parsed here (same rule as
    earn_by_tool). One query covers the whole page; returns
    {ledger_id: {"tool_key": str, "label_json": str|None,
                 "call_id": int}} for rows whose relation resolves to a
    stored tool row (call_id rides the same relation: the credits page
    links the row to the call); rows without a resolvable relation are
    absent from the map."""
    if not ledger_ids:
        return {}
    rows = con.execute(
        """
        SELECT l.id AS ledger_id, pt.tool_key, pt.label_json,
               COALESCE(tc.id, tc2.id) AS call_id
        FROM ledger l
        LEFT JOIN tool_calls tc
          ON l.kind = 'consume' AND l.ref_type = 'tool_call'
         AND l.ref_id = tc.id::text
        LEFT JOIN ledger src
          ON l.kind = 'refund' AND l.ref_type = 'ledger'
         AND src.kind = 'consume' AND l.ref_id = src.id::text
        LEFT JOIN tool_calls tc2
          ON src.ref_type = 'tool_call' AND tc2.id::text = src.ref_id
        LEFT JOIN platform_tools pt
          ON pt.tool_key = COALESCE(tc.tool_id, tc2.tool_id)
        WHERE l.id = ANY(%s)
        """,
        (list(ledger_ids),)).fetchall()
    return {int(r["ledger_id"]): {"tool_key": r["tool_key"],
                                  "label_json": r["label_json"],
                                  "call_id": (int(r["call_id"])
                                              if r["call_id"] is not None
                                              else None)}
            for r in rows if r["tool_key"]}


# ── provider_earn 投影 (收益页) ──

def earn_by_tool(con, user_id: int, limit: int = 100) -> list:
    """Provider earnings grouped by tool. Tool identity comes ONLY from the
    ledger→tool_calls relation (ref_type='tool_call', ref_id=tool_calls.id);
    note is human/audit text, never a tool key. Rows whose ref does not
    resolve to a real tool_call are unattributed — excluded from per-tool
    rows (they still count in totals via sum_kind)."""
    return con.execute(
        """
        SELECT tc.tool_id AS tool_key, SUM(l.delta) AS earned, COUNT(*) AS payouts
        FROM ledger l
        JOIN tool_calls tc
          ON l.ref_type = 'tool_call' AND l.ref_id = tc.id::text
        WHERE l.user_id = %s AND l.kind = 'provider_earn'
        GROUP BY tc.tool_id
        ORDER BY earned DESC LIMIT %s
        """,
        (user_id, limit)).fetchall()


def earn_lines_paged(con, user_id: int, limit: int, offset: int) -> tuple:
    total = con.execute(
        "SELECT COUNT(*) AS n FROM ledger WHERE user_id = %s AND kind = 'provider_earn'",
        (user_id,)).fetchone()["n"]
    rows = con.execute(
        """
        SELECT l.delta, l.ref_type, l.ref_id, l.note, l.created_at, tc.tool_id
        FROM ledger l
        LEFT JOIN tool_calls tc
          ON l.ref_type = 'tool_call' AND l.ref_id = tc.id::text
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
        JOIN tool_calls tc
          ON l.ref_type = 'tool_call' AND l.ref_id = tc.id::text
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


def running_calls(con) -> list:
    """startup recovery worklist: 全部 status='running' rows, ORDER BY id。
    纯 persistence query — recovery classification 属于 execution.gate。"""
    return con.execute(
        "SELECT id, user_id, tool_id FROM tool_calls"
        " WHERE status = 'running' ORDER BY id").fetchall()


def call_by_result_public_id(con, result_public_id: str) -> Optional[dict]:
    """Result page lookup: the /r/{id} authority — status must be ok."""
    return con.execute(
        "SELECT id, user_id, tool_id, status, result_json, finished_at"
        " FROM tool_calls WHERE result_public_id = %s AND status = 'ok'",
        (result_public_id,)).fetchone()


def call_outcome_for_update(con, call_id: int):
    """行锁读单条 call fact (status, result_json) — success-commit
    outcome reconciliation 的 durable read。纯 persistence query —
    success/rollback/unresolved 分类属于 execution.gate。"""
    return con.execute(
        "SELECT status, result_json FROM tool_calls"
        " WHERE id=%s FOR UPDATE", (int(call_id),)).fetchone()

_HEX64 = set("0123456789abcdef")


def _valid_invocation_hash(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX64 for c in value))


def claim_call(con, client_ref: str, user_id: int, tool_id: str,
               cost: int, params_json: str, started_at: int,
               commission_rate_snapshot: int,
               invocation_hash: str,
               oauth_client_id: str | None = None) -> tuple[dict, bool]:
    """Claim one logical invocation attempt before charging/executing.

    The partial unique index on (user_id, tool_id, client_ref) for
    running|ok rows makes concurrent retries converge here. Failed attempts do
    not block a later retry with the same logical client_ref — BUT only
    for the SAME operation: client_ref is permanently bound to the
    invocation_hash of its first claim. claim_call refuses (claimed=False,
    no INSERT) when the earliest historical row for this ref carries a
    different hash, or a NULL hash that proves nothing; the gate maps
    that to 409 idempotency_conflict.

    commission_rate_snapshot is frozen at claim time and written in the
    SAME INSERT — the only snapshot writer. oauth_client_id likewise:
    the call-creation source (the server-side authentication context of
    the request that CREATED the row — a consented OAuth client id, or
    NULL for every other credential) is stamped here in the claim
    transaction and has exactly ONE writer: this INSERT. A replay answers
    the settled row and never re-attributes it. Settlement reads it from the
    persisted row, never from settings. invocation_hash likewise has
    exactly ONE writer: this claim INSERT (64-char lowercase SHA-256
    hex, runtime-validated). Historical pre-hash rows keep NULL.

    Returns (row, claimed). claimed=False means the returned anchor owns
    the ref — live (running/ok) same-hash anchor, or a hash-mismatch/
    NULL-hash conflict anchor for the gate to 409 on.
    """
    if (not isinstance(commission_rate_snapshot, int)
            or not 0 <= commission_rate_snapshot <= 100):
        raise ValueError("invalid_commission_rate_snapshot")
    if not _valid_invocation_hash(invocation_hash):
        raise ValueError("invalid_invocation_hash")
    # Serialize same-user claims on the user row BEFORE any history read:
    # without this lock, B could read "no history", pause, A could claim
    # hash=A and even reach failed, and B's hash=B INSERT would then sail
    # past the live-only partial unique index — breaking the permanent
    # ref↔hash binding. The lock lives ONLY inside this short claim
    # transaction; it never spans execution.
    con.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
    # client_ref ↔ fingerprint binding: the EARLIEST historical row is
    # the authority. No rows → claim freely.
    hist = con.execute(
        """
        SELECT invocation_hash, status, id FROM tool_calls
        WHERE user_id=%s AND tool_id=%s AND client_ref=%s
        ORDER BY id ASC LIMIT 1
        """,
        (user_id, tool_id, client_ref)).fetchone()
    if hist is not None:
        if hist["invocation_hash"] != invocation_hash:
            # different operation under this ref (or NULL hash that
            # cannot prove same-operation) → conflict, zero side effects
            return dict(hist), False
        # same operation: failed/insufficient history may re-claim
    row = con.execute(
        """
        INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
           cost_snapshot, commission_rate_snapshot, invocation_hash,
           params_json, started_at, oauth_client_id)
        VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, tool_id, client_ref)
          WHERE status IN ('running','ok')
        DO NOTHING
        RETURNING *
        """,
        (client_ref, user_id, tool_id, cost, commission_rate_snapshot,
         invocation_hash, params_json, started_at,
         oauth_client_id)).fetchone()
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


def finalize_call_ok(con, call_id: int,
                     result_public_id,
                     result_json: str, finished_at: int) -> dict:
    """Success finalize — durable result binding 与 status/result 字段
    同一次 UPDATE 写入（tool_calls.upload_id 已退役：无读取方）。"""
    row = con.execute(
        """
        UPDATE tool_calls
        SET status='ok', result_public_id=%s, result_json=%s,
            error=NULL, finished_at=%s
        WHERE id=%s AND status='running'
        RETURNING *
        """,
        (result_public_id, result_json, finished_at,
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


def owner_successful_call_exists(con, tool_key: str, owner_id: int) -> bool:
    """发布门判定: 该工具是否存在"所有者本人发起且成功"的一次调用。
    Reads within the CALLER's transaction (the PATCH visibility write
    and this read share one DB transaction)."""
    return con.execute(
        """
        SELECT 1 FROM tool_calls
        WHERE tool_id = %s AND user_id = %s AND status = 'ok'
        LIMIT 1
        """,
        (tool_key, owner_id)).fetchone() is not None


def live_call_exists(con, user_id: int, tool_key: str, client_ref: str) -> bool:
    """幂等重放探针 (advisory preflight 用, 只读): (user, tool, client_ref)
    是否已有 running|ok 的 tool_calls 行 —— 与 claim_call 收敛所用的
    部分唯一索引同口径。True 时预检直接放行, 重放收敛/409 判定仍归
    gate 的 claim 权威。"""
    return con.execute(
        """
        SELECT 1 FROM tool_calls
        WHERE user_id = %s AND tool_id = %s AND client_ref = %s
          AND status IN ('running','ok')
        LIMIT 1
        """,
        (user_id, tool_key, client_ref)).fetchone() is not None
