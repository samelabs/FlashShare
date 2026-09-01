"""app/web_auth — 应用层公共 session 认证 (批3a, 消灭 routes 互 import)."""

from fastapi import Request
from fastapi.responses import JSONResponse

from config import COOKIE_NAME
from database import db
from repo.sessions import user_by_session_token
from security import token_hash
from utils import now


def require_session_auth(request: Request):
    """Session-only (money surface is human-facing)."""
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    with db() as con:
        row = user_by_session_token(con, token_hash(tok), now())
    if not row:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    return dict(row), None


def require_session_or_bearer_auth(request: Request):
    """api 面认证: 中间件已解析 request.state.user (session/bearer 双通道),
    此处只做存在性闸 (与原各路由 _require_auth 行为一致)。"""
    user = getattr(request.state, "user", None)
    if not user:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    return user, None
