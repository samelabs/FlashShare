"""兼容垫片 (批2d): 实现已迁 core/governance/pricing.py, 批3 末删除。"""
from governance.pricing import *  # noqa: F401,F403
from governance.pricing import (  # noqa: F401
    seed_pricing, get_pricing, get_cost, is_enabled, set_pricing, list_pricing,
)
