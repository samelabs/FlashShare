"""Page routes: home, file detail, auth pages — all via Jinja2 templates."""

import sys
import json
import logging
import hashlib
import datetime
import asyncio
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse

_BASE = Path(__file__).resolve().parent.parent.parent
_CORE = _BASE / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_BASE))

from config import APP_NAME, BASE_URL, SOCIAL_IMAGE_URL, MAX_UPLOAD_BYTES
from utils import file_glyph, page_number
from i18n import LOCALE_TAGS, server_text
from icons import _ICONS, icon_svg, icon_path
from tools import load_all
from governance.catalog import list_catalog
from governance.catalog import seed_from_registry
from governance.formats import format_manifest
from governance.tool_params import resolve_label

from database import db


from app.web_ctx import fmt_ts as _fmt_ts  # 单点 (治理4, UTC)

load_all()  # auto-discover every tools_*.py
from identity.auth import public_user


router = APIRouter()



from app.web_ctx import _STATIC  # 静态目录单点 (web_ctx 持有)
from app.web_ctx import (  # noqa: F401 (模板/路由沿用原名)
    ctx_base as _ctx_base_impl, _CSS_VERSION, _I18N_VERSION,
    error_text,
)

logger = logging.getLogger("flashshare")


def _ctx_base(request):
    return _ctx_base_impl(request)


# ── Static system routes ──

@router.get("/site.webmanifest")
async def manifest():
    """PWA manifest; the description is injected from the i18n single
    source (the en seo_desc that also feeds the home meta description) —
    the static file carries no second copy to drift."""
    with open(_STATIC / "manifest.json") as f:
        data = json.load(f)
    data["description"] = server_text("en").get("seo_desc", "")
    return Response(content=json.dumps(data), media_type="application/manifest+json")

@router.get("/sw.js")
async def service_worker():
    with open(_STATIC / "sw.js") as f:
        return Response(content=f.read(), media_type="application/javascript")

