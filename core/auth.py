"""兼容垫片 (批2b): 实现已迁 core/identity/auth.py, 批3 末删除。"""
from identity.auth import *  # noqa: F401,F403
from identity.auth import (  # noqa: F401  (显式再导出, 防 * 漏名单)
    validate_username, validate_email, validate_password,
    username_taken, register_user, authenticate_local,
    exchange_x_token, fetch_x_user, upsert_x_user, unbind_x,
    update_profile, change_password, set_password, public_user, has_password,
)
