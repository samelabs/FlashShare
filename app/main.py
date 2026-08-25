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

# Core modules (zero-change from legacy, now in ../core/)
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

from config import APP_NAME, COOKIE_NAME, SESSION_TTL
from database import init_db
from security import token_hash
from i18n import language_from_code, language_from_header
from utils import now
import tools_registry  # noqa: F401 — registers share + privacy tools
from tools import load_all

load_all()  # auto-discover every tools_*.py (incl. tools_registry itself)

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
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)

_STATIC_DIR = _BASE_DIR / "static"
_TEMPLATES_DIR = _BASE_DIR / "app" / "templates"
_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# autoescape disabled — templates output raw HTML (icon_svg, CSS, JS).
# All user-supplied values are escaped with |e or |tojson.
templates.env.autoescape = False
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── Auth helpers (reuse core/security.py + core/database.py) ──

def get_user_from_request(request: Request):
    """Extract and validate identity: session cookie first, then Bearer.

    The Bearer path is the agent/MCP channel (api_tokens): same user dict,
    same downstream authorization. Cookie keeps browser flows unchanged.
    """
    from database import db
    from apitokens import authenticate_token
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        # Bearer channel for programmatic clients
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            user = authenticate_token(auth[7:].strip())
            if user:
                request.state.auth_via = "token"
            return user
        return None
    try:
        with db() as con:
            row = con.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = %s AND s.expires_at > %s",
                (token_hash(token), now()),
            ).fetchone()
        if row:
            request.state.auth_via = "session"
        return dict(row) if row else None
    except Exception:
        return None


def detect_lang(request: Request) -> str:
    """Detect preferred language: query > cookie > Accept-Language."""
    # Query param
    for param in ("lang", "x"):
        val = request.query_params.get(param, "")
        code = language_from_code(val)
        if code:
            return code
    # Cookie
    cookie_lang = request.cookies.get("flashshare_lang", "")
    code = language_from_code(cookie_lang)
    if code:
        return code
    # Header
    return language_from_header(request.headers.get("accept-language", ""))


# ── Middleware ──

import asyncio

# Concurrency limiter — for CPU/IO-heavy operations (uploads, tool execution,
# auth endpoints running bcrypt ~250ms/attempt: login + register).
# Page views and other lightweight requests are never throttled.
_concurrency = asyncio.Semaphore(8)
_HEAVY_PATHS = ("/api/uploads", "/api/tools/", "/auth/login", "/auth/register")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.user = get_user_from_request(request)
    request.state.lang = detect_lang(request)
    path = request.url.path
    if any(path.startswith(p) for p in _HEAVY_PATHS):
        async with _concurrency:
            response = await call_next(request)
    else:
        response = await call_next(request)
    return response


# ── Health check ──

@app.get("/healthz")
async def healthz():
    return {"ok": True, "status": "healthy"}


# ── Global exception handler ──

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        {"ok": False, "error": "internal_error"},
        status_code=500,
    )


# ── Routes ──

from app.routes import pages, api, auth_route, proxy, apiv1, samelabs, mcp  # noqa: E402

app.include_router(pages.router)
app.include_router(api.router, prefix="/api")
app.include_router(apiv1.router, prefix="/api/v1")
app.include_router(auth_route.router, prefix="/auth")
app.include_router(samelabs.router)
app.include_router(mcp.router)
app.include_router(proxy.router)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    path = request.url.path
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return RedirectResponse("/", status_code=302)


@app.on_event("startup")
async def startup():
    init_db()
    # Seed pricing rows for every registered tool (idempotent, default
    # cost 0 = free until priced in /samelabs). Tools registry must be
    # loaded first.
    try:
        from tools import load_all, _registry
        load_all()
        from pricing import seed_pricing
        seeded = seed_pricing([t["id"] for t in _registry])
        logger.info("pricing seeded: +%d rows", seeded["added"])
        # Data-state registry: compile register() declarations into
        # platform_tools rows (runtime catalog source). Retired-in-code
        # tools keep rows with enabled=false (history stays interpretable).
        from catalog import seed_from_registry
        sync = seed_from_registry()
        logger.info("platform_tools seeded: +%d added, %d updated",
                    sync["added"], sync["updated"])
    except Exception:
        logger.exception("pricing seed failed (non-fatal)")
    logger.info("%s started", APP_NAME)
