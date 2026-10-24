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


# Web composer picker cap: relevance-ordered head of the working set.
# Own tools (≤ MAX_TOOLS_PER_USER=20) always fit; the rest of the
# catalog stays reachable through the picker's search.
WORKING_SET_LIMIT = 24


def working_set(user: dict | None, catalog: list) -> list:
    """A user's working set — the web composer's tool picker, relevance
    order: the user's OWN tools first, then their favorites (pin order),
    then official tools; capped at WORKING_SET_LIMIT (any other public
    tool is found through the picker's search). Anonymous = official
    tools (same cap). MCP tools/list is a different surface (every
    public tool + own, favorites flagged)."""
    if not user:
        return [t for t in catalog
                if t.get("origin") == "official"][:WORKING_SET_LIMIT]
    favs = favorite_keys(user["id"], catalog)
    handle = user.get("username")
    own = [t for t in catalog
           if handle and t.get("owner_handle") == handle]
    official = [t for t in catalog if t.get("origin") == "official"]
    by_id = {t["id"]: t for t in catalog}
    fav_items = [by_id[k] for k in favs if k in by_id]
    out, seen = [], set()
    for t in own + fav_items + official:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        out.append(t)
    return out[:WORKING_SET_LIMIT]
