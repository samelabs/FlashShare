"""repo: reports — 后台运营报表查询 (批1c).

跨表只读投影 (operator 专用)。规则同 repo: 参数化, 无事务, 无业务判断。
"""

from typing import Optional, Sequence


def overview_stats(con, spendable_kinds: Sequence[str],
                   platform_user_id: int) -> dict:
    """站级概览 (单连接多查)。积分口径 = 双池: 消费池 (spendable_kinds,
    由 economy 域传入) 与收益池 (provider_earn − withdraw) 分列; 平台
    记账主体不计入用户数/用户负债。"""
    def one(sql, params=()):
        return con.execute(sql, params).fetchone()["n"]
    return {
        "users": one("SELECT COUNT(*) n FROM users WHERE id <> %s",
                     (platform_user_id,)),
        "uploads": one("SELECT COUNT(*) n FROM uploads"),
        "calls_ok": one("SELECT COUNT(*) n FROM tool_calls WHERE status='ok'"),
        "calls_failed": one("SELECT COUNT(*) n FROM tool_calls WHERE status='failed'"),
        "calls_running": one("SELECT COUNT(*) n FROM tool_calls WHERE status='running'"),
        "spendable_liability": one(
            "SELECT COALESCE(SUM(delta),0) n FROM ledger"
            " WHERE kind = ANY(%s) AND user_id <> %s",
            (list(spendable_kinds), platform_user_id)),
        "earn_liability": one(
            "SELECT COALESCE(SUM(delta),0) n FROM ledger"
            " WHERE kind IN ('provider_earn','withdraw') AND user_id <> %s",
            (platform_user_id,)),
        "pending_withdrawals": one(
            "SELECT COALESCE(SUM(amount),0) n FROM withdrawals"
            " WHERE status='pending'"),
    }


def recent_calls(con, limit: int = 15) -> list:
    return con.execute(
        """SELECT tc.*, u.username FROM tool_calls tc
           LEFT JOIN users u ON u.id = tc.user_id
           ORDER BY tc.id DESC LIMIT %s""", (limit,)).fetchall()


def top_tools(con, limit: int = 10) -> list:
    return con.execute(
        """SELECT tool_id, COUNT(*) n FROM tool_calls
           GROUP BY tool_id ORDER BY n DESC LIMIT %s""", (limit,)).fetchall()


def users_overview(con, q: Optional[str],
                   spendable_kinds: Sequence[str]) -> list:
    """用户列表: 双池余额 (消费池 / 收益池) + 成功调用数 + 当前 membership
    原始行 (有效性由调用方经 economy.subscription 单点判定)。
    q: 数字 = 精确 id; 否则 username / display_name / email ILIKE。"""
    base = """
        SELECT u.id, u.username, u.display_name, u.email, u.status,
               u.created_at, u.last_login_at, u.avatar_url,
               COALESCE((SELECT SUM(delta) FROM ledger l WHERE l.user_id=u.id
                           AND l.kind = ANY(%s)), 0) spendable,
               COALESCE((SELECT SUM(delta) FROM ledger l WHERE l.user_id=u.id
                           AND l.kind IN ('provider_earn','withdraw')), 0) earn,
               (SELECT COUNT(*) FROM tool_calls tc WHERE tc.user_id=u.id
                  AND tc.status='ok') calls_ok,
               p.name AS plan_name, mm.status AS member_status, mm.renews_at
        FROM users u
        LEFT JOIN memberships mm ON mm.user_id = u.id
        LEFT JOIN plans p ON p.id = mm.plan_id
    """
    kinds = list(spendable_kinds)
    if q and q.isdigit():
        return con.execute(base + "WHERE u.id = %s", (kinds, int(q))).fetchall()
    if q:
        like = f"%{q}%"
        return con.execute(
            base + "WHERE u.username ILIKE %s OR u.display_name ILIKE %s"
                   " OR u.email ILIKE %s ORDER BY u.id DESC LIMIT 100",
            (kinds, like, like, like)).fetchall()
    return con.execute(
        base + "ORDER BY u.id DESC LIMIT 100", (kinds,)).fetchall()


def ledger_page(con, size: int, offset: int) -> tuple:
    total = con.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]
    rows = con.execute(
        """SELECT l.*, u.username FROM ledger l
           LEFT JOIN users u ON u.id = l.user_id
           ORDER BY l.id DESC LIMIT %s OFFSET %s""",
        (size, offset)).fetchall()
    return rows, total


# ── operator read models (P3: orders / memberships / calls / shares /
#    audit / user detail). Read-only projections; filters are optional
#    exact-match values validated by the route. ──

