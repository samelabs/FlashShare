"""Auth routes: /auth/*

X OAuth login/callback, local login/register, profile, password, logout.
GET routes render pages via Jinja2 templates, POST routes handle forms.
"""

import sys
import asyncio
import hashlib
import logging
import secrets
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse

import psycopg2

_BASE = Path(__file__).resolve().parent.parent.parent
_CORE = _BASE / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_BASE))

from config import (
    BASE_URL, X_CLIENT_ID, X_CLIENT_SECRET, OAUTH_TTL, SESSION_TTL,
    COOKIE_NAME,
)
from database import db
from repo.sessions import insert as insert_session, pop_oauth_state, insert_oauth_state
from security import token_hash
from identity.auth import (
    authenticate_local, register_user, public_user,
    exchange_x_token, fetch_x_user, upsert_x_user,
    update_profile, change_password, unbind_x,
    validate_username, validate_email, validate_password,
    has_password, username_taken,
)
from i18n import server_text
from utils import now, b64url

router = APIRouter()
logger = logging.getLogger("flashshare")

# ── Login failure throttle (in-process) ──
# 10 failures per (ip, login) in 15 minutes → further attempts rejected
# with the generic wrong-credentials message. In-process is sufficient:
# single uvicorn worker; restart resets the window, which is acceptable.
_LOGIN_FAIL_LIMIT = 10
_LOGIN_FAIL_WINDOW = 15 * 60
_login_failures: dict[tuple, list[int]] = {}


def _login_throttled(key: tuple) -> bool:
    ts = now()
    hits = [t for t in _login_failures.get(key, []) if ts - t < _LOGIN_FAIL_WINDOW]
    _login_failures[key] = hits
    return len(hits) >= _LOGIN_FAIL_LIMIT


def _record_login_failure(key: tuple) -> None:
    _login_failures.setdefault(key, []).append(now())
    if len(_login_failures) > 10000:  # opportunistic cleanup
        cutoff = now() - _LOGIN_FAIL_WINDOW
        for k in list(_login_failures):
            _login_failures[k] = [t for t in _login_failures[k] if t >= cutoff]
            if not _login_failures[k]:
                del _login_failures[k]


def _lang(request: Request) -> str:
    return getattr(request.state, "lang", "en")


def _t(request: Request, key: str, **kw):
    return server_text(_lang(request)).get(key, key)


def _create_session(user_id: int) -> str:
    token = b64url(secrets.token_bytes(32))
    ts = now()
    with db() as con:
        insert_session(con, token_hash(token), user_id, ts + SESSION_TTL, ts)
    return token


def _grant_daily(uid: int) -> None:
    """Daily login bonus at session creation (idempotent, own tx).

    economy.daily module (1002 收口, activities 表已拆). best-effort —
    停用 (额度 0) 是干净跳过, 不是错误路径。
    """
    try:
        from economy.daily import grant_daily
        grant_daily(uid)
    except Exception:
        logger.exception("daily grant failed uid=%s", uid)