@router.get("/offline", response_class=HTMLResponse)
async def offline_page(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    return templates.TemplateResponse(request, "offline.html", ctx)

@router.get("/llms.txt")
async def llms_txt():
    """Plain-text agent guide, generated from the live code authorities
    (protocol versions, endpoint, public catalog). Language-independent
    English by design."""
    from governance.llms_txt import build_llms_txt
    return Response(content=build_llms_txt(),
                    media_type="text/plain; charset=utf-8")


@router.get("/robots.txt")
async def robots():
    """Crawl boundary. Share links (/f/ and result pages /r/) stay
    FETCHABLE: link-preview bots and agents that honor robots must be able
    to read the share card and the result a person was handed; indexing
    is refused per-response (noindex), not here. Private namespaces are
    excluded: account, the operator back office, auth pages, the JSON API
    and the MCP machine surface (a POST JSON-RPC endpoint — MCP clients
    are not crawlers). /dh/ is the Nginx→Gokapi hotlink path."""
    body = ("User-agent: *\n"
            "Disallow: /account\n"
            "Disallow: /samelabs\n"
            "Disallow: /auth\n"
            "Disallow: /api/\n"
            "Disallow: /mcp\n"
            "Disallow: /dh/\n"
            "Allow: /\n"
            f"Sitemap: {BASE_URL}/sitemap.xml\n")
    return Response(content=body, media_type="text/plain")

@router.get("/sitemap.xml")
async def sitemap():
    """Every indexable page (the same set the routes mark
    seo_robots='index, follow'): the static surfaces, every public tool
    page + its MCP page, and the author pages behind public supply.
    lastmod only where a real update time exists — the tool declaration
    row's updated_at; static pages carry none rather than a fabricated
    date. Visibility comes from the one public-market predicate
    (repo._market_base), never re-derived here."""
    from database import db
    from repo import catalog as catalog_repo
    from governance.catalog import VIS_PUBLIC
    with db() as con:
        rows = catalog_repo.public_sitemap_rows(con, VIS_PUBLIC)

    def _xml_escape(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    def _w3c_date(ts) -> str:
        return datetime.datetime.fromtimestamp(
            int(ts), datetime.timezone.utc).strftime("%Y-%m-%d")

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in ("/", "/tools", "/agents", "/pricing", "/developers",
              "/terms", "/privacy"):
        lines.append(f"<url><loc>{_xml_escape(BASE_URL + p)}</loc></url>")
    owners = []
    for r in rows:
        loc = _xml_escape(f"{BASE_URL}/tool/{r['tool_key']}")
        lastmod = _w3c_date(r["updated_at"])
        lines.append(f"<url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>")
        lines.append(f"<url><loc>{loc}/mcp</loc><lastmod>{lastmod}</lastmod></url>")
        if r["owner_handle"]:
            owners.append(r["owner_handle"])
    for handle in sorted(set(owners)):
        lines.append(f"<url><loc>{_xml_escape(BASE_URL + '/u/' + handle)}</loc></url>")
    lines.append("</urlset>")
    return Response(content="\n".join(lines), media_type="application/xml")


def _support_note() -> str:
    """Append the public support channel to policy pages when configured."""
    from config import PUBLIC_SUPPORT_CHANNEL
    if not PUBLIC_SUPPORT_CHANNEL:
        return ""
    href = (PUBLIC_SUPPORT_CHANNEL if PUBLIC_SUPPORT_CHANNEL.startswith("http")
            else f"mailto:{PUBLIC_SUPPORT_CHANNEL}")
    return (f'<p class="doc-support"><a href="{href}" rel="noopener">'
            f'{PUBLIC_SUPPORT_CHANNEL}</a></p>')


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    lang = ctx["lang"]
    ctx.update({"shell_class": "", "seo_robots": "index, follow", "share_url": "", "doc_title": ctx["texts"].get("terms_title", "Terms of Service"),
        "doc_desc": ctx["texts"].get("terms_desc", ""), "doc_body": _load_doc("terms.html", lang) + _support_note()})
    return templates.TemplateResponse(request, "doc.html", ctx)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    lang = ctx["lang"]
    ctx.update({"shell_class": "", "seo_robots": "index, follow", "share_url": "", "doc_title": ctx["texts"].get("privacy_title", "Privacy Policy"),
        "doc_desc": ctx["texts"].get("privacy_desc", ""), "doc_body": _load_doc("privacy.html", lang) + _support_note()})
    return templates.TemplateResponse(request, "doc.html", ctx)


def _developer_facts() -> dict:
    """Code-constant projection for the developer guide: every number
    the /developers page shows comes from the authority module that
    enforces it — the policy doc never hand-writes a limit (the page
    renders through the app Jinja env, placeholders = these keys)."""
    from config import MAX_UPLOAD_BYTES
    from remote import MIN_TIMEOUT_S, MAX_TIMEOUT_S
    from governance.tool_supply import MAX_INPUT_FILES
    from governance.tool_params import TEXT_PARAM_MAX_CHARS, MAX_PARAMS
    from governance.user_tools import MAX_TOOLS_PER_USER
    from governance.commission import commission_rate
    from execution.results import MAX_RESULT_ITEMS, MAX_INLINE_RESULT_BYTES
    from economy.earnings import MIN_WITHDRAWAL_MILLIS
    from app.routes.usertools import MAX_PRICE
    with db() as con:
        rate = commission_rate(con)
    return {
        "base_url": BASE_URL,
        "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
        "files_max": MAX_INPUT_FILES,
        "timeout_min_s": MIN_TIMEOUT_S,
        "timeout_max_s": MAX_TIMEOUT_S,
        "max_items": MAX_RESULT_ITEMS,
        "inline_max_bytes": MAX_INLINE_RESULT_BYTES,
        "text_param_chars": TEXT_PARAM_MAX_CHARS,
        "max_params": MAX_PARAMS,
        "max_tools": MAX_TOOLS_PER_USER,
        "max_price": MAX_PRICE,
        "commission_rate": rate,
        "min_withdrawal_usd": MIN_WITHDRAWAL_MILLIS // 1000,
    }


@router.get("/developers", response_class=HTMLResponse)
async def developers(request: Request):
    """Public developer guide: the southbound HTTP contract, request/
    response shapes, declarations, limits and economics. Same policy-doc
    mechanism as /terms (per-locale file, en fallback) plus one render
    pass that substitutes the code-constant facts — open to everyone,
    anonymous included."""
    from app.main import templates
    ctx = _ctx_base_impl(request, "publish")
    lang = ctx["lang"]
    body = _load_doc("developers.html", lang)
    body = templates.env.from_string(body).render(_developer_facts())
    ctx.update({
        "shell_class": "",
        "seo_robots": "index, follow",
        "share_url": "",
        "doc_title": ctx["texts"].get("developers_title", "Developer Guide"),
        "doc_desc": ctx["texts"].get("developers_desc", ""),
        "doc_body": body,
    })
    return templates.TemplateResponse(request, "doc.html", ctx)


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    """Agent support surface: the MCP server."""
    from app.main import templates
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "seo_robots": "index, follow", "share_url": ""})
    return templates.TemplateResponse(request, "agents.html", ctx)



# ── Tool market & tool page (open platform supply side) ──

def _owner_handle(owner_id) -> str:
    from database import db
    from repo.users import username_of
    with db() as con:
        row = username_of(con, owner_id)
    return row["username"] if row else "unknown"


def _favorite_keys_for(user) -> list:
    from governance.favorites import favorite_keys
    return favorite_keys(user["id"])


def _public_stats(tool_key: str) -> dict:
    """Public counters: successful calls only. No user-level data."""
    from database import db
    from repo.ledger import call_stats_by_tool
    with db() as con:
        row = call_stats_by_tool(con, tool_key)
    return {"calls": int(row["ok"]) if row else 0}


def _market_page_window(current: int, total_pages: int,
                        radius: int = 2, edge: int = 2) -> list:
    """Web 页码窗口 (presentation, 非 governance): 固定大小, 不随 total_pages
    线性增长。规则: 首/末页恒可达, 当前页 ±radius 可达, 开头/结尾额外 edge 页;
    不可达区间折叠为单条 ellipsis。返回 [{"page": n} | {"ellipsis": True}]。"""
    if total_pages <= 7:
        return [{"page": p} for p in range(1, total_pages + 1)]
    pages = {1, total_pages}
    for p in range(2, edge + 2):
        pages.add(p)
        pages.add(total_pages + 1 - p)
    for p in range(current - radius, current + radius + 1):
        if 1 <= p <= total_pages:
            pages.add(p)
    out = []
    prev = 0
    for p in sorted(pages):
        if p - prev > 1:
            out.append({"ellipsis": True})
        out.append({"page": p})
        prev = p
    return out


