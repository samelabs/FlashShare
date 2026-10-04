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

from config import APP_NAME, BASE_URL, COOKIE_NAME, SESSION_TTL
from database import init_db
from security import token_hash
from i18n import language_from_code, language_from_header
from utils import now
import tools_registry  # noqa: F401 — registers share + privacy tools
from tools import load_all

load_all()

# ── Runtime source identity (task 9) ──
# Computed ONCE at startup — no per-request git exec. /healthz carries
# source_sha + source_dirty so an operator can see (and deploy postflight
# can assert) exactly which commit production is running. "unknown" keeps
# health semantics (200) but must be rejected by deploy postflight.
import subprocess as _subprocess

_SOURCE_IDENTITY: dict = {}


def _compute_source_identity() -> dict:
    try:
        sha = _subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        dirty = _subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        return {"source_sha": sha, "source_dirty": bool(dirty)}
    except Exception:
        return {"source_sha": "unknown", "source_dirty": False}


_SOURCE_IDENTITY.update(_compute_source_identity())


def source_identity() -> dict:
    return dict(_SOURCE_IDENTITY)

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
# Autoescape ON: every interpolated value escapes by default. The only
# raw-HTML producers are platform-owned — icons.py returns Markup, and
# the four |safe sites are platform-generated JSON (limits/delivery) and
# the repo-resident policy documents (doc_body). No user-controlled
# value reaches the page unescaped.
templates.env.autoescape = True

from governance.tool_params import resolve_label as _resolve_label

def _lt(value, lang: str):
    # plain resolution: env autoescape performs the escaping
    return _resolve_label(value, lang)

templates.env.filters["lt"] = _lt
templates.env.globals["lt"] = _lt


