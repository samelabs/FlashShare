"""兼容垫片 (批2a): 实现已迁 core/economy/billing.py, 批3 末删除。"""
from economy.billing import *  # noqa: F401,F403
from economy.billing import (  # noqa: F401
    grant, consume, refund, get_balance, spendable_balance, history_paged,
    InsufficientCredits, SPENDABLE_KINDS, lazy_signup_grant,
    SIGNUP_GRANT_CREDITS, DAILY_LOGIN_CREDITS,
)
