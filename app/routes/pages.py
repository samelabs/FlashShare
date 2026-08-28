"""Page routes: home, file detail, auth pages — all via Jinja2 templates."""

import sys
import json
import hashlib
import datetime
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response, RedirectResponse

_BASE = Path(__file__).resolve().parent.parent.parent
_CORE = _BASE / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_BASE))

from config import APP_NAME, BASE_URL, SOCIAL_IMAGE_URL, MAX_UPLOAD_BYTES, DAILY_UPLOADS, DAILY_BYTES
from i18n import LOCALE_TAGS, server_text
from icons import _ICONS, icon_svg, icon_path
from tools import load_all
from catalog import list_catalog, seed_from_registry
from database import db


def _fmt_ts(ts) -> str:
    """Unix ts → local time string for templates."""
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")

load_all()  # auto-discover every tools_*.py
from auth import public_user
from utils import (
    brand_icon_svg, upload_icon_svg, file_size_icon_svg, upload_count_icon_svg,
    bandwidth_icon_svg, expiry_icon_svg, download_count_icon_svg,
    close_icon_svg, history_icon_svg,
)

router = APIRouter()

# Load static assets once
_STATIC = _BASE / "static"
with open(_STATIC / "style.css", encoding="utf-8") as f:
    _CSS = f.read()
# Version stamp = content hash + mtime: content-converging edits (diverge then
# reconverge) can collide on a pure content hash and serve stale edge caches.
import os as _os
_CSS_VERSION = hashlib.md5((_CSS + str(int(_os.path.getmtime(_STATIC / "style.css")))).encode()).hexdigest()[:8]
with open(_STATIC / "app.js", encoding="utf-8") as f:
    JS = f.read()
# app.js gets its OWN stamp: it changes independently of style.css. Sharing
# css_version meant an app.js-only edit kept the old stamp → edge served the
# stale (buggy) app.js forever under the same URL.
_JS_VERSION = hashlib.md5((JS + str(int(_os.path.getmtime(_STATIC / "app.js")))).encode()).hexdigest()[:8]
# Separate cache-bust version for i18n.js — it changes independently of style.css
with open(_STATIC / "i18n.js", encoding="utf-8") as f:
    _I18N_JS = f.read()
_I18N_VERSION = hashlib.md5((_I18N_JS + str(int(_os.path.getmtime(_STATIC / "i18n.js")))).encode()).hexdigest()[:8]


def _ctx_base(request: Request) -> dict:
    """Base context for all pages."""
    lang = request.state.lang
    user = request.state.user
    from billing import spendable_balance as get_balance
    return {
        "request": request,
        "lang": lang,
        "locale": LOCALE_TAGS.get(lang, "en"),
        "direction": "rtl" if lang == "ar" else "ltr",
        "texts": server_text(lang),
        "user": public_user(user) if user else None,
        # dict (not pre-dumped JSON) so base.html can apply |tojson,
        # which escapes < > & — required inside a <script> block.
        "user_json": public_user(user) if user else None,
        # Header credit chip (None for anonymous)
        "credits": get_balance(user["id"]) if user else None,
        "icons_json": json.dumps(_ICONS, ensure_ascii=False),
        "APP_NAME": APP_NAME,
        "BASE_URL": BASE_URL,
        "SOCIAL_IMAGE_URL": SOCIAL_IMAGE_URL,
        "css_version": _CSS_VERSION,
        "js_version": _JS_VERSION,
        "i18n_version": _I18N_VERSION,
        "fmt_ts": _fmt_ts,
        "year": datetime.datetime.now().year,
        # icon helpers
        "icon_svg": icon_svg,
        "icon_path": icon_path,
        "brand_icon": brand_icon_svg(),
        "close_icon": close_icon_svg(),
        "history_icon_svg": history_icon_svg(),
    }


# ── Static system routes ──

@router.get("/site.webmanifest")
async def manifest():
    with open(_STATIC / "manifest.json") as f:
        return Response(content=f.read(), media_type="application/manifest+json")

@router.get("/sw.js")
async def service_worker():
    with open(_STATIC / "sw.js") as f:
        return Response(content=f.read(), media_type="application/javascript")

