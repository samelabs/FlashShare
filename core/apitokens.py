"""兼容垫片 (批2b): 实现已迁 core/identity/apitokens.py, 批3 末删除。"""
from identity.apitokens import *  # noqa: F401,F403
from identity.apitokens import (  # noqa: F401
    create_token, revoke_token, list_tokens, authenticate_token, TOKEN_PREFIX,
)
