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