@router.get("/offline", response_class=HTMLResponse)
async def offline_page(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    return templates.TemplateResponse("offline.html", ctx)

@router.get("/robots.txt")
async def robots():
    body = f"User-agent: *\nDisallow: /api/\nDisallow: /auth/\nDisallow: /f/\nDisallow: /d/\nDisallow: /dh/\nAllow: /\nSitemap: {BASE_URL}/sitemap.xml\n"
    return Response(content=body, media_type="text/plain")

@router.get("/sitemap.xml")
async def sitemap():
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    body += f'<url><loc>{BASE_URL}/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
    body += '</urlset>'
    return Response(content=body, media_type="application/xml")


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "doc_title": ctx["texts"].get("terms_title", "Terms of Service"), "doc_body": _load_doc("terms.html")})
    return templates.TemplateResponse("doc.html", ctx)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "doc_title": ctx["texts"].get("privacy_title", "Privacy Policy"), "doc_body": _load_doc("privacy.html")})
    return templates.TemplateResponse("doc.html", ctx)


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    """Agent support surface: HTTP API + MCP server + skill."""
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": ""})
    return templates.TemplateResponse("agents.html", ctx)


_DOCS_DIR = _BASE / "templates"
def _load_doc(name: str) -> str:
    with open(_DOCS_DIR / name) as f:
        return f.read()


# ── Home ──

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    lang = ctx["lang"]
    user = request.state.user
    texts = ctx["texts"]

    tools = list_catalog(user["id"] if user else None)  # grid: local + remote + community (public)
    max_mb = MAX_UPLOAD_BYTES // 1024 // 1024
    home_share_url = "https://x.com/intent/tweet?" + urllib.parse.urlencode({
        "text": texts.get("home_share_text", texts.get("seo_title", "518.PLUS")),
        "url": BASE_URL,
    })

    ctx.update({
        "shell_class": "",
        "share_url": home_share_url,
        "max_mb": max_mb,
        "daily_uploads": DAILY_UPLOADS,
        "daily_mb": DAILY_BYTES // 1024 // 1024,
        "tools": tools,
        "limits_json": json.dumps({"maxMb": max_mb, "dailyUploads": DAILY_UPLOADS, "dailyMb": DAILY_BYTES // 1024 // 1024, "maxDays": 7, "maxDownloads": 200}),
        "tools_json": json.dumps(tools, ensure_ascii=False),
        "JS": JS,
        "og_title": texts.get("home_share_text", texts.get("seo_title", "518.PLUS")),
        "og_desc": texts.get("subtitle", texts.get("seo_desc", "")),
        "file_size_icon": file_size_icon_svg(),
        "upload_count_icon": upload_count_icon_svg(),
        "bandwidth_icon": bandwidth_icon_svg(),
        "expiry_icon": expiry_icon_svg(),
        "download_count_icon": download_count_icon_svg(),
        "upload_icon": upload_icon_svg(),
    })
    return templates.TemplateResponse("home.html", ctx)


# ── Auth pages ──

@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "error": error})
    return templates.TemplateResponse("auth/login.html", ctx)


