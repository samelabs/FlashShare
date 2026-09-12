"""Canonical membership tiers — 产品层唯一事实源.

free = 无 paid Membership (无 plans row 要求);
plus / pro = 仅有的两个 paid Membership tier. Route/template/admin
禁止各自写字符串判断, 一律 import 本模块。

Entitlement profile 数值归 core/governance/entitlements.py (代码治理),
本模块只承载 tier 身份与 canonical monthly credits。
"""

PAID_TIER_CODES = ("plus", "pro")

# canonical plan rows: code -> (name, monthly_credits)
TIER_PLAN_SPECS = {
    "plus": {"name": "Plus", "monthly_credits": 100},
    "pro": {"name": "Pro", "monthly_credits": 500},
}

# tier 升降序 (仅用于 UI 语义: upgrade = 升到更高档)
TIER_ORDER = {"free": 0, "plus": 1, "pro": 2}


def is_paid_tier(code: str | None) -> bool:
    return code in PAID_TIER_CODES


def sellable_plan_codes() -> tuple:
    """production sellable membership codes (数据库 plans 只能承载这些)."""
    return PAID_TIER_CODES
