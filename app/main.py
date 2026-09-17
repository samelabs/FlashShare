"""FlashShare FastAPI application entry point.

Architecture:
    Cloudflare → Nginx → Uvicorn (this app)
                              ├── Jinja2: page rendering
                              ├── API routes: /api/*
                              ├── Auth routes: /auth/*
                              ├── Proxy routes: file download / QR
                              └── Worker pool: tool processing (subprocess isolation)
"""

import sys
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
_CORE_DIR = _BASE_DIR / "core"
sys.path.insert(0, str(_CORE_DIR))

import json
import logging
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, RedirectResponse
from markupsafe import escape as html_escape

from config import APP_NAME, COOKIE_NAME, SESSION_TTL
from database import init_db
from security import token_hash
from i18n import language_from_code, language_from_header
from utils import now
import tools_registry  # noqa: F401 — registers share + privacy tools
from tools import load_all

load_all()

# ── Logging ──
import os as _os
_LOG_DIR = _os.environ.get("FLASHSHARE_LOG_DIR", "/var/log/flashshare")
_os.makedirs(_LOG_DIR, exist_ok=True)
_file_handler = RotatingFileHandler(f"{_LOG_DIR}/app.log", maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger("flashshare")

# ── App ──
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)

_STATIC_DIR = _BASE_DIR / "static"
_TEMPLATES_DIR = _BASE_DIR / "app" / "templates"
_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Raw platform-owned SVG/HTML still relies on autoescape=False. User-controlled
# text/data has explicit canonical boundaries: |lt, |e and |tojson.
templates.env.autoescape = False

from governance.tool_params import resolve_label as _resolve_label

def _lt(value, lang: str):
    return html_escape(_resolve_label(value, lang))

templates.env.filters["lt"] = _lt
templates.env.globals["lt"] = _lt
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── Auth helpers ──

def get_user_from_request(request: Request):
    """Session first, then Bearer capability identity."""
    from database import db
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            from identity.apitokens import authenticate_token
            user = authenticate_token(auth[7:].strip())
            if user:
                request.state.auth_via = "token"
            return user
        return None
    try:
        from repo.sessions import user_by_session_token
        with db() as con:
            row = user_by_session_token(con, token_hash(token), now())
        if row:
            request.state.auth_via = "session"
        return dict(row) if row else None
    except Exception:
        return None


def detect_lang(request: Request) -> str:
    for param in ("lang", "x"):
        val = request.query_params.get(param, "")
        code = language_from_code(val)
        if code:
            return code
    cookie_lang = request.cookies.get("flashshare_lang", "")
    code = language_from_code(cookie_lang)
    if code:
        return code
    return language_from_header(request.headers.get("accept-language", ""))


# ── Middleware ──

import asyncio

_concurrency = asyncio.Semaphore(8)
_HEAVY_PATHS = ("/api/uploads", "/api/tools/", "/api/v1/uploads", "/mcp",
                "/auth/login", "/auth/register")
# Mutating account identity/credentials is a browser-session surface. Capability
# tokens may run tools/API, but cannot use themselves to mint/alter account
# credentials. Token CRUD and X bind/unbind also enforce session at their routes.
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.user = get_user_from_request(request)
    request.state.lang = detect_lang(request)
    path = request.url.path

    # Account namespace is the human browser surface. Capability tokens
    # (Bearer) run tools/REST/MCP — they never render or mutate
    # the account pages. Anonymous browsers get the login redirect instead.
    if path == "/account" or path.startswith("/account/"):
        via = getattr(request.state, "auth_via", "")
        if not request.state.user:
            if request.method in ("GET", "HEAD"):
                return RedirectResponse("/auth/login", status_code=302)
            return JSONResponse({"ok": False, "error": "auth_required"},
                                status_code=401)
        if via != "session":
            if request.method in ("GET", "HEAD"):
                return RedirectResponse("/auth/login", status_code=302)
            return JSONResponse({"ok": False, "error": "auth_required"},
                                status_code=401)

    user_id = request.state.user.get("id") if request.state.user else None
    if any(path.startswith(p) for p in _HEAVY_PATHS):
        async with _concurrency:
            response = await call_next(request)
    else:
        response = await call_next(request)

    # change_password/set_password revoke every old session/token atomically.
    # A successful browser password POST gets exactly one fresh current session.
    if (request.method == "POST" and path == "/account/password"
            and user_id and getattr(request.state, "auth_via", "") == "session"
            and 300 <= response.status_code < 400):
        try:
            from app.web_auth import create_browser_session
            fresh = create_browser_session(user_id)
            response.set_cookie(COOKIE_NAME, fresh, max_age=SESSION_TTL,
                                httponly=True, secure=True, samesite="lax")
        except Exception:
            logger.exception("password session rotation failed uid=%s", user_id)
            return JSONResponse({"ok": False, "error": "internal_error"}, 500)
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True, "status": "healthy"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        {"ok": False, "error": "internal_error"},
        status_code=500,
    )


