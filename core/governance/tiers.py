"""Canonical membership tier identity — product codes only.

free = no paid Membership;
plus / pro = the only paid Membership tier identities.

Commercial terms (price, included credits, provider product id) live in the
plans table and are operator-configured. Entitlement values live in
core/governance/entitlements.py. This module must not duplicate either.
"""

PAID_TIER_CODES = ("plus", "pro")

# Canonical identity only. No price / included-credit values are hard-coded.
TIER_PLAN_SPECS = {
    "plus": {"name": "Plus"},
    "pro": {"name": "Pro"},
}

# tier order is used only for upgrade/downgrade direction.
TIER_ORDER = {"free": 0, "plus": 1, "pro": 2}


def is_paid_tier(code: str | None) -> bool:
    return code in PAID_TIER_CODES


def sellable_plan_codes() -> tuple:
    """Canonical paid membership codes."""
    return PAID_TIER_CODES
