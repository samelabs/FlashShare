"""Auth routes: X OAuth, local login/register and logout."""

import sys
import asyncio
import hashlib
import logging
import secrets
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse

import psycopg2

_BASE = Path(__file__).resolve().parent.parent.parent
_CORE = _BASE / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_BASE))

from config import BASE_URL, X_CLIENT_ID, X_CLIENT_SECRET, OAUTH_TTL, COOKIE_NAME
from database import db
from repo.sessions import pop_oauth_state, insert_oauth_state, delete_session
from security import token_hash
from identity.auth import (
    authenticate_local, register_user,
    exchange_x_token, fetch_x_user, upsert_x_user,
    unbind_x,
    validate_username, validate_email, validate_password,
    username_taken, normalize_email,
)
from i18n import server_text
from utils import now, b64url
from app.web_auth import require_session_auth, redirect_with_session, client_ip
from app.web_ctx import error_text

router = APIRouter()
logger = logging.getLogger("flashshare")

# In-memory sliding-window throttles (single uvicorn worker).
# Limits are configurable so isolated test stacks can raise them (the CI
# suites all register from 127.0.0.1 and exceed the production defaults);
# there is NO per-ip exemption — production traffic arrives via Nginx
# from 127.0.0.1 too, so 127.0.0.1 must not bypass these gates.
import os as _os


def _env_int(name: str, default: int) -> int:
    raw = _os.environ.get(name, "").strip()
    try:
        val = int(raw) if raw else default
    except ValueError:
        val = default
    return val if val > 0 else default


_LOGIN_FAIL_LIMIT = 10              # failures per (ip, login)
_LOGIN_FAIL_WINDOW = 15 * 60
_login_failures: dict[tuple, list[int]] = {}
_LOGIN_IP_FAIL_LIMIT = _env_int("FLASHSHARE_LOGIN_LIMIT_PER_15MIN", 50)
_LOGIN_IP_FAIL_WINDOW = 15 * 60
_login_ip_failures: dict[tuple, list[int]] = {}
_REGISTER_IP_LIMIT = _env_int("FLASHSHARE_REGISTER_LIMIT_PER_HOUR", 30)
_REGISTER_IP_WINDOW = 60 * 60
_register_attempts: dict[tuple, list[int]] = {}
_MCP_REGISTER_IP_LIMIT = _env_int("FLASHSHARE_MCP_REGISTER_LIMIT_PER_HOUR", 10)
_MCP_REGISTER_IP_WINDOW = 60 * 60
_mcp_register_attempts: dict[tuple, list[int]] = {}

# X OAuth browser binding: the initiating browser holds the state in an
# HttpOnly cookie; the callback must present the same value.
_OAUTH_COOKIE = "flashshare_x_oauth"


def _throttled(store: dict, key: tuple, limit: int, window: int) -> bool:
    ts = now()
    hits = [t for t in store.get(key, []) if ts - t < window]
    store[key] = hits
    return len(hits) >= limit


def _record_hit(store: dict, key: tuple, window: int) -> None:
    store.setdefault(key, []).append(now())
    if len(store) > 10000:
        cutoff = now() - window
        for k in list(store):
            store[k] = [t for t in store[k] if t >= cutoff]
            if not store[k]:
                del store[k]


def _login_throttled(key: tuple) -> bool:
    return _throttled(_login_failures, key, _LOGIN_FAIL_LIMIT, _LOGIN_FAIL_WINDOW)


def _record_login_failure(key: tuple) -> None:
    _record_hit(_login_failures, key, _LOGIN_FAIL_WINDOW)


def _lang(request: Request) -> str:
    return getattr(request.state, "lang", "en")


def _grant_daily(uid: int) -> None:
    try:
        from economy.daily import grant_daily
        grant_daily(uid)
    except Exception:
        logger.exception("daily grant failed uid=%s", uid)


def _login_maintenance(uid: int) -> None:
    """Best-effort login maintenance after identity has been accepted."""
    _grant_daily(uid)
    try:
        from economy.reconcile import reconcile_user
        res = reconcile_user(uid)
        if res.get("granted"):
            logger.info("login reconcile granted: uid=%s +%d",
                        uid, res["credits"])
    except Exception:
        logger.exception("login reconcile failed uid=%s", uid)


# ── X OAuth ──

