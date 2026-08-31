"""兼容垫片 (批2d): 实现已迁 core/governance/catalog.py, 批3 末删除。"""
from governance.catalog import *  # noqa: F401,F403
from governance.catalog import (  # noqa: F401
    seed_from_registry, search_catalog, list_catalog, resolve,
    get_default_preset, catalog_version, mcp_name,
    VIS_PRIVATE, VIS_UNLISTED, VIS_PUBLIC,
)