@router.get("/tools", response_class=HTMLResponse)
async def tools_market(request: Request):
    """Open-platform market: server-side search + paginated public list.
    Search and pagination live in the URL (?q= / ?page=); pricing
    visibility, ordering and paging all happen in SQL."""
    from app.main import templates
    from governance.catalog import market_page as market_page_data

    page = page_number(request.query_params.get("page", "1"))
    q = (request.query_params.get("q") or "").strip()

    page_data = market_page_data(q=q or None, page=page)
    tools = page_data["items"]

    ctx = _ctx_base(request)
    ctx.update({
        "shell_class": "",
        "seo_robots": "index, follow",
        "tools_all": tools,
        "market_q": q,
        "market_page": page_data["page"],
        "market_total": page_data["total"],
        "market_total_pages": page_data["total_pages"],
        "market_page_items": _market_page_window(
            page_data["page"], page_data["total_pages"]),
    })
    return templates.TemplateResponse(request, "tools_market.html", ctx)


@router.get("/u/{handle}", response_class=HTMLResponse)
async def user_page(request: Request, handle: str):
    """用户页 (开发者公开面): 作者身份 + 公开工具罗列。并列于工具页
    (App Store 开发者页参照), 只透公开字段白名单, 404 不泄露存在性。"""
    from app.main import templates
    from governance.catalog import owner_public_tools
    from database import db
    from repo.ledger import ok_calls_by_owner

    with db() as con:
        urow = con.execute(
            "SELECT id, status FROM users WHERE username = %s",
            (handle,)).fetchone()
    if urow is None or urow["status"] == "suspended":
        # suspended provider: the author page does not exist
        return templates.TemplateResponse(request, "missing.html",
                                          _ctx_base(request), status_code=404)
    owner_id = urow["id"]
    tools = owner_public_tools(owner_id)
    with db() as con:
        total_calls = ok_calls_by_owner(con, owner_id, [t["id"] for t in tools])
    # Verified badge fact: the author is a current effective member —
    # the same batch predicate the access projection uses (no second
    # membership judgment here).
    from economy.subscription import effective_member_ids
    ctx = _ctx_base(request)
    user = request.state.user
    ctx.update({
        "shell_class": "",
        "seo_robots": "index, follow",
        "owner_handle": handle,
        "owner_verified": owner_id in effective_member_ids([owner_id]),
        "owner_tools": tools,
        "owner_total_calls": total_calls,
        "is_self": bool(user and user.get("id") == owner_id),
    })
    return templates.TemplateResponse(request, "user_page.html", ctx)


def _limits_projection(ctx: dict) -> dict:
    """THE single file/delivery limits projection for every input surface
    (home, tool page, publish preview). Source is always ctx_base's access
    projection; anonymous falls back to the Free capability
    (resolve_from_membership(None)), never hand-written Free values.
    Injected verbatim as window.__LIMITS__; window.__DELIVERY__ derives
    from the same dict, so no surface can drift from another.

    dailyRemaining = rolling-24h ingress headroom (entitlement files minus
    usage facts, same projection) — the quota hint consumes it and never
    recomputes a second number.
    """
    if ctx.get("access"):
        ent = ctx["access"]["entitlements"]
        used_files = int((ctx["access"].get("usage") or {})
                         .get("ingress_files_24h") or 0)
    else:
        from governance.entitlements import resolve_from_membership
        _raw = resolve_from_membership(None)
        ent = {"ingress_files_24h": _raw["ingress_files_24h"],
               "ingress_bytes_24h": _raw["ingress_bytes_24h"],
               "expiry_days": list(_raw["expiry_days"]),
               "download_limits": list(_raw["download_limits"])}
        used_files = 0
    days = list(ent["expiry_days"])
    dls = list(ent["download_limits"])
    gb = ent["ingress_bytes_24h"] / (1024 * 1024 * 1024)
    daily_files = ent["ingress_files_24h"]
    return {
        "maxFileMb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "dailyFiles": daily_files,
        "dailyRemaining": max(0, daily_files - used_files),
        "dailyGb": int(gb) if gb == int(gb) else round(gb, 1),
        "expiryDays": days,
        "downloadLimits": dls,
        "defaultDays": 1 if 1 in days else days[0],
        "defaultDownloads": 50 if 50 in dls else dls[0],
    }


def _limits_json(ctx: dict) -> str:
    return json.dumps(_limits_projection(ctx), ensure_ascii=False)


def _delivery_projection(ctx: dict) -> str:
    """Delivery options for the shared input component — derived from the
    single limits projection (same source, same shape everywhere)."""
    p = _limits_projection(ctx)
    return json.dumps({
        "days": p["expiryDays"], "downloads": p["downloadLimits"],
        "defaultDays": p["defaultDays"], "defaultDownloads": p["defaultDownloads"],
    })