@router.get("/x/login")
def x_login(request: Request):
    if not X_CLIENT_ID or not X_CLIENT_SECRET:
        # X login unavailable on this deployment: the entry points are
        # hidden, and a direct hit goes back to the regular login page.
        return RedirectResponse("/auth/login", 302)

    wants_bind = request.query_params.get("bind") == "1"
    bind_user_id = None
    if wants_bind:
        current, err = require_session_auth(request)
        if err:
            return RedirectResponse("/auth/login", 302)
        bind_user_id = current["id"]

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
    resp = RedirectResponse("https://x.com/i/oauth2/authorize?" + urllib.parse.urlencode(params))
    resp.set_cookie(_OAUTH_COOKIE, state, max_age=OAUTH_TTL, path="/auth/x",
                    httponly=True, secure=True, samesite="lax")
    # short-lived return target (login flows hand it over; bind does not)
    if not wants_bind:
        resp.set_cookie(_X_NEXT_COOKIE, _safe_next(request.query_params.get("next")),
                        max_age=_X_NEXT_TTL, path="/auth/x",
                        httponly=True, secure=True, samesite="lax")
    return resp


def _x_error(request: Request, code: str):
    from app.main import templates
    from app.web_ctx import ctx_base as _ctx_base
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "error": error_text(request, code)})
    resp = templates.TemplateResponse(request, "auth/login.html", ctx)
    resp.status_code = 400
    resp.delete_cookie(_OAUTH_COOKIE, path="/auth/x")
    return resp


def verify_oauth_callback(state: str, browser_state: str, row,
                          session_user_id: int | None) -> str | None:
    """Callback acceptance for a popped (single-use) state record.

    The state must have been issued to this browser (cookie equals state)
    and, for a bind flow, the browser's live session must belong to the
    bind target. Returns an error code or None."""
    if not row:
        return "err_x_state_expired"
    if not browser_state or not secrets.compare_digest(browser_state, state):
        return "err_x_state_expired"
    bind_user_id = row.get("bind_user_id")
    if bind_user_id and session_user_id != bind_user_id:
        return "err_x_bind_failed"
    return None


def _x_callback_stage1(state: str, oauth_cookie: str, session_uid):
    """Sync stage (thread): consume the one-shot state + verify."""
    with db() as con:
        row = pop_oauth_state(con, state, now())
    err = verify_oauth_callback(state, oauth_cookie, row, session_uid)
    return row, err


def _x_callback_stage2(x_user: dict, bind_user_id):
    """Sync stage (thread): converge the identity + login maintenance."""
    try:
        user_id = upsert_x_user(x_user, bind_user_id=bind_user_id)
    except ValueError as exc:
        logger.warning("x callback: upsert rejected: %s", exc)
        code_ = str(exc)
        if code_ not in ("err_user_suspended", "err_x_bound_other",
                         "err_user_not_found"):
            code_ = "err_x_bind_failed"
        return None, code_
    if not bind_user_id:
        _login_maintenance(user_id)
    return user_id, None


@router.get("/x/callback")
async def x_callback(request: Request):
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not state:
        return RedirectResponse("/", 302)

    session_user, _ = await asyncio.to_thread(
        require_session_auth, request)
    # Single use: the state record is consumed by any callback attempt.
    row, err = await asyncio.to_thread(
        _x_callback_stage1, state,
        request.cookies.get(_OAUTH_COOKIE, ""),
        session_user["id"] if session_user else None)
    if err:
        logger.warning("x callback rejected: %s (state_found=%s)", err, bool(row))
        return _x_error(request, err)

    try:
        token = await asyncio.to_thread(exchange_x_token, code,
                                        row["verifier"])
        x_user = await asyncio.to_thread(fetch_x_user,
                                         token["access_token"])
    except Exception:
        logger.exception("x callback: token exchange / user lookup failed")
        return _x_error(request, "err_x_login_incomplete")

    bind_user_id = row.get("bind_user_id")
    user_id, upsert_err = await asyncio.to_thread(
        _x_callback_stage2, x_user, bind_user_id)
    if upsert_err:
        return _x_error(request, upsert_err)

    if bind_user_id:
        resp = RedirectResponse("/account/profile", 302)
    else:
        resp = redirect_with_session(
            _safe_next(request.cookies.get(_X_NEXT_COOKIE)), user_id)
    resp.delete_cookie(_OAUTH_COOKIE, path="/auth/x")
    resp.delete_cookie(_X_NEXT_COOKIE, path="/auth/x")
    return resp


@router.post("/x/unbind")
async def x_unbind(request: Request):
    user, err = require_session_auth(request)
    if err:
        return err
    try:
        unbind_x(user["id"])
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    return {"ok": True}


# ── Local auth ──

_X_NEXT_COOKIE = "fs_x_next"
_X_NEXT_TTL = 600  # seconds — the X-login return target window