def orders_page(con, status: Optional[str], q: Optional[str], limit: int = 100) -> list:
    """Orders newest-first. q: digits = user id or order id; otherwise
    username or provider order/checkout id (exact)."""
    where, args = [], []
    if status:
        where.append("o.status = %s"); args.append(status)
    if q:
        if q.isdigit():
            where.append("(o.user_id = %s OR o.id = %s)"); args += [int(q), int(q)]
        else:
            where.append("(u.username = %s OR o.provider_order_id = %s"
                         " OR o.provider_order_id = %s)")
            args += [q, q, "ch:" + q]
    sql = """
        SELECT o.*, u.username,
               COALESCE(pk.name, pl.name) AS item_name
        FROM orders o
        JOIN users u ON u.id = o.user_id
        LEFT JOIN packages pk ON o.kind = 'package' AND pk.id = o.item_id
        LEFT JOIN plans pl ON o.kind = 'plan' AND pl.id = o.item_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.id DESC LIMIT %s"
    return con.execute(sql, (*args, limit)).fetchall()


def memberships_page(con, limit: int = 200) -> list:
    """All current Membership rows (one per user) with plan + pending
    downgrade target. Validity is decided by the caller's single point."""
    return con.execute(
        """
        SELECT m.*, u.username, p.code AS plan_code, p.name AS plan_name,
               pd.effective_at AS downgrade_at, tp.name AS downgrade_to
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        JOIN plans p ON p.id = m.plan_id
        LEFT JOIN pending_downgrades pd ON pd.user_id = m.user_id
        LEFT JOIN plans tp ON tp.id = pd.target_plan_id
        ORDER BY m.updated_at DESC NULLS LAST, m.user_id DESC
        LIMIT %s
        """, (limit,)).fetchall()


def calls_page(con, status: Optional[str], tool_id: Optional[str],
               user_id: Optional[int], limit: int = 100) -> list:
    where, args = [], []
    if status:
        where.append("tc.status = %s"); args.append(status)
    if tool_id:
        where.append("tc.tool_id = %s"); args.append(tool_id)
    if user_id is not None:
        where.append("tc.user_id = %s"); args.append(user_id)
    sql = """
        SELECT tc.id, tc.user_id, u.username, tc.tool_id, tc.status,
               tc.cost_snapshot, tc.error, tc.result_public_id,
               tc.started_at, tc.finished_at
        FROM tool_calls tc LEFT JOIN users u ON u.id = tc.user_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY tc.id DESC LIMIT %s"
    return con.execute(sql, (*args, limit)).fetchall()


def uploads_page(con, q: Optional[str], limit: int = 100) -> list:
    """Shares newest-first. q: exact public id, else username."""
    sql = """
        SELECT up.id, up.public_id, up.user_id, u.username, up.file_name,
               up.content_type, up.size_bytes, up.expire_at, up.created_at
        FROM uploads up JOIN users u ON u.id = up.user_id
    """
    if q:
        return con.execute(sql + " WHERE up.public_id = %s OR u.username = %s"
                           " ORDER BY up.id DESC LIMIT %s", (q, q, limit)).fetchall()
    return con.execute(sql + " ORDER BY up.id DESC LIMIT %s", (limit,)).fetchall()


def audit_page(con, action: Optional[str], limit: int = 100) -> list:
    sql = """
        SELECT a.*, u.username AS actor
        FROM operator_audit a LEFT JOIN users u ON u.id = a.actor_user_id
    """
    if action:
        return con.execute(sql + " WHERE a.action = %s ORDER BY a.id DESC LIMIT %s",
                           (action, limit)).fetchall()
    return con.execute(sql + " ORDER BY a.id DESC LIMIT %s", (limit,)).fetchall()


def audit_actions(con) -> list:
    return [r["action"] for r in con.execute(
        "SELECT DISTINCT action FROM operator_audit ORDER BY action").fetchall()]


def user_ledger(con, user_id: int, limit: int = 50) -> list:
    return con.execute(
        "SELECT * FROM ledger WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit)).fetchall()


def user_token_counts(con, user_id: int) -> dict:
    row = con.execute(
        "SELECT COUNT(*) FILTER (WHERE status='active') AS active,"
        " COUNT(*) AS total FROM api_tokens WHERE user_id = %s",
        (user_id,)).fetchone()
    return {"active": int(row["active"]), "total": int(row["total"])}


def user_withdrawals(con, user_id: int, limit: int = 20) -> list:
    return con.execute(
        "SELECT * FROM withdrawals WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit)).fetchall()
