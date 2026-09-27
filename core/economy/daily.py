"""Daily login credits — self-contained module.

活动=功能模块: 无活动表/触发器注册。
本模块是第一个也是唯一的模块: 每日登录领积分。

运营参数在 settings 表 key='daily_login_amount' (0..1000 整数, 运营后台
audited 写入; 新库无该键 = 0 = 停用)。取值失败的方向是"停用"而非报错:
与 commission_rate 的 fail-closed 相反 — 那是结算分成, 不可证的费率
不得结算; 这是奖励, 配置坏了最多不发, 不能拦登录/领取。
历史沿袭 (与 activities 表时代逐字节对齐):
  kind 'grant_daily' · idem 'daily:{uid}:{yyyymmdd}'
  ledger note 'activity:daily_login'
账本历史语义不变, 流水可继续按 note 聚合。
"""

import time

from database import db
from repo.orders import setting

AMOUNT_KEY = "daily_login_amount"  # settings key — 每日额度 (积分); 0 = 停用
DAILY_KIND = "grant_daily"         # ledger kind (发放类, 总池语义)
DAILY_NOTE = "activity:daily_login"  # ledger 溯源键 — 历史不变


def daily_amount(con) -> int:
    """每日额度 (积分)。缺失、非整数、超出 0..1000 一律视为 0 (停用)。

    Reads within the CALLER's transaction (与 commission_rate 同一读法)。
    """
    row = setting(con, AMOUNT_KEY)
    if row is None:
        return 0
    raw = row["value"]
    if not isinstance(raw, str) or not raw.strip():
        return 0
    try:
        amount = int(raw.strip())
    except (ValueError, TypeError):
        return 0
    return amount if 0 <= amount <= 1000 else 0


def daily_idem(user_id: int) -> str:
    """每日幂等键 — 原键族逐字节保留 (ledger lineage)。"""
    day = time.strftime("%Y%m%d", time.gmtime())
    return f"daily:{user_id}:{day}"


def grant_daily(user_id: int) -> dict | None:
    """发放今日奖励。幂等重放返回原行; 停用/异常一律干净跳过
    (best-effort, 与登录路径的历史语义一致)。"""
    from economy.billing import grant
    try:
        with db() as con:
            amount = daily_amount(con)
        if amount <= 0:
            return None
        return grant(user_id, amount, DAILY_KIND,
                     idem_key=daily_idem(user_id), note=DAILY_NOTE)
    except Exception:
        return None


def granted_today(user_id: int) -> bool:
    """今日是否已入账 (credits 页 chip 状态)。"""
    from repo import ledger as ledger_repo
    with db() as con:
        return ledger_repo.idem_key_exists(con, user_id, daily_idem(user_id))
