"""Application-level browser/session authentication helpers."""

import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from config import COOKIE_NAME, SESSION_TTL
from database import db
from repo.sessions import user_by_session_token, insert as insert_session
from security import token_hash
from utils import now, b64url


def require_session_auth(request: Request):
    """Account-control/money surfaces require a live browser session."""
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    with db() as con:
        row = user_by_session_token(con, token_hash(tok), now())
    if not row:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    return dict(row), None


def create_browser_session(user_id: int) -> str:
    """Create one fresh browser credential and persist only its HMAC hash."""
    token = b64url(secrets.token_bytes(32))
    ts = now()
    with db() as con:
        insert_session(con, token_hash(token), user_id, ts + SESSION_TTL, ts)
    return token


def redirect_with_session(url: str, user_id: int) -> RedirectResponse:
    """Create a fresh session and attach the platform cookie to a redirect."""
    token = create_browser_session(user_id)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL,
                    httponly=True, secure=True, samesite="lax")
    return resp
