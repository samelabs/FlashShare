"""兼容垫片 (批2c): 实现已迁 core/sharing/uploads.py, 批3 末删除。"""
from sharing.uploads import *  # noqa: F401,F403
from sharing.uploads import (  # noqa: F401
    check_quota, get_upload_by_public_id, list_uploads, delete_upload,
)
