"""兼容垫片 (批2a): 实现已迁 core/economy/activities.py, 批3 末删除。"""
from economy.activities import *  # noqa: F401,F403
from economy.activities import (  # noqa: F401
    apply_activity, get_activity, list_activities, granted_today,
    set_activity, activity_label, ensure_schema_and_seed,
)
