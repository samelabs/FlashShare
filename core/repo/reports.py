"""repo: reports — 后台运营报表查询 (批1c).

跨表只读投影 (operator 专用)。规则同 repo: 参数化, 无事务, 无业务判断。
"""

from typing import Optional


def overview_stats(con) -> dict:
    """站级概览计数 (单连接多查, 服务层一次 with 内调用)。"""
    def one(sql):
        return con.execute(sql).fetchone()
    users = one("SELECT COUNT(*) n FROM users")["n"]
    uploads = one("SELECT COUNT(*) n FROM uploads")["n"]
    calls_ok = one("SELECT COUNT(*) n FROM tool_calls WHERE status='ok'")["n"]
    calls_failed = one("SELECT COUNT(*) n FROM tool_calls WHERE status<>'ok'")["n"]
    credits_granted = one("SELECT COALESCE(SUM(delta),0) n FROM ledger WHERE delta>0")["n"]
    credits_consumed = one("SELECT COALESCE(-SUM(delta),0) n FROM ledger WHERE delta<0")["n"]
    return {
        "users": users,
        "uploads": uploads,
        "calls_ok": calls_ok,
        "calls_failed": calls_failed,
        "credits_granted": credits_granted,
        "credits_consumed": credits_consumed,
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


def users_overview(con, q: Optional[str]) -> list:
    """用户列表 + 余额/成功调用数 子查询投影; q=ILIKE 搜索或 None。"""
    base = """
        SELECT u.id, u.username, u.display_name, u.status, u.created_at,
               u.last_login_at, u.avatar_url,
               COALESCE((SELECT SUM(delta) FROM ledger l WHERE l.user_id=u.id), 0) balance,
               (SELECT COUNT(*) FROM tool_calls tc WHERE tc.user_id=u.id
                  AND tc.status='ok') calls_ok
        FROM users u
    """
    if q:
        return con.execute(
            base + "WHERE u.username ILIKE %s OR u.display_name ILIKE %s "
                   "ORDER BY u.id DESC LIMIT 100",
            (f"%{q}%", f"%{q}%")).fetchall()
    return con.execute(
        base + "ORDER BY u.id DESC LIMIT 100").fetchall()


def ledger_page(con, size: int, offset: int) -> tuple:
    total = con.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]
    rows = con.execute(
        """SELECT l.*, u.username FROM ledger l
           LEFT JOIN users u ON u.id = l.user_id
           ORDER BY l.id DESC LIMIT %s OFFSET %s""",
        (size, offset)).fetchall()
    return rows, total
