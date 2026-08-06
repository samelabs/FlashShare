"""Auth routes: /auth/*

X OAuth login/callback, local login/register, profile, password, logout.
GET routes render pages via Jinja2 templates, POST routes handle forms.
"""

import sys
import hashlib
import secrets
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

_BASE = Path(__file__).resolve().parent.parent.parent
_CORE = _BASE / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_BASE))

from config import (
    BASE_URL, X_CLIENT_ID, X_CLIENT_SECRET, OAUTH_TTL, SESSION_TTL,
    COOKIE_NAME,
)
from database import db
from security import token_hash
from auth import (
    authenticate_local, register_user, public_user,
    exchange_x_token, fetch_x_user, upsert_x_user,
    update_profile, change_password, unbind_x,
    validate_username, validate_email, validate_password,
    username_taken, email_taken, has_password,
)
from i18n import server_text
from utils import now, b64url

router = APIRouter()


def _lang(request: Request) -> str:
    return getattr(request.state, "lang", "en")


def _t(request: Request, key: str, **kw):
    return server_text(_lang(request)).get(key, key)


def _create_session(user_id: int) -> str:
    token = b64url(secrets.token_bytes(32))
    with db() as con:
        con.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(?,?,?,?)",
            (token_hash(token), user_id, now() + SESSION_TTL, now()),
        )
    return token


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
    with db() as con:
        con.execute(
            "INSERT INTO oauth_states(state, verifier, expires_at, created_at, bind_user_id) VALUES(?,?,?,?,?)",
            (state, verifier, now() + OAUTH_TTL, now(), bind_user_id),
        )
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
        row = con.execute("SELECT * FROM oauth_states WHERE state = ? AND expires_at > ?", (state, now())).fetchone()
        con.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    if not row:
        from app.main import templates
        from app.routes.pages import _ctx_base
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": _t(request, "err_x_state_expired")})
        resp = templates.TemplateResponse("auth/login.html", ctx); resp.status_code = 400; return resp

    try:
        token = exchange_x_token(code, row["verifier"])
        x_user = fetch_x_user(token["access_token"])
    except Exception as exc:
        from app.main import templates
        from app.routes.pages import _ctx_base
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": f'{_t(request, "err_x_login_incomplete")}: {exc}'})
        resp = templates.TemplateResponse("auth/login.html", ctx); resp.status_code = 400; return resp

    bind_user_id = row["bind_user_id"] if "bind_user_id" in row.keys() else None
    try:
        user_id = upsert_x_user(x_user, bind_user_id=bind_user_id)
    except ValueError as exc:
        from app.main import templates
        from app.routes.pages import _ctx_base
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": f'{_t(request, "err_x_bind_failed")}: {exc}'})
        resp = templates.TemplateResponse("auth/login.html", ctx); resp.status_code = 400; return resp

    if bind_user_id:
        return RedirectResponse("/auth/profile", 302)

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
    from app.routes.pages import _ctx_base
    lang = _lang(request)
    texts = server_text(lang)
    form = await request.form()
    login = (form.get("login") or "").strip()
    password = form.get("password") or ""
    if not login or not password:
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": texts.get("err_fill_login", "Please fill in all fields")})
        return templates.TemplateResponse("auth/login.html", ctx)

    user_id = authenticate_local(login, password)
    if not user_id:
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": texts.get("err_login_failed", "Invalid credentials")})
        return templates.TemplateResponse("auth/login.html", ctx)

    session_token = _create_session(user_id)
    return _redirect_with_cookie("/", session_token)


@router.post("/register")
async def register_submit(request: Request):
    from app.main import templates
    from app.routes.pages import _ctx_base
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
        return templates.TemplateResponse("auth/register.html", ctx)

    err = validate_username(username)
    if err: return _err(texts.get(err, err))
    err = validate_email(email)
    if err: return _err(texts.get(err, err))
    err = validate_password(password)
    if err: return _err(texts.get(err, err))
    if password != password_confirm:
        return _err(texts.get("passwordMismatch", "Passwords do not match"))

    try:
        user_id = register_user(username, email, password)
    except Exception as exc:
        return _err(str(exc))

    session_token = _create_session(user_id)
    return _redirect_with_cookie("/", session_token)


@router.post("/logout")
async def logout_post(request: Request):
    resp = RedirectResponse("/", 302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ── Profile ──

@router.post("/profile")
async def update_profile_route(request: Request):
    from app.main import templates
    from app.routes.pages import _ctx_base
    user = request.state.user
    lang = _lang(request)
    if not user:
        return RedirectResponse("/auth/login", 302)

    form = await request.form()
    display_name = (form.get("display_name") or "").strip()
    email = (form.get("email") or "").strip()
    bio = (form.get("bio") or "").strip()

    try:
        update_profile(user["id"], display_name=display_name or None, email=email or None, bio=bio or None)
    except Exception as exc:
        u = public_user(user)
        texts = server_text(lang)
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": str(exc), "u": u, "user_has_password": u.get("hasPassword", False), "x_bound": bool(u.get("xUsername"))})
        return templates.TemplateResponse("auth/profile.html", ctx)

    return RedirectResponse("/auth/profile", 302)


@router.post("/password")
async def change_password_route(request: Request):
    from app.main import templates
    from app.routes.pages import _ctx_base
    user = request.state.user
    lang = _lang(request)
    if not user:
        return RedirectResponse("/auth/login", 302)

    form = await request.form()
    new_password = form.get("new_password") or ""
    new_password_confirm = form.get("new_password_confirm") or ""
    old_password = form.get("old_password") or ""

    def _err(msg):
        u = public_user(user)
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": msg, "u": u, "user_has_password": u.get("hasPassword", False), "x_bound": bool(u.get("xUsername"))})
        return templates.TemplateResponse("auth/profile.html", ctx)

    if new_password != new_password_confirm:
        return _err(server_text(lang).get("passwordMismatch", ""))

    if has_password(user["id"]):
        if not old_password:
            return _err(server_text(lang).get("err_fill_login", ""))
        try:
            change_password(user["id"], old_password, new_password)
        except Exception as exc:
            return _err(str(exc))
    else:
        from auth import set_password
        set_password(user["id"], new_password)

    return RedirectResponse("/auth/profile", 302)