def _tool_page_ctx(request: Request, owner: str | None, tool_id: str, mcp: bool):
    """Shared builder for tool page + MCP page. resolve() 是唯一权限判断
    (public/unlisted → everyone when key known; private → owner only)。

    owner=None → first-party bare-id tool (官方工具, 无作者命名空间)."""
    from governance.catalog import resolve, catalog_item, mcp_name
    user = request.state.user
    tool_key = f"{owner}/{tool_id}" if owner else tool_id
    tool, err = resolve(tool_key, user["id"] if user else None)
    if err:
        return None
    # 4A-fix5: Tool Page 不再手工重建公开 contract — canonical projection
    # (governance.catalog.row_to_item) + pricing overlay 是唯一数据源, 与
    # list/search/owner 同一 projection。raw resolved row 仅保留在服务端
    # (owner_id → handle 上下文), 敏感字段 (endpoint_json/auth_token/
    # owner_id/trusted/timestamps) 绝不进入 tool/tools_json。
    tool_public = catalog_item(tool)
    if tool_public is None:
        # pricing 缺行/停用 — 未完成商业创建的工具不渲染工具页
        # (判定在 governance.catalog 单点, 此处不重复实现)
        return None
    ctx = _ctx_base(request)
    from icons import icon_svg
    tool_public["icon_svg"] = icon_svg(tool_public.get("icon") or "wand")
    tool_name = resolve_label(tool_public.get("label") or {}, ctx["lang"]) or tool_id
    handle = _owner_handle(tool.get("owner_id")) if tool.get("owner_id") else None
    # meta description = 工具说明 (LocalizedText 协议解析);
    # 无说明的工具退回 "工具名 · @作者", 不输出空 description
    tool_desc = resolve_label(tool_public.get("desc") or {}, ctx["lang"]) or (
        f"{tool_name} · @{handle}" if handle else tool_name)
    ctx.update({
        "shell_class": "",
        "seo_robots": "index, follow",
        "tool_desc": tool_desc,
        "tool": tool_public,
        "delivery_json": _delivery_projection(ctx),
        "tool_name": tool_name,
        "handle": handle,
        "stats": _public_stats(tool_key),
        "mcp_name": mcp_name(tool_key),
        "tool_key": tool_key,
        "is_favorite": bool(user) and tool_key in _favorite_keys_for(user),
        "is_mcp": mcp,
        # publish gate guidance: the author viewing their own not-yet-public
        # tool sees the run-once hint (server-rendered, one sentence)
        "owner_gate_notice": bool(
            user and tool.get("owner_id")
            and int(tool["owner_id"]) == int(user["id"])
            and tool.get("visibility") != "public"),
        "tools_json": json.dumps([tool_public], ensure_ascii=False),
        "limits_json": _limits_json(ctx),
        "formats_json": json.dumps(format_manifest(), ensure_ascii=False),
    })
    return ctx


# ── First-party bare-id routes (官方工具, 无作者命名空间) ──
# /tool/{x}/mcp 必须注册在 /tool/{owner}/{tool_id} 之前, 否则
# /tool/compress/mcp 被两段式错吞 (owner=compress, tool_id=mcp)。
# FastAPI 按注册序匹配。

@router.get("/tool/{tool_id}/mcp", response_class=HTMLResponse)
async def tool_first_party_mcp_page(request: Request, tool_id: str):
    """官方工具 MCP 页 (/tool/compress/mcp)。

    兜底: 社区工具 slug 恰为 'mcp' 时, /tool/{owner}/mcp 是其 UI 页
    (被本路由遮蔽), 回退两段式解析。"""
    from app.main import templates
    from fastapi.responses import HTMLResponse as _H
    ctx = _tool_page_ctx(request, None, tool_id, mcp=True)
    if ctx is not None:
        return templates.TemplateResponse(request, "tool_mcp.html", ctx)
    ctx = _tool_page_ctx(request, tool_id, "mcp", mcp=False)
    if ctx is not None:
        return templates.TemplateResponse(request, "tool_page.html", ctx)
    return _H("", status_code=404)


@router.get("/tool/{tool_id}", response_class=HTMLResponse)
async def tool_first_party_page(request: Request, tool_id: str):
    from app.main import templates
    from fastapi.responses import HTMLResponse as _H
    ctx = _tool_page_ctx(request, None, tool_id, mcp=False)
    if ctx is None:
        return _H("", status_code=404)
    return templates.TemplateResponse(request, "tool_page.html", ctx)


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
_POLICY_DIR = _BASE / "templates" / "policies"
def _load_doc(name: str, lang: str = "en") -> str:
    """Policy document by locale; en fallback when a locale file is missing."""
    loc = _POLICY_DIR / lang / name
    if not loc.is_file():
        loc = _POLICY_DIR / "en" / name
    with open(loc, encoding="utf-8") as f:
        return f.read()


# ── Home ──

