"""platform principal — 平台账户 (0930 经济模型闭合).

两条平台收入路径的记账主体:
  A. 用户工具抽佣: cost 的 (1-payout) 部分 → kind='platform_commission'
  B. 系统工具直销: owner_id IS NULL 的 OK 调用全额 → kind='platform_revenue'

不变量 (守恒): 每笔计费调用 cost = 作者 payout + 平台收入 (A)
            / cost = 平台收入 (B)。平台账户只记账不流通 (不可消费/不可提现)。

账户形态: users 表内的真实行 (username='platform', status='suspended',
无 password_hash/email → 不可登录), ledger.user_id FK 天然满足。
"""

from __future__ import annotations

import logging

from database import db
from repo import ledger as ledger_repo
from repo import users as users_repo
from utils import now

logger = logging.getLogger("flashshare")

PLATFORM_USERNAME = "platform"

# 启动时缓存的平台账户 id (ensure_platform_account 填充)
_platform_id: int | None = None


def ensure_platform_account(con=None) -> int:
    """幂等创建平台账户 (真实 users 行: status='suspended', 无凭据 →
    authenticate_local 因 password_hash 为空直接 None, 不可登录)。
    返回 platform user id。"""
    if con is not None:
        return _ensure(con)
    with db() as c:
        return _ensure(c)


def _ensure(con) -> int:
    global _platform_id
    if _platform_id is not None:
        row = users_repo.by_id(con, _platform_id)
        if row:
            return _platform_id
        _platform_id = None  # 账户被删 (不应发生), 重建
    row = con.execute(
        "SELECT id FROM users WHERE username = %s", (PLATFORM_USERNAME,)
    ).fetchone()
    if row:
        _platform_id = row["id"]
        return _platform_id
    ts = now()
    created = users_repo.insert_local(con, PLATFORM_USERNAME, None, None,
                                      "518.PLUS Platform", ts)
    # 不可登录: 冻结状态 + 无 password_hash (authenticate_local 双重拒绝)
    con.execute("UPDATE users SET status = 'suspended' WHERE id = %s",
                (created["id"],))
    _platform_id = created["id"]
    logger.info("platform account ensured: id=%s", _platform_id)
    return _platform_id


def platform_id() -> int:
    """启动后获取平台账户 id (未初始化则惰性创建)。"""
    global _platform_id
    if _platform_id is None:
        return ensure_platform_account()
    return _platform_id


def record_platform_income(con, call_id, kind: str, amount: int, note: str,
                           ts: int) -> None:
    """平台收入记账 (调用方事务内执行)。kind: platform_commission |
    platform_revenue。幂等键: {kind}:{call_id} (与 provider_earn 同族)。"""
    if amount <= 0:
        return
    pid = ensure_platform_account(con)
    # balance_after = 平台账户全池累计 (与 provider_earn 同口径)
    base = ledger_repo.total_balance(con, pid)
    idem = f"{kind}:{call_id}"
    if ledger_repo.idem_key_exists(con, pid, idem):
        return
    con.execute(
        """
        INSERT INTO ledger (user_id, delta, balance_after, kind,
                            ref_type, ref_id, idem_key, note, created_at)
        VALUES (%s, %s, %s, %s, 'tool_call', %s, %s, %s, %s)
        """,
        (pid, amount, base + amount, kind, call_id, idem, note, ts))


def platform_income_summary() -> dict:
    """平台收入汇总 (运营报表): 抽佣 / 直销 / 合计。"""
    pid = ensure_platform_account()
    with db() as con:
        rows = con.execute(
            """
            SELECT kind, COALESCE(SUM(delta), 0) AS total
            FROM ledger WHERE user_id = %s GROUP BY kind
            """,
            (pid,)).fetchall()
    out = {"commission": 0, "revenue": 0, "total": 0}
    for r in rows:
        if r["kind"] == "platform_commission":
            out["commission"] = int(r["total"])
        elif r["kind"] == "platform_revenue":
            out["revenue"] = int(r["total"])
    out["total"] = out["commission"] + out["revenue"]
    return out
