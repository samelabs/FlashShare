"""platform principal — 平台账户 (经济模型闭合).

单位 (S3): 平台收入金额为 USD-millis 现金 (调用方 gate 经
economy.money.credits_to_millis 完成唯一一次 Credits→cash 转换后传入);
cost_snapshot 在消费侧仍是 Credits。

两条平台收入路径的记账主体:
  A. 用户工具抽佣: cost 的 (1-payout) 部分 → kind='platform_commission'
  B. 系统工具直销: owner_id IS NULL 的 OK 调用全额 → kind='platform_revenue'

不变量 (守恒): 每笔计费调用 cost (Credits) = 作者 payout + 平台收入
            (两者均为 USD-millis, 现行 1000 Credits=$1.00 下数值相等) (A)
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
    row = users_repo.id_by_username(con, PLATFORM_USERNAME)
    if row:
        _platform_id = row["id"]
        return _platform_id
    ts = now()
    created = users_repo.insert_local(con, PLATFORM_USERNAME, None, None,
                                      "518.PLUS Platform", ts)
    # 不可登录: 冻结状态 + 无 password_hash (authenticate_local 双重拒绝)
    users_repo.update_status(con, created["id"], "suspended")
    _platform_id = created["id"]
    logger.info("platform account ensured: id=%s", _platform_id)
    return _platform_id


def record_platform_income(con, call_id, kind: str, amount: int, note: str,
                           ts: int) -> None:
    """平台收入记账 (调用方事务内执行)。kind: platform_commission |
    platform_revenue。幂等键: {kind}:{call_id} (与 provider_earn 同族)。

    Serialization: the platform users row is locked (FOR UPDATE) BEFORE
    the pool read, the idempotency check and the insert — concurrent
    settlements queue on it, so balance_after is a consecutive running
    total (same anchor pattern as the provider_earn writer). Lock order
    inside the settlement transaction (gate._finish): tool_calls row →
    users rows in ascending-id order (payer + owner locked up front,
    BEFORE the uploads landing whose FK takes KEY SHARE on the payer) →
    platform users row (always last). The users-row order is total, so
    no cycle can form — including two reciprocal provider calls."""
    if amount <= 0:
        return
    pid = ensure_platform_account(con)
    ledger_repo.lock_user(con, pid)
    idem = f"{kind}:{call_id}"
    if ledger_repo.idem_key_exists(con, pid, idem):
        return
    # balance_after = 平台收入池口径 (platform_commission + platform_revenue
    # — 平台账户的全部经济 kind), 与 provider_earn writer 的单池口径同规则
    base = (ledger_repo.sum_kind(con, pid, "platform_commission")
            + ledger_repo.sum_kind(con, pid, "platform_revenue"))
    ledger_repo.insert_row(con, pid, amount, base + amount, kind,
                           "tool_call", call_id, idem, note, ts)
