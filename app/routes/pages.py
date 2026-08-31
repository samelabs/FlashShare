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
from governance.catalog import list_catalog, seed_from_registry

MAX_UPLOAD_MB = MAX_UPLOAD_BYTES // (1024 * 1024)
from database import db


def _fmt_ts(ts) -> str:
    """Unix ts → local time string for templates."""
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")

load_all()  # auto-discover every tools_*.py
from identity.auth import public_user
from utils import (
    brand_icon_svg, upload_icon_svg, file_size_icon_svg, upload_count_icon_svg,
    bandwidth_icon_svg, expiry_icon_svg, download_count_icon_svg,
    close_icon_svg, history_icon_svg,
)

router = APIRouter()



from app.web_ctx import (  # noqa: F401 (模板/路由沿用原名)
    ctx_base as _ctx_base_impl, _CSS_VERSION, _JS_VERSION, _I18N_VERSION, JS,
)


def _ctx_base(request):
    return _ctx_base_impl(request)


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
    return templates.TemplateResponse(request, "offline.html", ctx)

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
    return templates.TemplateResponse(request, "doc.html", ctx)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "doc_title": ctx["texts"].get("privacy_title", "Privacy Policy"), "doc_body": _load_doc("privacy.html")})
    return templates.TemplateResponse(request, "doc.html", ctx)


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    """Agent support surface: HTTP API + MCP server + skill."""
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": ""})
    return templates.TemplateResponse(request, "agents.html", ctx)



# ── Tool market & tool page (open platform supply side) ──

def _owner_handle(owner_id) -> str:
    from database import db
    from repo.users import username_of
    with db() as con:
        row = username_of(con, owner_id)
    return row["username"] if row else "unknown"


def _public_stats(tool_key: str) -> dict:
    """Public counters: successful calls only. No user-level data."""
    from database import db
    from repo.ledger import call_stats_by_tool
    with db() as con:
        row = call_stats_by_tool(con, tool_key)
    return {"calls": int(row["ok"]) if row else 0}


@router.get("/tools", response_class=HTMLResponse)
async def tools_market(request: Request):
    """Open-platform market: official row + community cards. Public only."""
    from app.main import templates
    from governance.catalog import list_catalog
    from database import db
    ctx = _ctx_base(request)
    from icons import icon_svg
    tools_all = []
    for t in list_catalog(None):
        t["icon_svg"] = icon_svg(t["icon"] or "wand")
        t["stats"] = _public_stats(t["id"])
        tools_all.append(t)
    ctx.update({"shell_class": "", "tools_all": tools_all})
    return templates.TemplateResponse(request, "tools_market.html", ctx)


def _tool_page_ctx(request: Request, owner: str, tool_id: str, mcp: bool):
    """Shared builder for tool page + MCP page. Non-public → None (404, no probe)."""
    from governance.catalog import resolve
    user = request.state.user
    tool_key = f"{owner}/{tool_id}"
    tool, err = resolve(tool_key, user["id"] if user else None)
    if err or tool.get("visibility") != "public":
        return None
    ctx = _ctx_base(request)
    import json as _json
    try:
        label_map = json.loads(tool.get("label_json") or "{}")
    except Exception:
        label_map = {}
    from icons import icon_svg
    # Public-surface whitelist ONLY (same shape as catalog items): the raw
    # DB row (endpoint_json/auth_token/owner_id/trusted/timestamps) must
    # never reach any client — owner or anonymous.
    tool_public = {
        "id": tool["tool_key"],
        "type": tool["tool_type"],
        "label": label_map,
        "icon": tool.get("icon"),
        "accept": _json.loads(tool["accept_json"]) if tool.get("accept_json") else None,
        "presets": None,
        "multi": bool(tool.get("multi")),
        "cost": int(tool.get("cost") or 0),
        "icon_svg": icon_svg(tool.get("icon") or "wand"),
    }
    ctx.update({
        "shell_class": "",
        "tool": tool_public,
        "tool_name": label_map.get(ctx["lang"]) or label_map.get("en") or tool_id,
        "handle": _owner_handle(tool["owner_id"]),
        "stats": _public_stats(tool_key),
        "mcp_name": tool_key.replace("/", "__"),
        "is_mcp": mcp,
        "tools_json": _json.dumps([tool_public], ensure_ascii=False),
        "limits_json": ctx.get("limits_json") or _json.dumps({"max_upload_mb": MAX_UPLOAD_MB}, ensure_ascii=False),
    })
    return ctx


