"""governance/favorites — 我的收藏 (a user's pinned tools), single point.

One rule decides a user's working set — the web composer's tool
picker: first-party tools, the user's own tools, and the user's
favorites. MCP tools/list is NOT this working set: it carries every
public tool plus the user's own, with favorites merely flagged. Every
other visible tool stays reachable through search (catalog_search /
the picker's query) and by name — the list is a working set, not an
access boundary.
"""

from database import db
from repo import favorites as fav_repo
from utils import now


def favorite_keys(user_id: int, catalog: list | None = None) -> list:
    """The user's favorites that are still visible to them, in pin order."""
    from governance.catalog import list_catalog
    visible = {t["id"] for t in (catalog if catalog is not None
                                 else list_catalog(user_id))}
    with db() as con:
        keys = fav_repo.list_keys(con, user_id)
    return [k for k in keys if k in visible]


def set_favorite(user_id: int, tool_key: str, on: bool) -> str | None:
    """Pin or unpin one tool. Returns an error code, or None on success.
    Pinning requires the tool to be visible to the user; unpinning is
    always allowed (it also clears rows for tools no longer visible)."""
    tool_key = (tool_key or "").strip()
    if not tool_key or len(tool_key) > 120:
        return "tool_not_found"
    if on:
        from governance.catalog import list_catalog
        if tool_key not in {t["id"] for t in list_catalog(user_id)}:
            return "tool_not_found"
        with db() as con:
            fav_repo.add(con, user_id, tool_key, now())
    else:
        with db() as con:
            fav_repo.remove(con, user_id, tool_key)
    return None


def working_set(user: dict | None, catalog: list) -> list:
    """A user's working set — the web composer's tool picker: official
    tools, the user's own tools, then their favorites (pin order).
    Anonymous = official tools. MCP tools/list is a different surface
    (every public tool + own, favorites flagged). Any other public tool
    is found through search."""
    if not user:
        return [t for t in catalog if t.get("origin") == "official"]
    favs = favorite_keys(user["id"], catalog)
    handle = user.get("username")
    base = [t for t in catalog
            if t.get("origin") == "official"
            or (handle and t.get("owner_handle") == handle)]
    base_ids = {t["id"] for t in base}
    by_id = {t["id"]: t for t in catalog}
    return base + [by_id[k] for k in favs
                   if k not in base_ids and k in by_id]