@router.get("/auth/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = ""):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "error": error})
    return templates.TemplateResponse("auth/register.html", ctx)


# ── Account pages: profile split into focused pages (/auth/profile removed) ──

def _account_ctx(request: Request, page: str, **extra) -> dict:
    from app.main import templates  # noqa: F401
    from billing import spendable_balance as get_balance
    ctx = _ctx_base(request)
    ctx.update({
        "shell_class": "",
        "acct_page": page,
        "credits": get_balance(request.state.user["id"]) if request.state.user else 0,
    })
    ctx.update(extra)
    return ctx


@router.get("/account/profile", response_class=HTMLResponse)
async def account_profile(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from auth import public_user
    u = public_user(user)
    ctx = _account_ctx(request, "profile",
                       u=u, user_has_password=u.get("hasPassword", False),
                       x_bound=bool(u.get("xUsername")))
    return templates.TemplateResponse("account/profile.html", ctx)


@router.get("/account/security", response_class=HTMLResponse)
async def account_security(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from auth import public_user
    u = public_user(user)
    ctx = _account_ctx(request, "security",
                       u=u, user_has_password=u.get("hasPassword", False),
                       x_bound=bool(u.get("xUsername")))
    return templates.TemplateResponse("account/security.html", ctx)


@router.get("/account/credits", response_class=HTMLResponse)
async def account_credits(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from billing import spendable_balance as get_balance, history_paged
    from activities import get_activity, granted_today
    daily = get_activity("daily_login")
    daily_amount = int(daily["amount"]) if daily and daily["active"] else 0
    pg = 1
    try:
        pg = max(1, int(request.query_params.get("page", "1") or 1))
    except ValueError:
        pg = 1
    size = 20
    rows, total = history_paged(user["id"], page=pg, size=size)
    # packages (one-off top-up packs) — active rows only, data-driven
    with db() as con:
        pkgs = [dict(r) for r in con.execute(
            "SELECT code, name, credits, price_cents, currency FROM packages "
            "WHERE active ORDER BY sort_order, id").fetchall()]
    ctx = _account_ctx(request, "credits",
                       balance=get_balance(user["id"]),
                       granted_today=granted_today("daily_login", user["id"]) if daily_amount > 0 else True,
                       daily_amount=daily_amount,
                       packages=pkgs,
                       priced_tools=[t for t in list_catalog(user["id"]) if t["cost"] > 0],
                       rows=rows, total=total, pg=pg,
                       pages=(total + size - 1) // size)
    return templates.TemplateResponse("account/credits.html", ctx)


@router.post("/account/credits/claim")
async def account_credits_claim(request: Request):
    """Explicit daily-bonus claim button.

    Closes the gap where a long-lived session crossing UTC midnight
    never re-triggers the login-time grant: the user just clicks claim.
    Idempotency is the same daily:{uid}:{yyyymmdd} key — clicking twice
    in one day simply re-renders with 'already granted'.
    """
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from activities import apply_activity
    try:
        apply_activity("daily_login", user["id"])
    except Exception:
        logger.exception("daily claim failed")
    return RedirectResponse("/account/credits", 303)


@router.get("/account/earnings", response_class=HTMLResponse)
async def account_earnings(request: Request):
    """Earnings & withdrawals (money view — its own module since 08-26)."""
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    ctx = _account_ctx(request, "earnings")
    return templates.TemplateResponse("account/earnings.html", ctx)



@router.get("/account/tools", response_class=HTMLResponse)
async def account_tools(request: Request):
    """User-published tools (C2C supply side)."""
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from app.routes.usertools import _owner_rows, _public_item, MAX_TOOLS_PER_USER
    # NOTE: do NOT pass icons_json here — base.html already injects the
    # icon dict under that exact key; overriding it broke site-wide lookups.
    ctx = _account_ctx(request, "tools",
                       tools=[_public_item(r) for r in _owner_rows(user["id"])],
                       max_tools=MAX_TOOLS_PER_USER)
    return templates.TemplateResponse("account/tools.html", ctx)


@router.get("/account/tokens", response_class=HTMLResponse)
async def account_tokens(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    ctx = _account_ctx(request, "tokens")
    return templates.TemplateResponse("account/tokens.html", ctx)


# Legacy /auth/profile is intentionally gone (single-page account removed).
# Profile/password POST handlers moved here from auth_route (account forms).

@router.post("/account/profile")
async def account_profile_save(request: Request):
    from app.main import templates
    from auth import update_profile, public_user
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})

    form = await request.form()
    display_name = (form.get("display_name") or "").strip()
    email = (form.get("email") or "").strip()
    bio = (form.get("bio") or "").strip()
    payout_channel = (form.get("payout_channel") or "").strip()

    try:
        update_profile(user["id"], display_name=display_name or None, email=email or None, bio=bio or None, payout_channel=payout_channel or None)
    except Exception as exc:
        u = public_user(user)
        ctx = _account_ctx(request, "profile", u=u,
                           user_has_password=u.get("hasPassword", False),
                           x_bound=bool(u.get("xUsername")), error=str(exc))
        return templates.TemplateResponse("account/profile.html", ctx)

    return RedirectResponse("/account/profile", 302)


@router.post("/account/password")
async def account_password_save(request: Request):
    from app.main import templates
    from auth import public_user, has_password, change_password, set_password
    from i18n import server_text
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    lang = getattr(request.state, "lang", "en")

    form = await request.form()
    new_password = form.get("new_password") or ""
    new_password_confirm = form.get("new_password_confirm") or ""
    old_password = form.get("old_password") or ""

    def _err(msg):
        u = public_user(user)
        ctx = _account_ctx(request, "security", u=u,
                           user_has_password=u.get("hasPassword", False),
                           x_bound=bool(u.get("xUsername")), error=msg)
        return templates.TemplateResponse("account/security.html", ctx)

    if new_password != new_password_confirm:
        return _err(server_text(lang).get("passwordMismatch", ""))
    if not new_password:
        return _err(server_text(lang).get("err_fill_login", ""))

    if has_password(user["id"]):
        if not old_password:
            return _err(server_text(lang).get("err_fill_login", ""))
        try:
            change_password(user["id"], old_password, new_password)
        except Exception as exc:
            return _err(str(exc))
    else:
        set_password(user["id"], new_password)

    return RedirectResponse("/account/security", 302)