@router.get("/tool/{owner}/{tool_id}", response_class=HTMLResponse)
async def tool_page(request: Request, owner: str, tool_id: str):
    from app.main import templates
    from fastapi.responses import HTMLResponse as _H
    ctx = _tool_page_ctx(request, owner, tool_id, mcp=False)
    if ctx is None:
        return _H("", status_code=404)
    return templates.TemplateResponse(request, "tool_page.html", ctx)


@router.get("/tool/{owner}/{tool_id}/mcp", response_class=HTMLResponse)
async def tool_mcp_page(request: Request, owner: str, tool_id: str):
    from app.main import templates
    from fastapi.responses import HTMLResponse as _H
    ctx = _tool_page_ctx(request, owner, tool_id, mcp=True)
    if ctx is None:
        return _H("", status_code=404)
    return templates.TemplateResponse(request, "tool_mcp.html", ctx)


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
    return templates.TemplateResponse(request, "home.html", ctx)


# ── Auth pages ──

@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "error": error})
    return templates.TemplateResponse(request, "auth/login.html", ctx)


@router.get("/auth/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = ""):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "error": error})
    return templates.TemplateResponse(request, "auth/register.html", ctx)


# ── Account pages: profile split into focused pages (/auth/profile removed) ──

def _account_ctx(request: Request, page: str, **extra) -> dict:
    from app.main import templates  # noqa: F401
    from economy.billing import spendable_balance as get_balance
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
    from identity.auth import public_user
    u = public_user(user)
    ctx = _account_ctx(request, "profile",
                       u=u, user_has_password=u.get("hasPassword", False),
                       x_bound=bool(u.get("xUsername")))
    return templates.TemplateResponse(request, "account/profile.html", ctx)


@router.get("/account/security", response_class=HTMLResponse)
async def account_security(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from identity.auth import public_user
    u = public_user(user)
    ctx = _account_ctx(request, "security",
                       u=u, user_has_password=u.get("hasPassword", False),
                       x_bound=bool(u.get("xUsername")))
    return templates.TemplateResponse(request, "account/security.html", ctx)


@router.get("/account/credits", response_class=HTMLResponse)
async def account_credits(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from economy.billing import spendable_balance as get_balance, history_paged
    from economy.activities import get_activity, granted_today
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
    from repo.orders import active_packages
    with db() as con:
        pkgs = [dict(r) for r in active_packages(con)]
    import economy.creem as _creem
    topup_ready = _creem.creem_enabled()
    ctx = _account_ctx(request, "credits",
                       balance=get_balance(user["id"]),
                       granted_today=granted_today("daily_login", user["id"]) if daily_amount > 0 else True,
                       daily_amount=daily_amount,
                       packages=pkgs, topup_ready=topup_ready,
                       rows=rows, total=total, pg=pg,
                       pages=(total + size - 1) // size)
    return templates.TemplateResponse(request, "account/credits.html", ctx)


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
    from economy.activities import apply_activity
    import logging as _logging
    try:
        apply_activity("daily_login", user["id"])
    except Exception:
        _logging.getLogger("flashshare").exception("daily claim failed")
    return RedirectResponse("/account/credits", 303)


@router.get("/account/earnings", response_class=HTMLResponse)
async def account_earnings(request: Request):
    """Earnings & withdrawals (money view — its own module since 08-26)."""
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    ctx = _account_ctx(request, "earnings")
    return templates.TemplateResponse(request, "account/earnings.html", ctx)



@router.get("/account/tools", response_class=HTMLResponse)
async def account_tools(request: Request):
    """User-published tools (C2C supply side)."""
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from governance.user_tools import _owner_rows, _public_item, MAX_TOOLS_PER_USER
    # NOTE: do NOT pass icons_json here — base.html already injects the
    # icon dict under that exact key; overriding it broke site-wide lookups.
    ctx = _account_ctx(request, "tools",
                       tools=[_public_item(r) for r in _owner_rows(user["id"])],
                       max_tools=MAX_TOOLS_PER_USER)
    return templates.TemplateResponse(request, "account/tools.html", ctx)


@router.get("/account/tokens", response_class=HTMLResponse)
async def account_tokens(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    ctx = _account_ctx(request, "tokens")
    return templates.TemplateResponse(request, "account/tokens.html", ctx)


# Legacy /auth/profile is intentionally gone (single-page account removed).
# Profile/password POST handlers moved here from auth_route (account forms).

@router.post("/account/profile")
async def account_profile_save(request: Request):
    from app.main import templates
    from identity.auth import update_profile, public_user
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
        return templates.TemplateResponse(request, "account/profile.html", ctx)

    return RedirectResponse("/account/profile", 302)


@router.post("/account/password")
async def account_password_save(request: Request):
    from app.main import templates
    from identity.auth import public_user, has_password, change_password, set_password
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
        return templates.TemplateResponse(request, "account/security.html", ctx)

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