HOME_GRID_SIZE = 8

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    lang = ctx["lang"]
    user = request.state.user
    texts = ctx["texts"]

    # Grid = the curated browse list (featured, operator order); the
    # composer picker = the viewer's working set (first-party + own +
    # favorites); any other public tool comes from the picker's search.
    from governance.catalog import search_catalog
    from governance.favorites import working_set
    featured = search_catalog("", limit=HOME_GRID_SIZE)
    picker = working_set(user, list_catalog(user["id"] if user else None))

    # Delivery/ingress entitlement projection — consume ctx_base's single
    # access projection read (never a second project()); anonymous falls
    # back to the canonical pure Free capability (resolve_from_membership
    # (None)), never hand-written Free values. Web boundary converts the
    # internal tuples to lists once, here.
    lim = _limits_projection(ctx)

    ctx.update({
        "shell_class": "",
        "seo_robots": "index, follow",
        "max_mb": lim["maxFileMb"],
        "daily_uploads": lim["dailyFiles"],
        "daily_mb": int(lim["dailyGb"] * 1024),
        "tools": featured,
        "delivery_expiry_days": lim["expiryDays"],
        "delivery_download_limits": lim["downloadLimits"],
        "delivery_default_expiry": lim["defaultDays"],
        "delivery_default_downloads": lim["defaultDownloads"],
        "delivery_json": _delivery_projection(ctx),
        "limits_json": json.dumps(lim),
        # 格式族单源 (governance.formats): composer 上传区族名文案词表,
        # 与发布页 window.__FORMAT_FAMILIES__ 同名同形。
        "formats_json": json.dumps(format_manifest(), ensure_ascii=False),
        "tools_json": json.dumps(picker, ensure_ascii=False),
        "file_size_icon": icon_svg("file_size"),
        "upload_count_icon": icon_svg("upload_count"),
        "bandwidth_icon": icon_svg("bandwidth"),
        "expiry_icon": icon_svg("expiry"),
        "download_count_icon": icon_svg("download_count"),
        "upload_icon": icon_svg("upload"),
    })
    return templates.TemplateResponse(request, "home.html", ctx)


# ── Auth pages ──

@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", next: str = "/"):
    from app.main import templates
    from app.routes.auth_route import _safe_next
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "error": error,
                "next": _safe_next(next)})
    return templates.TemplateResponse(request, "auth/login.html", ctx)


