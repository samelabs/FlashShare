"""兼容垫片 (批2a): 实现已迁 core/economy/creem.py, 批3 末删除。"""
from economy.creem import *  # noqa: F401,F403
from economy.creem import (  # noqa: F401
    creem_enabled, create_checkout, verify_signature, fulfill,
)