def _redirect_with_cookie(url: str, token: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax")
    return resp


# ── X OAuth ──

@router.get("/x/login")
async def x_login(request: Request):
    if not X_CLIENT_ID or not X_CLIENT_SECRET:
        return HTMLResponse("X login not configured", 500)

    current = request.state.user
    is_bind = current is not None and request.query_params.get("bind") == "1"
    bind_user_id = current["id"] if is_bind else None

    verifier = b64url(secrets.token_bytes(48))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(32))
    ts = now()
    with db() as con:
        insert_oauth_state(con, state, verifier, ts + OAUTH_TTL, ts, bind_user_id)
    params = {
        "response_type": "code",
        "client_id": X_CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/auth/x/callback",
        "scope": "tweet.read users.read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse("https://x.com/i/oauth2/authorize?" + urllib.parse.urlencode(params))


@router.get("/x/callback")
async def x_callback(request: Request):
    lang = _lang(request)
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    # User cancelled OAuth — redirect home, don't block
    if not code or not state:
        return RedirectResponse("/", 302)

    with db() as con:
        row = pop_oauth_state(con, state, now())
    if not row:
        from app.main import templates
        from app.web_ctx import ctx_base as _ctx_base
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": _t(request, "err_x_state_expired")})
        resp = templates.TemplateResponse(request, "auth/login.html", ctx); resp.status_code = 400; return resp

    try:
        token = exchange_x_token(code, row["verifier"])
        x_user = fetch_x_user(token["access_token"])
    except Exception as exc:
        from app.main import templates
        from app.web_ctx import ctx_base as _ctx_base
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": f'{_t(request, "err_x_login_incomplete")}: {exc}'})
        resp = templates.TemplateResponse(request, "auth/login.html", ctx); resp.status_code = 400; return resp

    bind_user_id = row["bind_user_id"] if "bind_user_id" in row.keys() else None
    try:
        user_id = upsert_x_user(x_user, bind_user_id=bind_user_id)
    except ValueError as exc:
        from app.main import templates
        from app.web_ctx import ctx_base as _ctx_base
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": f'{_t(request, "err_x_bind_failed")}: {exc}'})
        resp = templates.TemplateResponse(request, "auth/login.html", ctx); resp.status_code = 400; return resp

    if bind_user_id:
        return RedirectResponse("/account/profile", 302)

    _grant_daily(user_id)
    session_token = _create_session(user_id)
    return _redirect_with_cookie("/", session_token)


@router.post("/x/unbind")
async def x_unbind(request: Request):
    user = request.state.user
    if not user:
        return HTMLResponse('{"ok":false,"error":"auth_required"}', 401, media_type="application/json")
    unbind_x(user["id"])
    return HTMLResponse('{"ok":true}', media_type="application/json")


# ── Local auth ──

@router.post("/login")
async def login_submit(request: Request):
    from app.main import templates
    from app.web_ctx import ctx_base as _ctx_base
    lang = _lang(request)
    texts = server_text(lang)
    form = await request.form()
    login = (form.get("login") or "").strip()
    password = form.get("password") or ""
    if not login or not password:
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": texts.get("err_fill_login", "Please fill in all fields")})
        return templates.TemplateResponse(request, "auth/login.html", ctx)

    ip = request.headers.get("X-Real-IP", request.client.host if request.client else "")
    throttle_key = (ip, login.lower())
    if _login_throttled(throttle_key):
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": texts.get("err_login_failed", "Invalid credentials")})
        return templates.TemplateResponse(request, "auth/login.html", ctx)

    # bcrypt verification is CPU-heavy (~250ms) — keep it off the event loop
    user_id = await asyncio.to_thread(authenticate_local, login, password)
    if not user_id:
        _record_login_failure(throttle_key)
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": texts.get("err_login_failed", "Invalid credentials")})
        return templates.TemplateResponse(request, "auth/login.html", ctx)

    _login_failures.pop(throttle_key, None)
    _grant_daily(user_id)
    session_token = _create_session(user_id)
    return _redirect_with_cookie("/", session_token)


@router.post("/register")
async def register_submit(request: Request):
    from app.main import templates
    from app.web_ctx import ctx_base as _ctx_base
    lang = _lang(request)
    texts = server_text(lang)
    form = await request.form()
    username = (form.get("username") or "").strip()
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""
    password_confirm = form.get("password_confirm") or ""

    def _err(msg):
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": msg})
        return templates.TemplateResponse(request, "auth/register.html", ctx)

    err = validate_username(username)
    if err: return _err(texts.get(err, err))
    err = validate_email(email)
    if err: return _err(texts.get(err, err))
    err = validate_password(password)
    if err: return _err(texts.get(err, err))
    if password != password_confirm:
        return _err(texts.get("passwordMismatch", "Passwords do not match"))

    try:
        # bcrypt hashing is CPU-heavy — keep it off the event loop
        user_id = await asyncio.to_thread(register_user, username, email, password)
    except psycopg2.IntegrityError:
        # Lost the race between validation and INSERT — map back to i18n keys
        if username_taken(username):
            return _err(texts.get("err_username_taken", "err_username_taken"))
        return _err(texts.get("err_email_taken", "err_email_taken"))
    except Exception:
        logger.exception("register failed for username=%s", username)
        return _err(texts.get("err_invalid_request", "err_invalid_request"))

    _grant_daily(user_id)
    session_token = _create_session(user_id)
    return _redirect_with_cookie("/", session_token)


@router.post("/logout")
async def logout_post(request: Request):
    resp = RedirectResponse("/", 302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ── Profile ──

# Legacy /auth/profile is intentionally gone (single-page account removed).
# Profile/password POST handlers live in app/routes/pages.py (/account/* forms).
