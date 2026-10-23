"""我的收藏 — web endpoints for a user's pinned tools.

The rule lives in governance.favorites (single point); these routes are
the browser surface. MCP uses the same point via pin_tool/unpin_tool.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.web_auth import require_session_auth

router = APIRouter()


@router.get("/api/favorites")
def list_favorites(request: Request):
    user, err = require_session_auth(request)
    if err:
        return err
    from governance.favorites import favorite_keys
    return {"ok": True, "tools": favorite_keys(user["id"])}


@router.post("/api/favorites")
async def set_favorite_route(request: Request):
    """Body {"tool_key": "...", "on": true|false}."""
    import asyncio
    user, err = await asyncio.to_thread(require_session_auth, request)
    if err:
        return err
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if not isinstance(raw, dict) or not isinstance(raw.get("on"), bool):
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from governance.favorites import set_favorite
    code = await asyncio.to_thread(
        set_favorite, user["id"], str(raw.get("tool_key") or ""), raw["on"])
    if code:
        return JSONResponse({"ok": False, "error": code}, 404)
    return {"ok": True, "tool_key": raw.get("tool_key"), "on": raw["on"]}