# ── Routes ──

from app.routes import pages, api, auth_route, proxy, apiv1, samelabs, mcp, usertools  # noqa: E402

app.include_router(pages.router)
app.include_router(api.router, prefix="/api")
app.include_router(apiv1.router, prefix="/api/v1")
app.include_router(auth_route.router, prefix="/auth")
app.include_router(samelabs.router)
app.include_router(mcp.router)
from app.routes import topup  # noqa: E402
app.include_router(topup.router)
app.include_router(usertools.router)
from app.routes import earn
app.include_router(earn.router)
app.include_router(proxy.router)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    path = request.url.path
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return RedirectResponse("/", status_code=302)


_housekeeping_task = None
_HOUSEKEEPING_INTERVAL = 3600


async def _resource_housekeeping_loop():
    """One lightweight lifecycle pass per hour; no scheduler subsystem."""
    from sharing.housekeeping import housekeeping_once
    while True:
        await asyncio.sleep(_HOUSEKEEPING_INTERVAL)
        try:
            stats = await asyncio.to_thread(housekeeping_once)
            if stats["expired"] or stats["usage_purged"] or stats["storage_reconciled"]:
                logger.info("resource housekeeping: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("resource housekeeping failed")


@app.on_event("startup")
async def startup():
    global _housekeeping_task
    init_db()
    # Tier 数据 fail-closed 校验: 非法 plans 行 (缺 free / 权益越界) 必须
    # abort 启动 — 不留"流量已就绪但目录非法"的窗口。
    from governance.plan_catalog import validate_config
    validate_config()
    # Durable settlement for invocation facts left running by a dead process
    # (single-worker deployment). Runs after schema init, before traffic.
    from execution.gate import recover_abandoned_calls
    recovered = recover_abandoned_calls()
    if recovered.get("scanned"):
        logger.info("startup call recovery: %s", recovered)
    try:
        from tools import load_all, _registry
        load_all()
        from governance.pricing import seed_pricing
        seeded = seed_pricing([t["id"] for t in _registry])
        logger.info("pricing seeded: +%d rows", seeded["added"])
        from governance.catalog import seed_from_registry, validate_catalog_rows
        sync = seed_from_registry()
        logger.info("platform_tools seeded: +%d added, %d updated",
                    sync["added"], sync["updated"])
        validate_catalog_rows()
        logger.info("catalog rows validated")
        from governance.platform import ensure_platform_account
        pid = ensure_platform_account()
        logger.info("platform account ready: id=%s", pid)

        # Resource lifecycle is dual-owned: Gokapi expires physical bytes;
        # 518 periodically converges public-id mappings and rolling quota facts.
        from sharing.housekeeping import housekeeping_once
        housekeeping = await asyncio.to_thread(housekeeping_once)
        logger.info("startup resource housekeeping: %s", housekeeping)
        _housekeeping_task = asyncio.create_task(_resource_housekeeping_loop())
    except Exception:
        logger.exception("startup seed failed - ABORT")
        raise
    logger.info("%s started", APP_NAME)


@app.on_event("shutdown")
async def shutdown():
    global _housekeeping_task
    if _housekeeping_task is None:
        return
    _housekeeping_task.cancel()
    try:
        await _housekeeping_task
    except asyncio.CancelledError:
        pass
    _housekeeping_task = None
