"""兼容垫片 (批2a): 实现已迁 core/economy/earnings.py, 批3 末删除。"""
from economy.earnings import *  # noqa: F401,F403
from economy.earnings import (  # noqa: F401
    earnings_summary, per_tool_revenue, request_withdrawal, process_withdrawal,
    revenue_lines, list_withdrawals, withdrawal_count, list_all_pending,
    to_usd, ensure_schema, MIN_WITHDRAWAL, CREDITS_PER_USD, WITHDRAWAL_STATES,
)
