"""Daily login credits — self-contained module.

活动=功能模块: 无活动表/触发器注册。
本模块是第一个也是唯一的模块: 每日登录领积分。

运营参数即模块常量 — 改额度/停用 = 改这里 + 重启 (与 commission_rate
默认值同一模式)。历史沿袭 (与 activities 表时代逐字节对齐):
  amount 5 · kind 'grant_daily' · idem 'daily:{uid}:{yyyymmdd}'
  ledger note 'activity:daily_login'
账本历史语义不变, 流水可继续按 note 聚合。
"""

import time

from database import db
from utils import now

# ── 运营参数 ──
DAILY_AMOUNT = 5            # 每日额度 (积分); 0 = 停用
DAILY_KIND = "grant_daily"  # ledger kind (发放类, 总池语义)
DAILY_NOTE = "activity:daily_login"  # ledger 溯源键 — 历史不变


def daily_idem(user_id: int) -> str:
    """每日幂等键 — 原键族逐字节保留 (ledger lineage)。"""
    day = time.strftime("%Y%m%d", time.gmtime())
    return f"daily:{user_id}:{day}"


def grant_daily(user_id: int) -> dict | None:
    """发放今日奖励。幂等重放返回原行; 停用/异常一律干净跳过
    (best-effort, 与登录路径的历史语义一致)。"""
    if DAILY_AMOUNT <= 0:
        return None
    from economy.billing import grant
    try:
        return grant(user_id, DAILY_AMOUNT, DAILY_KIND,
                     idem_key=daily_idem(user_id), note=DAILY_NOTE)
    except Exception:
        return None


def granted_today(user_id: int) -> bool:
    """今日是否已入账 (credits 页 chip 状态)。"""
    from repo import ledger as ledger_repo
    with db() as con:
        return ledger_repo.idem_key_exists(con, user_id, daily_idem(user_id))