def _numfmt(value, lang: str):
    """Group digits by the page language's convention (display layer
    only — the value itself is untouched)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return value
    sep = {"fr": "\u202f", "ru": "\u202f", "de": ".", "pt": ".",
           "es": "."}.get(lang, ",")
    sign = "-" if n < 0 else ""
    digits = str(abs(n))
    grouped = ""
    while len(digits) > 3:
        grouped = sep + digits[-3:] + grouped
        digits = digits[:-3]
    return sign + digits + grouped


templates.env.filters["num"] = _numfmt
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── Auth helpers ──

def get_user_from_request(request: Request):
    """Browser session identity for the web surface; Bearer tokens are
    resolved only for the /mcp machine surface that authenticates with
    them. A Bearer header never turns its holder into a logged-in
    web user."""
    from database import db
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            from repo.sessions import user_by_session_token
            with db() as con:
                row = user_by_session_token(con, token_hash(token), now())
        except Exception:
            return None
        if row:
            request.state.auth_via = "session"
            return dict(row)
        # A stale/invalid cookie is no identity: /mcp still resolves its
        # Bearer below; every other path stays anonymous.
    path = request.url.path
    auth = request.headers.get("authorization", "")
    if path == "/mcp/openai" and auth.lower().startswith("bearer "):
        from identity.oauth_provider import authenticate_access_token
        identity = authenticate_access_token(
            auth[7:].strip(), resource=f"{BASE_URL}/mcp/openai")
        if identity:
            request.state.auth_via = "oauth"
            request.state.oauth_scopes = identity["scopes"]
            return identity["user"]
        return None
    if path == "/mcp" and auth.lower().startswith("bearer "):
        from identity.apikeys import authenticate_key
        user = authenticate_key(auth[7:].strip())
        if user:
            request.state.auth_via = "token"
        return user
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
# Heavy surface gate.
_HEAVY_PATHS = ("/api/uploads", "/api/tools/", "/mcp",
                "/auth/login", "/auth/register")
# Mutating account identity/credentials is a browser-session surface. Capability
# tokens may run the machine surface (MCP), but cannot use themselves to
# mint/alter account credentials. Token CRUD and X bind/unbind also enforce
# session at their routes.
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.user = get_user_from_request(request)
    request.state.lang = detect_lang(request)
    path = request.url.path
    if request.state.user and getattr(request.state, "auth_via", "") in (
            "session", "token"):
        # lapsed-renewal compensation on authenticated access (web
        # session / MCP token): throttled per user, background thread
        try:
            from economy.reconcile import reconcile_on_access
            reconcile_on_access(int(request.state.user["id"]))
        except Exception:
            logger.exception("access reconcile trigger failed")

    # Account namespace is the human browser surface (session-only).
    # Capability tokens (Bearer) serve MCP only —
    # they never render or mutate the account pages. Anonymous browsers get
    # the login redirect instead.
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
    # Small-form surface cap: these endpoints carry tiny credentials /
    # diagnostic payloads; a body over 64KB is abuse, not input. Checked
    # BEFORE the heavy-path semaphore so bulk cannot occupy concurrency
    # slots (and cannot be slow-dripped through them).
    if path in ("/api/client-error", "/auth/login", "/auth/register"):
        _declared_len = request.headers.get("content-length")
        if _declared_len:
            try:
                if int(_declared_len) > 65536:
                    return JSONResponse(
                        {"ok": False, "error": "err_payload_too_large"},
                        status_code=413)
            except ValueError:
                pass  # malformed length: the transport still bounds it
    if any(path.startswith(p) for p in _HEAVY_PATHS):
        async with _concurrency:
            response = await call_next(request)
    else:
        response = await call_next(request)

    # Agent indexing boundary (B7): /f/ share surfaces stay fetchable
    # (robots.txt no longer blocks them) but never indexable; /mcp is a
    # POST-only machine surface — both carry the noindex header on every
    # response.
    if path.startswith("/f/") or path == "/mcp" or path.startswith("/mcp/"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"

    # change_password/set_password delete every old browser session (API
    # keys are independent program credentials and survive). A successful
    # browser password POST gets exactly one fresh current session.
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
    resp = {"ok": True, "status": "healthy"}
    resp.update(source_identity())
    return resp


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        {"ok": False, "error": "internal_error"},
        status_code=500,
    )


# ── Routes ──

from app.routes import pages, api, auth_route, proxy, samelabs, mcp, mcp_openai, oauth_provider, mcp_upload, usertools, favorites  # noqa: E402

app.include_router(pages.router)
app.include_router(api.router, prefix="/api")
app.include_router(auth_route.router, prefix="/auth")
app.include_router(samelabs.router)
app.include_router(mcp.router)
app.include_router(mcp_openai.router)
app.include_router(oauth_provider.router)
app.include_router(mcp_upload.router)
from app.routes import topup  # noqa: E402
app.include_router(topup.router)
app.include_router(usertools.router)
app.include_router(favorites.router)
from app.routes import earn
app.include_router(earn.router)
app.include_router(proxy.router)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    path = request.url.path
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    # real 404 for humans and agents: a rendered page carrying the way
    # out (home / tools / MCP setup / the LLM plain-text guide)
    from i18n import server_text
    from app.web_ctx import ctx_base
    ctx = ctx_base(request)
    ctx["shell_class"] = ""
    return templates.TemplateResponse(request, "not_found.html", ctx,
                                      status_code=404)


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
    # X login needs both halves of the OAuth client; one without the other
    # silently disables it, so say so in the startup log.
    from config import X_CLIENT_ID, X_CLIENT_SECRET
    if bool(X_CLIENT_ID) != bool(X_CLIENT_SECRET):
        logger.warning("X login disabled: set both FLASHSHARE_X_CLIENT_ID "
                       "and FLASHSHARE_X_CLIENT_SECRET")
    # Gokapi capability probe (task 7): fail-closed BEFORE traffic. If the
    # service key lacks DOWNLOAD permission the whole tool/preview chain
    # would fail at user time — abort startup with the explicit marker.
    from gokapi import verify_download_permission, InternalDownloadUnavailable
    try:
        verify_download_permission()
        logger.info("gokapi capability probe: download permission OK")
    except InternalDownloadUnavailable as exc:
        logger.error("gokapi capability probe failed: %s", exc)
        raise RuntimeError(f"gokapi capability probe failed: {exc}") from exc
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
    # Durable settlement for ingress receipts left running by a dead
    # process: exact usage release + receipt failed; no Gokapi deletion
    # (receipts never held durable object-id authority; orphans age out
    # via the existing Gokapi TTL).
    from sharing.ingress_receipt import recover_abandoned_ingress_requests
    ing = recover_abandoned_ingress_requests()
    if ing["scanned"]:
        logger.info("startup ingress recovery: %s", ing)
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