def _safe_next(value) -> str:
    """Login-return target: site-relative only ("/…", never "//…")."""
    v = (value or "").strip()
    # browsers treat "\\" like "/" in URLs ("/\\evil.com" == "//evil.com");
    # control characters never belong in a return path
    if (v.startswith("/") and not v.startswith("//")
            and "\\" not in v and not any(ord(c) < 0x20 for c in v)):
        return v
    return "/"


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _auth_json_ok(user_id: int) -> JSONResponse:
    """Success JSON for the login modal: the SAME browser session
    cookie the form redirect mints (one create_browser_session path),
    plus the header-swap facts."""
    from repo.users import by_id as user_by_id
    from identity.auth import public_user
    from app.web_auth import create_browser_session
    from config import COOKIE_NAME, SESSION_TTL
    with db() as con:
        row = user_by_id(con, user_id)
    pub = public_user(row) if row else {}
    token = create_browser_session(user_id)
    resp = JSONResponse({"ok": True, "user": {
        "username": pub.get("username"),
        "display_name": pub.get("displayName") or pub.get("username"),
    }})
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL,
                    httponly=True, secure=True, samesite="lax")
    return resp


def _auth_json_err(code: str, status: int = 200) -> JSONResponse:
    """Failure JSON for the modal: the RAW domain error code (the client
    translates via the same i18n boundary); status mirrors the form flow
    (throttling stays 429)."""
    return JSONResponse({"ok": False, "error": code}, status_code=status)


@router.post("/login")
async def login_submit(request: Request):
    from app.main import templates
    from app.web_ctx import ctx_base as _ctx_base
    texts = server_text(_lang(request))
    wants_json = _wants_json(request)
    form = await request.form()
    login = (form.get("login") or "").strip()
    password = form.get("password") or ""
    next_url = _safe_next(form.get("next"))
    if not login or not password:
        if wants_json:
            return _auth_json_err("err_fill_login")
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "",
                    "error": texts.get("err_fill_login", "Please fill in all fields"),
                    "next": next_url})
        return templates.TemplateResponse(request, "auth/login.html", ctx)

    ip = client_ip(request)
    throttle_key = (ip, login.lower())
    if _throttled(_login_ip_failures, (ip,), _LOGIN_IP_FAIL_LIMIT, _LOGIN_FAIL_WINDOW):
        if wants_json:
            return _auth_json_err("err_too_many_attempts", 429)
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "",
                    "error": error_text(request, "err_too_many_attempts"),
                    "next": next_url})
        resp = templates.TemplateResponse(request, "auth/login.html", ctx)
        resp.status_code = 429
        return resp
    if _login_throttled(throttle_key):
        if wants_json:
            # JSON callers get the throttle status explicitly (429); the
            # code matches the message the form flow shows here
            return _auth_json_err("err_login_failed", 429)
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "",
                    "error": texts.get("err_login_failed", "Invalid credentials"),
                    "next": next_url})
        return templates.TemplateResponse(request, "auth/login.html", ctx)

    user_id = await asyncio.to_thread(authenticate_local, login, password)
    if not user_id:
        _record_login_failure(throttle_key)
        _record_hit(_login_ip_failures, (ip,), _LOGIN_FAIL_WINDOW)
        if wants_json:
            return _auth_json_err("err_login_failed")
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "",
                    "error": texts.get("err_login_failed", "Invalid credentials"),
                    "next": next_url})
        return templates.TemplateResponse(request, "auth/login.html", ctx)

    _login_failures.pop(throttle_key, None)
    await asyncio.to_thread(_login_maintenance, user_id)
    if wants_json:
        return _auth_json_ok(user_id)
    return redirect_with_session(next_url, user_id)


