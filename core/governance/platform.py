"""platform principal — 平台账户 (cash bookkeeping subject).

平台账户是 users 表内的真实行 (username='platform', status='suspended',
无 password_hash/email → 不可登录)。它是将来真实现金流 (topup 收入、
payout 支出) 的记账主体 — Payment Closure 批次接入。

当前没有任何运行时写入方: 从 credits cost 推算平台现金收入
(commission/revenue) 的机制已删除; 平台现金收入只能来自真实支付事实。
"""

from __future__ import annotations

import logging

from database import db
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
