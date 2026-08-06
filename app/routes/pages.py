"""Page routes: home, file detail, auth pages — all via Jinja2 templates."""

import sys
import json
import datetime
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

_BASE = Path(__file__).resolve().parent.parent.parent
_CORE = _BASE / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_BASE))

from config import APP_NAME, BASE_URL, SOCIAL_IMAGE_URL, MAX_UPLOAD_BYTES, DAILY_UPLOADS, DAILY_BYTES
from database import db
from i18n import LOCALE_TAGS, server_text
from icons import _ICONS, icon_svg, icon_path
from tools import list_all_tools
import tools_registry, tools_beautify, tools_compress, tools_ico  # noqa: F401
from auth import public_user
from uploads import get_upload_by_public_id
from gokapi import get_gokapi_file
from utils import (
    html_escape, format_bytes, format_ts, format_share_date, compact_filename,
    brand_icon_svg, upload_icon_svg, file_size_icon_svg, upload_count_icon_svg,
    bandwidth_icon_svg, expiry_icon_svg, download_count_icon_svg, refresh_icon_svg,
    close_icon_svg, file_format_icon, public_file_url,
)

router = APIRouter()

# Load static assets once
_STATIC = _BASE / "static"
with open(_STATIC / "style.css", encoding="utf-8") as f:
    CSS = f.read()
with open(_STATIC / "app.js", encoding="utf-8") as f:
    JS = f.read()

# i18n: single source of truth (core/i18n.py SERVER_I18N dict)
# Browser receives the full dict as window.__I18N__
import json as _json
from i18n import SERVER_I18N as _SERVER_I18N
I18N_JSON = _json.dumps(_SERVER_I18N, ensure_ascii=False)


def _ctx_base(request: Request) -> dict:
    """Base context for all pages."""
    lang = request.state.lang
    user = request.state.user
    return {
        "request": request,
        "lang": lang,
        "locale": LOCALE_TAGS.get(lang, "en"),
        "direction": "rtl" if lang == "ar" else "ltr",
        "texts": server_text(lang),
        "user": public_user(user) if user else None,
        "APP_NAME": APP_NAME,
        "BASE_URL": BASE_URL,
        "SOCIAL_IMAGE_URL": SOCIAL_IMAGE_URL,
        "CSS": CSS,
        "year": datetime.datetime.now().year,
        # icon helpers
        "icon_svg": icon_svg,
        "icon_path": icon_path,
        "brand_icon": brand_icon_svg(),
        "close_icon": close_icon_svg(),
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
    body = "User-agent: *\nDisallow: /api/\nDisallow: /auth/\nDisallow: /f/\nDisallow: /d/\nDisallow: /dh/\nAllow: /\nSitemap: https://518.plus/sitemap.xml\n"
    return Response(content=body, media_type="text/plain")

@router.get("/sitemap.xml")
async def sitemap():
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    body += '<url><loc>https://518.plus/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
    body += '</urlset>'
    return Response(content=body, media_type="application/xml")


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "doc_title": "Terms", "doc_body": _load_doc("terms.html")})
    return templates.TemplateResponse("doc.html", ctx)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "doc_title": "Privacy", "doc_body": _load_doc("privacy.html")})
    return templates.TemplateResponse("doc.html", ctx)


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

    tools = list_all_tools()
    max_mb = MAX_UPLOAD_BYTES // 1024 // 1024
    home_share_url = "https://x.com/intent/tweet?" + urllib.parse.urlencode({
        "text": texts.get("home_share_text", texts.get("seo_title", "Flash Share")),
        "url": f"{BASE_URL}/?{urllib.parse.urlencode({'x': lang})}",
    })

    ctx.update({
        "shell_class": "",
        "share_url": home_share_url,
        "max_mb": max_mb,
        "daily_uploads": DAILY_UPLOADS,
        "daily_mb": DAILY_BYTES // 1024 // 1024,
        "tools": tools,
        "user_json": json.dumps(public_user(user) if user else None, ensure_ascii=False),
        "limits_json": json.dumps({"maxMb": max_mb, "dailyUploads": DAILY_UPLOADS, "dailyMb": DAILY_BYTES // 1024 // 1024, "maxDays": 7, "maxDownloads": 50}),
        "i18n_json": I18N_JSON,
        "tools_json": json.dumps(tools, ensure_ascii=False),
        "icons_json": json.dumps(_ICONS, ensure_ascii=False),
        "JS": JS,
        "og_title": texts.get("home_share_text", texts.get("seo_title", "Flash Share")),
        "og_desc": texts.get("subtitle", texts.get("seo_desc", "")),
        "file_size_icon": file_size_icon_svg(),
        "upload_count_icon": upload_count_icon_svg(),
        "bandwidth_icon": bandwidth_icon_svg(),
        "expiry_icon": expiry_icon_svg(),
        "download_count_icon": download_count_icon_svg(),
        "upload_icon": upload_icon_svg(),
        "refresh_icon": refresh_icon_svg(),
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


@router.get("/auth/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from app.main import templates
    ctx = _ctx_base(request)
    u = public_user(user)
    texts = ctx["texts"]
    ctx.update({
        "shell_class": "",
        "share_url": "",
        "u": u,
        "user_has_password": u.get("hasPassword", False),
        "x_bound": bool(u.get("xUsername")),
    })
    return templates.TemplateResponse("auth/profile.html", ctx)