@router.get("/auth/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = "", next: str = "/"):
    from app.main import templates
    from app.routes.auth_route import _safe_next
    ctx = _ctx_base(request)
    ctx.update({"shell_class": "", "share_url": "", "error": error,
                "next": _safe_next(next)})
    return templates.TemplateResponse(request, "auth/register.html", ctx)


# ── Account pages: profile split into focused pages (/auth/profile removed) ──

def _account_ctx(request: Request, page: str, **extra) -> dict:
    """base=None 时复用调用方已取的 ctx_base (跳过重复 projection 读)。"""
    from app.main import templates  # noqa: F401
    from economy.billing import spendable_balance as get_balance
    ctx = extra.pop("_base", None) or _ctx_base(request)
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


def _fmt_bytes(n: int) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            v = n / size
            return (f"{v:.0f}" if v == int(v) else f"{v:.1f}") + " " + unit
    return f"{n} B"


def _tier_benefits(t) -> dict:
    b = t.benefits
    return {"files_24h": int(b.ingress_files_24h),
            "bytes_24h": _fmt_bytes(int(b.ingress_bytes_24h)),
            "expiry_days": max(b.expiry_days) if b.expiry_days else 0,
            "downloads": max(b.download_limits) if b.download_limits else 0}


def _shop_offers(user: dict | None, membership: dict | None) -> dict:
    """What the shop sells, projected for one viewer (anonymous allowed).

    packages: active one-off credit packs, listed only when credit checkout
    is configured. plans: active paid tiers that are sellable (Tier.sellable
    is the single authority), each with the viewer's action — subscribe /
    current / upgrade / downgrade / pending — decided here, never in the
    template."""
    import economy.creem as _creem
    from repo.orders import active_packages, all_plans
    from repo.orders import pending_downgrade as pending_row
    from governance import plan_catalog
    topup_ready = _creem.creem_enabled()
    with db() as con:
        pkgs = [dict(r) for r in active_packages(con)] \
            if topup_ready and _creem.credits_ready() else []
        tiers = [t for t in plan_catalog.catalog()
                 if t.active and not t.is_free]
        plan_ids = {t.id for t in tiers}
        rows_by_id = {r["id"]: dict(r) for r in all_plans(con)
                      if r["id"] in plan_ids}
        pd = pending_row(con, user["id"]) if user else None
    pending = None
    if pd is not None:
        tgt = plan_catalog.by_id(pd["target_plan_id"])
        if tgt is not None and tgt.id in plan_ids:
            pending = {"code": tgt.code, "name": tgt.name,
                       "effective_at": pd["effective_at"]}
    my = plan_catalog.by_code(membership["code"]) \
        if membership and membership.get("member") else None
    plans = []
    for t in tiers:
        if t.id not in rows_by_id or not (topup_ready and t.sellable):
            continue
        if not my:
            action = "subscribe"
        elif t.id == my.id:
            action = "current"
        elif plan_catalog.direction(t, my) > 0:
            action = "upgrade"
        elif pending and pending["code"] == t.code:
            action = "pending"  # 已排期切到该档 — 不再可点
        else:
            action = "downgrade"
        plans.append({
            "id": t.id, "code": t.code, "name": t.name,
            "monthly_credits": t.monthly_credits,
            "period_days": t.period_days,
            "price_cents": t.price_cents,
            "currency": t.currency,
            "benefits": _tier_benefits(t),
            "action": action,
        })
    return {"packages": pkgs, "plans": plans, "pending_downgrade": pending,
            "free": _tier_benefits(plan_catalog.free_tier())}


@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    """Public shop: credit packs, membership plans, refund policy.
    Visible signed out; buying opens the login modal first."""
    from app.main import templates
    from economy.daily import daily_amount as _daily_amount
    ctx = _ctx_base_impl(request, "pricing")
    user = request.state.user
    membership = (ctx.get("access") or {}).get("membership")
    with db() as con:
        daily = _daily_amount(con)
    ctx.update({"shell_class": "", "seo_robots": "index, follow", "daily_amount": daily,
                "max_file_mb": _limits_projection(ctx)["maxFileMb"],
                **_shop_offers(user, membership)})
    return templates.TemplateResponse(request, "pricing.html", ctx)


@router.get("/account/credits", response_class=HTMLResponse)
async def account_credits(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from economy.billing import spendable_balance as get_balance, history_paged
    pg = page_number(request.query_params.get("page", "1"))
    size = 20
    rows, total = history_paged(user["id"], page=pg, size=size)
    rows = [dict(r) for r in rows]
    # 41-12 note column: machine notes (raw tool ids, plan cycle stamps,
    # refund order refs) never render. Tool identity comes ONLY from the
    # ledger relation (consume → tool_call, refund → its consume row) via
    # the batched repo query — note is audit text and is never parsed.
    # Tool-bound rows get a localized "Tool: <resolved name>" description;
    # rows without a resolvable relation show no tool description. Human
    # notes (operator/manual) keep rendering verbatim.
    from repo import ledger as _ledger_repo
    from governance.tool_params import resolve_label as _resolve_label
    from economy.daily import daily_amount as _daily_amount, granted_today
    _lang = getattr(request.state, "lang", "en")
    with db() as con:
        _tool_refs = _ledger_repo.history_tool_refs(con, [r["id"] for r in rows])
        daily_amount = _daily_amount(con)

    def _ref_label(r: dict) -> str | None:
        ref = _tool_refs.get(int(r["id"]))
        if not ref:
            return None
        try:
            label = json.loads(ref["label_json"]) if ref["label_json"] else None
        except ValueError:
            label = None
        return _resolve_label(label, _lang) if label else ref["tool_key"]

    _MACHINE_NOTE_KINDS = ("grant_plan", "grant_plan_upgrade",
                           "grant_package", "revoke")
    for r in rows:
        note = r.get("note") or ""
        if r["kind"] in ("consume", "refund"):
            name = _ref_label(r)
            tpl = server_text(_lang).get("ledger_note_tool")
            r["note_display"] = (
                (tpl or "Tool: {name}").replace("{name}", name) if name else None)
        elif r["kind"] in _MACHINE_NOTE_KINDS:
            r["note_display"] = None
        elif note and not note.startswith(("activity:", "grant_", "payout:")):
            r["note_display"] = note
        else:
            r["note_display"] = None
    _base = _ctx_base(request)  # access projection 单次读
    _membership = (_base.get("access") or {}).get("membership")
    shop = _shop_offers(user, _membership)
    ctx = _account_ctx(request, "credits", _base=_base,
                       balance=get_balance(user["id"]),
                       granted_today=granted_today(user["id"]) if daily_amount > 0 else True,
                       daily_amount=daily_amount,
                       pending_downgrade=shop["pending_downgrade"],
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
    from economy.daily import grant_daily, daily_amount
    import logging as _logging
    with db() as con:
        amount = daily_amount(con)
    if amount <= 0:
        return RedirectResponse("/account/credits", 303)
    try:
        grant_daily(user["id"])
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
    from governance.commission import commission_rate
    with db() as con:
        rate = commission_rate(con)
    ctx = _account_ctx(request, "earnings", commission_rate=rate)
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


@router.get("/account/tools/publish", response_class=HTMLResponse)
async def account_tools_publish(request: Request):
    """Standalone publish page (create + edit via ?id={tool_id})."""
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    from governance.user_tools import _owner_rows, _public_item
    from governance.formats import format_manifest
    tool_id = request.query_params.get("id", "").strip()
    tool = None
    if tool_id:
        for r in _owner_rows(user["id"]):
            if r["tool_id"] == tool_id:
                tool = _public_item(r)
                break
        if tool is None:
            return HTMLResponse("", status_code=404)
    # 4C-2a: format manifest 服务端单点派生 (governance.formats),
    # publish 页唯一格式事实源 — 前端不再维护词表镜像。
    from governance.commission import commission_rate
    with db() as con:
        rate = commission_rate(con)
    ctx = _account_ctx(request, "tools", tool=tool,
                       tool_json=json.dumps(tool, ensure_ascii=False) if tool else "null",
                       format_manifest_json=json.dumps(format_manifest(), ensure_ascii=False),
                       commission_rate=rate)
    ctx["limits_json"] = _limits_json(ctx)
    ctx["delivery_json"] = _delivery_projection(ctx)
    return templates.TemplateResponse(request, "account/publish.html", ctx)


@router.get("/r/{result_id}", response_class=HTMLResponse)
async def result_page(result_id: str, request: Request):
    """Result page (IO-2): the /r/{id} authority. Delivery-owned expiry
    (record expireAt — every result shape carries one) gates access;
    expired or unknown ids get the 404 page. Server-rendered readable
    without JS: ordered items (text/json shown, files linked), header
    facts (tool, generated time, expiry, remaining downloads)."""
    from app.main import templates
    from repo.ledger import call_by_result_public_id
    from utils import format_bytes, format_ts, now, remaining_downloads
    with db() as con:
        row = call_by_result_public_id(con, result_id)
    if row is None:
        ctx = _ctx_base(request)
        ctx["shell_class"] = "file-shell"
        return templates.TemplateResponse(request, "missing.html", ctx,
                                          status_code=404)
    import json as _json
    try:
        record = _json.loads(row["result_json"])
    except Exception:
        record = {"items": []}
    expire_at = record.get("expires_at")
    if not expire_at or int(expire_at) <= now():
        # Expired result: same 404 surface as an unknown id (no
        # existence oracle for dead results).
        ctx = _ctx_base(request)
        ctx["shell_class"] = "file-shell"
        return templates.TemplateResponse(request, "missing.html", ctx,
                                          status_code=404)
    # Per-file live download counters (storage authority) for the header
    # fact + file rows.
    from repo.uploads import by_public_id
    from storage import info as storage_info
    file_pids = [it.get("public_id") for it in (record.get("items") or [])
                 if it.get("kind") == "file" and it.get("public_id")]
    uploads_by_pid = {}
    if file_pids:
        with db() as con:
            for pid in file_pids:
                u = by_public_id(con, pid)
                if u is not None:
                    uploads_by_pid[pid] = u
    items = []
    file_count = 0
    for it in (record.get("items") or []):
        d = dict(it)
        if d.get("kind") == "json":
            d["rendered"] = _json.dumps(d.get("data"), indent=2,
                                        ensure_ascii=False)
        elif d.get("kind") == "file":
            file_count += 1
            ct = d.get("content_type") or ""
            d["is_image"] = (ct.startswith("image/")
                             and ct.lower() != "image/svg+xml")
            d["is_pdf"] = ct.lower() == "application/pdf"
            d["size_text"] = format_bytes(int(d.get("size") or 0))
            d["glyph"] = file_glyph(ct, d.get("filename") or "")
            # Remaining downloads live in ONE place: the file row,
            # computed by the single calculator.
            d["remaining"] = None
            u = uploads_by_pid.get(d.get("public_id"))
            if u is None:
                # no mapping row → the file was deleted; dead row, no
                # download control (presentation only, no new fields)
                d["deleted"] = True
            else:
                live = await asyncio.to_thread(storage_info, u["gokapi_id"]) \
                    if u["gokapi_id"] else None
                d["remaining"] = remaining_downloads(
                    int(u["allowed_downloads"] or 0), live)
        items.append(d)
    # Agents asking for JSON get the result facts, not the human page.
    from app.web_ctx import wants_json
    from utils import public_result_url
    if wants_json(request):
        out = []
        for d in items:
            if d.get("kind") == "file":
                if d.get("deleted"):
                    out.append({"kind": "file", "filename": d.get("filename"),
                                "deleted": True})
                    continue
                out.append({"kind": "file", "public_id": d.get("public_id"),
                            "filename": d.get("filename"),
                            "content_type": d.get("content_type"),
                            "size": int(d.get("size") or 0),
                            "downloads_remaining": d.get("remaining"),
                            "download_url": d.get("download_url"),
                            "page_url": d.get("page_url")})
            elif d.get("kind") == "text":
                out.append({"kind": "text", "text": d.get("text")})
            elif d.get("kind") == "json":
                out.append({"kind": "json", "data": d.get("data")})
        return JSONResponse({"result_url": public_result_url(result_id),
                             "tool": row["tool_id"],
                             "expires_at": int(expire_at), "items": out},
                            headers={"Vary": "Accept"})
    # Tool display name: the call's tool row label (resolver chain).
    ctx = _ctx_base(request)
    tool_name = row["tool_id"]
    with db() as con:
        trow = con.execute(
            "SELECT label_json FROM platform_tools WHERE tool_key = %s",
            (row["tool_id"],)).fetchone()
    if trow is not None:
        from governance.tool_params import resolve_label
        tool_name = resolve_label(_json.loads(trow["label_json"]),
                                  ctx["lang"]) or tool_name
    # human type from the format-family vocabulary (no raw MIME) —
    # resolved once ctx texts exist
    from governance.formats import sniff_ext_to_families
    for d in items:
        if d.get("kind") != "file":
            continue
        _fn = d.get("filename") or ""
        _fams = sniff_ext_to_families(
            "." + _fn.rsplit(".", 1)[-1].lower() if "." in _fn else "")
        _fk = ("pdf" if d.get("is_pdf") else sorted(_fams)[0]) if _fams else "document"
        d["type_text"] = (ctx["texts"].get("fmt_family_" + _fk)
                          or ctx["texts"].get("out_kind_text", ""))
    from utils import public_result_url
    ctx["shell_class"] = "file-shell"
    # Share card (noindex page, but link previews need a card): title =
    # tool + what came back (files / texts / mixed); description = the
    # delivery facts (expiry + remaining downloads). Same approach as /f.
    texts = ctx["texts"]
    text_count = sum(1 for d in items if d.get("kind") in ("text", "json"))
    # Page/card title = what came back: one file → its name; several →
    # "N files"; text-only → the tool. The tool is secondary context.
    _first_file = next((d for d in items if d.get("kind") == "file"
                        and not d.get("deleted")), None)
    if file_count == 1 and _first_file:
        r_title = _first_file.get("filename") or tool_name
    elif file_count > 1:
        r_title = texts["r_n_files"].format(n=file_count)
    else:
        r_title = tool_name or texts["r_page_title"]
    og_title = (f"{r_title} · {tool_name}"
                if tool_name and r_title != tool_name else r_title)
    og_desc = f"{texts['r_expires']}: {format_ts(int(expire_at))}"
    min_remaining = next(iter(sorted(
        [d["remaining"] for d in items
         if d.get("kind") == "file" and not d.get("deleted")
         and d.get("remaining") is not None])), None)
    if min_remaining is not None:
        og_desc += f" · {texts['r_downloads_left']}: {min_remaining}"
    # Share card image: the first live image result itself (inline
    # preview is the non-counting channel), else the site card.
    _first_img = next((d for d in items if d.get("kind") == "file"
                       and d.get("is_image") and not d.get("deleted")), None)
    _viewer = request.state.user
    ctx.update({
        "og_image": (f"{BASE_URL}/f/{_first_img['public_id']}/inline"
                     if _first_img else SOCIAL_IMAGE_URL),
        # no image in the result → small card: the result facts lead,
        # not the site banner
        "og_card": "summary_large_image" if _first_img else "summary",
        "r_title": r_title,
        "is_owner": bool(_viewer and _viewer.get("id") == row["user_id"]),
        "result_id": result_id,
        "tool_name": tool_name,
        "generated_text": format_ts(row["finished_at"]),
        "expire_text": format_ts(int(expire_at)),
        "items": items,
        "file_count": file_count,
        "first_file": _first_file,
        "min_remaining": min_remaining,
        "result_url": public_result_url(result_id),
        "og_title": og_title,
        "og_desc": og_desc,
        "file_size_icon": icon_svg("file_size"),
        "expiry_icon": icon_svg("expiry"),
        "download_count_icon": icon_svg("download_count"),
    })
    resp = templates.TemplateResponse(request, "result.html", ctx)
    resp.headers["Vary"] = "Accept"
    return resp


@router.get("/account/favorites", response_class=HTMLResponse)
async def account_favorites(request: Request):
    """我的收藏: the user's pinned tools (same set MCP tools/list carries
    besides first-party and own tools)."""
    from app.main import templates
    from governance.catalog import list_catalog, mcp_names
    from governance.favorites import favorite_keys
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login?next=/account/favorites"})
    catalog = list_catalog(user["id"])
    by_id = {t["id"]: t for t in catalog}
    names = mcp_names()
    favs = []
    for k in favorite_keys(user["id"], catalog):
        t = dict(by_id[k])
        t["mcp_name"] = names.get(k, k.replace("/", "__"))
        favs.append(t)
    ctx = _account_ctx(request, "favorites", favorites=favs)
    return templates.TemplateResponse(request, "account/favorites.html", ctx)


@router.get("/account/keys", response_class=HTMLResponse)
async def account_keys(request: Request):
    from app.main import templates
    user = request.state.user
    if not user:
        return HTMLResponse("", status_code=302, headers={"Location": "/auth/login"})
    ctx = _account_ctx(request, "keys")
    return templates.TemplateResponse(request, "account/keys.html", ctx)


# /auth/profile does not exist (single-page account model).
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
    current_password = form.get("current_password") or ""

    try:
        # bio / payout_channel: the field is ALWAYS submitted on this
        # form — "" is an explicit clear, not "leave unchanged"
        update_profile(user["id"], display_name=display_name or None,
                       email=email or None, bio=bio,
                       payout_channel=payout_channel,
                       current_password=current_password or None)
    except Exception as exc:
        if isinstance(exc, ValueError):
            code = str(exc)
        else:
            logger.exception("profile update failed uid=%s", user["id"])
            code = ""
        u = public_user(user)
        ctx = _account_ctx(request, "profile", u=u,
                           user_has_password=u.get("hasPassword", False),
                           x_bound=bool(u.get("xUsername")),
                           error=error_text(request, code))
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
        return _err(error_text(request, "err_password_empty"))

    try:
        if has_password(user["id"]):
            if not old_password:
                return _err(error_text(request, "err_current_password_required"))
            change_password(user["id"], old_password, new_password)
        else:
            set_password(user["id"], new_password)
    except ValueError as exc:
        return _err(error_text(request, str(exc)))
    except Exception:
        logger.exception("password update failed uid=%s", user["id"])
        return _err(error_text(request, ""))

    return RedirectResponse("/account/security", 302)