@router.post("/register")
async def register_submit(request: Request):
    from app.main import templates
    from app.web_ctx import ctx_base as _ctx_base
    texts = server_text(_lang(request))
    wants_json = _wants_json(request)
    form = await request.form()
    username = (form.get("username") or "").strip()
    email = normalize_email(form.get("email"))
    password = form.get("password") or ""
    password_confirm = form.get("password_confirm") or ""
    next_url = _safe_next(form.get("next"))

    def _err(msg, status=200, code=None):
        if wants_json:
            # JSON callers get the RAW code (client-side i18n boundary);
            # msg here is already the translated text for the form path.
            return _auth_json_err(code or "err_invalid_request", status)
        ctx = _ctx_base(request)
        ctx.update({"shell_class": "", "share_url": "", "error": msg,
                    "next": next_url})
        resp = templates.TemplateResponse(request, "auth/register.html", ctx)
        resp.status_code = status
        return resp

    ip_key = (client_ip(request),)
    if _throttled(_register_attempts, ip_key, _REGISTER_IP_LIMIT, _REGISTER_IP_WINDOW):
        return _err(error_text(request, "err_too_many_attempts"), 429,
                    "err_too_many_attempts")
    _record_hit(_register_attempts, ip_key, _REGISTER_IP_WINDOW)

    err = validate_username(username)
    if err:
        return _err(texts.get(err, err), code=err)
    err = validate_email(email)
    if err:
        return _err(texts.get(err, err), code=err)
    err = validate_password(password)
    if err:
        return _err(texts.get(err, err), code=err)
    if password != password_confirm:
        return _err(texts.get("passwordMismatch", "Passwords do not match"),
                    code="passwordMismatch")

    try:
        user_id = await asyncio.to_thread(register_user, username, email, password)
    except ValueError as exc:
        return _err(error_text(request, str(exc)), code=str(exc))
    except psycopg2.IntegrityError:
        if username_taken(username):
            return _err(texts.get("err_username_taken", "err_username_taken"),
                        code="err_username_taken")
        return _err(texts.get("err_email_taken", "err_email_taken"),
                    code="err_email_taken")
    except Exception:
        logger.exception("register failed for username=%s", username)
        return _err(texts.get("err_invalid_request", "err_invalid_request"),
                    code="err_invalid_request")

    await asyncio.to_thread(_login_maintenance, user_id)
    if wants_json:
        return _auth_json_ok(user_id)
    return redirect_with_session(next_url, user_id)


async def headless_register(request: Request, raw) -> tuple[int, dict]:
    """The single headless registration: (HTTP status, JSON body).
    Shared by POST /auth/mcp/register and the MCP `register` tool (same
    validators, same per-IP limit, same apikeys single point)."""
    if not isinstance(raw, dict):
        return 400, {"ok": False, "error": "err_invalid_request"}
    username = str(raw.get("username") or "").strip()

    ip_key = (client_ip(request),)
    if _throttled(_mcp_register_attempts, ip_key,
                  _MCP_REGISTER_IP_LIMIT, _MCP_REGISTER_IP_WINDOW):
        return 429, {"ok": False, "error": "err_too_many_attempts"}
    _record_hit(_mcp_register_attempts, ip_key, _MCP_REGISTER_IP_WINDOW)

    if username:
        err = validate_username(username)
        if err:
            return 400, {"ok": False, "error": err}
    else:
        username = "agent_" + secrets.token_hex(4)
    email = f"mcp-{secrets.token_hex(8)}@mcp.518.plus"
    password = secrets.token_urlsafe(18)

    try:
        user_id = await asyncio.to_thread(
            register_user, username, email, password)
    except ValueError as exc:
        token = str(exc)
        status = 409 if token in ("err_username_taken", "err_email_taken") \
            else 400
        return status, {"ok": False, "error": token}
    except psycopg2.IntegrityError:
        if username_taken(username):
            return 409, {"ok": False, "error": "err_username_taken"}
        return 409, {"ok": False, "error": "err_email_taken"}
    except Exception:
        logger.exception("mcp register failed for username=%s", username)
        return 500, {"ok": False, "error": "err_invalid_request"}

    from identity.apikeys import create_key
    try:
        key = await asyncio.to_thread(create_key, user_id, "mcp")
    except ValueError as exc:
        return 503, {"ok": False, "error": str(exc)}
    key.pop("key_sealed", None)  # server-side sealed copy never leaves
    return 200, {"ok": True, "username": username,
                 "password": password, "key": key}


# Headless MCP registration over plain HTTP: the machine surface's only
# unauthenticated write besides the MCP `register` tool (same function).
# An agent registers itself and receives its Bearer key in the same
# response; sets no session (no human, no cookie). No login maintenance
# — daily-login benefits start only when a human logs into this account
# on the web, so headless account farming yields no credits. If key
# creation fails after register committed (vault unavailable), the
# account stays as a zero-balance row; retrying with a fresh username
# is the recovery.
@router.post("/mcp/register")
async def mcp_register(request: Request):
    try:
        raw = await request.json()
    except Exception:
        raw = None
    status, body = await headless_register(request, raw)
    return JSONResponse(body, status)


@router.post("/logout")
def logout_post(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            with db() as con:
                delete_session(con, token_hash(token))
        except Exception:
            logger.exception("logout session revoke failed")
    resp = RedirectResponse("/", 302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# Profile/password POST handlers live in app/routes/pages.py (/account/*).
