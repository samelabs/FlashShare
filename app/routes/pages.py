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

from config import APP_NAME, BASE_URL, SOCIAL_IMAGE_URL, MAX_UPLOAD_BYTES
from i18n import LOCALE_TAGS, server_text
from icons import _ICONS, icon_svg, icon_path
from tools import load_all
from governance.catalog import list_catalog
from governance.catalog import seed_from_registry
from governance.tool_params import resolve_label

MAX_UPLOAD_MB = MAX_UPLOAD_BYTES // (1024 * 1024)
from database import db


from app.web_ctx import fmt_ts as _fmt_ts  # 单点 (治理4, UTC)

load_all()  # auto-discover every tools_*.py
from identity.auth import public_user
from utils import (
    brand_icon_svg, upload_icon_svg, file_size_icon_svg, upload_count_icon_svg,
    bandwidth_icon_svg, expiry_icon_svg, download_count_icon_svg,
    close_icon_svg, history_icon_svg,
)

router = APIRouter()



from app.web_ctx import _STATIC  # 静态目录单点 (web_ctx 持有)
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
    ctx.update({"shell_class": "", "share_url": "", "doc_title": ctx["texts"].get("terms_title", "Terms of Service"), "doc_body": _load_doc("terms.html", lang) + _support_note()})
    return templates.TemplateResponse(request, "doc.html", ctx)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    from app.main import templates
    ctx = _ctx_base(request)
    lang = ctx["lang"]
    ctx.update({"shell_class": "", "share_url": "", "doc_title": ctx["texts"].get("privacy_title", "Privacy Policy"), "doc_body": _load_doc("privacy.html", lang) + _support_note()})
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
    from repo.ledger import ok_calls_by_tools
    from database import db

    raw_page = request.query_params.get("page", "1")
    try:
        page = max(int(raw_page), 1)
    except ValueError:
        page = 1
    q = (request.query_params.get("q") or "").strip()

    page_data = market_page_data(q=q or None, page=page)
    tools = page_data["items"]
    with db() as con:
        stats = ok_calls_by_tools(con, [t["id"] for t in tools])
    for t in tools:
        t["stats"] = {"calls": stats.get(t["id"], 0)}

    ctx = _ctx_base(request)
    ctx.update({
        "shell_class": "",
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
    from repo.users import id_by_username
    from governance.catalog import owner_public_tools
    from database import db
    from repo.ledger import ok_calls_by_owner

    with db() as con:
        urow = id_by_username(con, handle)
    if urow is None:
        return templates.TemplateResponse(request, "missing.html",
                                          _ctx_base(request), status_code=404)
    owner_id = urow["id"]
    tools = owner_public_tools(owner_id)
    for t in tools:
        t["stats"] = _public_stats(t["id"])
    with db() as con:
        total_calls = ok_calls_by_owner(con, owner_id, [t["id"] for t in tools])
    ctx = _ctx_base(request)
    user = request.state.user
    ctx.update({
        "shell_class": "",
        "owner_handle": handle,
        "owner_tools": tools,
        "owner_total_calls": total_calls,
        "is_self": bool(user and user.get("id") == owner_id),
    })
    return templates.TemplateResponse(request, "user_page.html", ctx)


def _tool_page_ctx(request: Request, owner: str | None, tool_id: str, mcp: bool):
    """Shared builder for tool page + MCP page. resolve() 是唯一权限判断
    (public/unlisted → everyone when key known; private → owner only)。

    owner=None → first-party bare-id tool (官方工具, 无作者命名空间)."""
    from governance.catalog import resolve, catalog_item
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
    ctx = _ctx_base(request)
    from icons import icon_svg
    tool_public["icon_svg"] = icon_svg(tool_public.get("icon") or "wand")
    ctx.update({
        "shell_class": "",
        "tool": tool_public,
        # LocalizedText 协议单链解析; tool_id 兜底属调用方 (协议注释见 app/main.py lt filter)
        "tool_name": resolve_label(tool_public.get("label") or {}, ctx["lang"]) or tool_id,
        "handle": _owner_handle(tool.get("owner_id")) if tool.get("owner_id") else None,
        "stats": _public_stats(tool_key),
        "mcp_name": tool_key.replace("/", "__"),
        "is_mcp": mcp,
        "tools_json": json.dumps([tool_public], ensure_ascii=False),
        "limits_json": ctx.get("limits_json") or json.dumps({"max_upload_mb": MAX_UPLOAD_MB}, ensure_ascii=False),
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

    # Delivery/ingress entitlement projection — consume ctx_base's single
    # access projection read (never a second project()); anonymous falls
    # back to the canonical pure Free capability (resolve_from_membership
    # (None)), never hand-written Free values. Web boundary converts the
    # internal tuples to lists once, here.
    if ctx.get("access"):
        ent = ctx["access"]["entitlements"]
    else:
        from governance.entitlements import resolve_from_membership
        _raw = resolve_from_membership(None)
        ent = {"plan_code": _raw["plan_code"],
               "ingress_files_24h": _raw["ingress_files_24h"],
               "ingress_bytes_24h": _raw["ingress_bytes_24h"],
               "expiry_days": list(_raw["expiry_days"]),
               "download_limits": list(_raw["download_limits"])}
    limits = {
        "maxMb": max_mb,
        "dailyUploads": ent["ingress_files_24h"],
        "dailyMb": ent["ingress_bytes_24h"] // 1024 // 1024,
        "maxDays": max(ent["expiry_days"]),
        "maxDownloads": max(ent["download_limits"]),
        "expiryDays": list(ent["expiry_days"]),
        "downloadLimits": list(ent["download_limits"]),
    }
    # UI default preference (not entitlement policy): keep 1/50 when
    # allowed, else the first allowed value.
    def_exp = 1 if 1 in ent["expiry_days"] else ent["expiry_days"][0]
    def_dl = 50 if 50 in ent["download_limits"] else ent["download_limits"][0]

    # Quota visibility: rolling-24h ingress facts from the SAME canonical
    # projection (ctx_base access read — no second project(), no local
    # quota math; remaining is presentation-level subtraction only).
    # Projection/read failure never masquerades as 0-used: ctx_base already
    # propagates the fault, so this block simply never renders then.
    quota = None
    access = ctx.get("access")
    if access is not None:
        _ent = access["entitlements"]
        _usage = access["usage"]
        files_used = int(_usage["ingress_files_24h"])
        files_limit = int(_ent["ingress_files_24h"])
        bytes_used = int(_usage["ingress_bytes_24h"])
        bytes_limit = int(_ent["ingress_bytes_24h"])
        quota = {
            "plan_code": _ent["plan_code"],
            "files_used": files_used,
            "files_limit": files_limit,
            "files_remaining": max(files_limit - files_used, 0),
            "bytes_used": bytes_used,
            "bytes_limit": bytes_limit,
            "bytes_remaining": max(bytes_limit - bytes_used, 0),
            "files_near": files_limit and files_used * 100 >= files_limit * 80,
            "bytes_near": bytes_limit and bytes_used * 100 >= bytes_limit * 80,
            "files_reached": files_used >= files_limit,
            "bytes_reached": bytes_used >= bytes_limit,
        }

    ctx.update({
        "shell_class": "",
        "share_url": home_share_url,
        "max_mb": max_mb,
        "quota": quota,
        "daily_uploads": ent["ingress_files_24h"],
        "daily_mb": ent["ingress_bytes_24h"] // 1024 // 1024,
        "tools": tools,
        "delivery_expiry_days": list(ent["expiry_days"]),
        "delivery_download_limits": list(ent["download_limits"]),
        "delivery_default_expiry": def_exp,
        "delivery_default_downloads": def_dl,
        "limits_json": json.dumps(limits),
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
    from economy.daily import DAILY_AMOUNT, granted_today
    daily_amount = DAILY_AMOUNT
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
    from economy import creem as _creem_mod
    credits_ready = _creem_mod.credits_ready()
    # subscription tier(s) purchasable catalog — a different concept from
    # the viewer's membership fact: active_plans() lists what can be
    # bought, while my_plan comes from _account_ctx → _ctx_base's single
    # access projection (access["membership"] alias), never a second
    # plan_of() read here.
    from repo.orders import canonical_plans
    from governance.entitlements import entitlement_profile_exists
    with db() as con:
        # sellable = canonical paid tiers (plus/pro) × entitlement profile 单点:
        # DB 历史 plan rows 不渲染为会员商品。
        plans = [dict(r) for r in canonical_plans(con)
                 if entitlement_profile_exists(r["code"])]
    ctx = _account_ctx(request, "credits",
                       balance=get_balance(user["id"]),
                       granted_today=granted_today(user["id"]) if daily_amount > 0 else True,
                       daily_amount=daily_amount,
                       packages=pkgs, topup_ready=topup_ready,
                       credits_ready=credits_ready,
                       plans=plans,
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
    from economy.daily import grant_daily, DAILY_AMOUNT
    import logging as _logging
    if DAILY_AMOUNT <= 0:
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
    ctx = _account_ctx(request, "tools", tool=tool,
                       tool_json=json.dumps(tool, ensure_ascii=False) if tool else "null",
                       format_manifest_json=json.dumps(format_manifest(), ensure_ascii=False))
    return templates.TemplateResponse(request, "account/publish.html", ctx)


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
