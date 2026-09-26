"""Page routes: home, file detail, auth pages — all via Jinja2 templates."""

import sys
import json
import logging
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
from utils import file_glyph
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


router = APIRouter()



from app.web_ctx import _STATIC  # 静态目录单点 (web_ctx 持有)
from app.web_ctx import (  # noqa: F401 (模板/路由沿用原名)
    ctx_base as _ctx_base_impl, _CSS_VERSION, _JS_VERSION, _I18N_VERSION, JS,
    error_text,
)

logger = logging.getLogger("flashshare")


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
    """Share links (/f/) stay FETCHABLE for agents that honor robots
    (indexing is refused per-response via X-Robots-Tag, not here); /mcp
    is likewise open — HTTP POST JSON-RPC, no installation required.
    /dh/ is the Nginx→Gokapi hotlink path (still Disallow); the old /d/
    rule referenced a retired path and was removed."""
    body = (f"User-agent: *\nDisallow: /api/\nDisallow: /auth/\n"
            f"Disallow: /dh/\nAllow: /\n"
            f"Sitemap: {BASE_URL}/sitemap.xml\n")
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
    """Agent support surface: the MCP server."""
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


def _delivery_projection(ctx: dict) -> str:
    """Delivery options for the shared input component (tool pages).
    Same entitlement source as the home surface — ctx_base's single
    access projection; anonymous falls back to the Free capability
    (resolve_from_membership(None)), never hand-written Free values."""
    if ctx.get("access"):
        ent = ctx["access"]["entitlements"]
    else:
        from governance.entitlements import resolve_from_membership
        _raw = resolve_from_membership(None)
        ent = {"expiry_days": list(_raw["expiry_days"]),
               "download_limits": list(_raw["download_limits"])}
    days = list(ent["expiry_days"])
    dls = list(ent["download_limits"])
    return json.dumps({
        "days": days, "downloads": dls,
        "defaultDays": 1 if 1 in days else days[0],
        "defaultDownloads": 50 if 50 in dls else dls[0],
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
    ctx.update({
        "shell_class": "",
        "tool": tool_public,
        "delivery_json": _delivery_projection(ctx),
        # LocalizedText 协议单链解析; tool_id 兜底属调用方 (协议注释见 app/main.py lt filter)
        "tool_name": resolve_label(tool_public.get("label") or {}, ctx["lang"]) or tool_id,
        "handle": _owner_handle(tool.get("owner_id")) if tool.get("owner_id") else None,
        "stats": _public_stats(tool_key),
        "mcp_name": mcp_name(tool_key),
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

    ctx.update({
        "shell_class": "",
        "share_url": home_share_url,
        "max_mb": max_mb,
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
    _lang = getattr(request.state, "lang", "en")
    with db() as con:
        _tool_refs = _ledger_repo.history_tool_refs(con, [r["id"] for r in rows])

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
    # packages (one-off top-up packs) — active rows only, data-driven
    from repo.orders import active_packages
    with db() as con:
        pkgs = [dict(r) for r in active_packages(con)]
    import economy.creem as _creem
    topup_ready = _creem.creem_enabled()
    from economy import creem as _creem_mod
    credits_ready = _creem_mod.credits_ready()
    # membership 商品投影 — 全部由 plans 行驱动, 服务端单点算 action,
    # 模板只渲染不判断 (零档名字符串)。membership fact 复用 _ctx_base
    # 的同一 access projection 读 (ctx 尚未构建, 此处先取 base 再传 extra)。
    from repo.orders import pending_downgrade as pending_row
    from repo.orders import all_plans as orders_repo_all_paid
    from governance import plan_catalog
    _base = _ctx_base(request)  # access projection 单次读
    _membership = (_base.get("access") or {}).get("membership")
    with db() as con:
        pd = pending_row(con, user["id"])
        # 展示目录 = active paid 行 (未配齐的档显示 TBD 占位, 配好后即变可售);
        # 可售性单点 = Tier.sellable (价>0 + product + benefits 配齐)
        tiers = [t for t in plan_catalog.catalog()
                 if t.active and not t.is_free]
        plan_ids = {t.id for t in tiers}
        # 行 id → 行 dict (active paid 行全集, 直读行)
        rows_by_id = {r["id"]: dict(r) for r in orders_repo_all_paid(con)
                      if r["id"] in plan_ids}
    # scheduled downgrade fact — 与订阅边界同一读模型; 页面据此渲染
    # 排期态而非可点的降级按钮。
    pending = None
    if pd is not None:
        tgt = plan_catalog.by_id(pd["target_plan_id"])
        if tgt is not None and tgt.id in plan_ids:
            pending = {"code": tgt.code, "name": tgt.name,
                       "effective_at": pd["effective_at"]}
    # 观看者当前档 (Tier 对象; 非会员为 None)
    my = plan_catalog.by_code(_membership["code"]) \
        if _membership and _membership.get("member") else None
    plans = []
    for t in tiers:
        row = rows_by_id.get(t.id)
        if row is None:
            continue
        # action 单点: subscribe / upgrade / downgrade / current / pending / none
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
            "ready": bool(topup_ready and t.sellable),  # 可售性单点 = Tier.sellable
            "action": action,
        })
    ctx = _account_ctx(request, "credits", _base=_base,
                       balance=get_balance(user["id"]),
                       granted_today=granted_today(user["id"]) if daily_amount > 0 else True,
                       daily_amount=daily_amount,
                       packages=pkgs, topup_ready=topup_ready,
                       credits_ready=credits_ready,
                       plans=plans, pending_downgrade=pending,
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


@router.get("/r/{result_id}", response_class=HTMLResponse)
async def result_page(result_id: str, request: Request):
    """Result page (IO-2): the /r/{id} authority. Delivery-owned expiry
    (record expireAt — every result shape carries one) gates access;
    expired or unknown ids get the 404 page. Server-rendered readable
    without JS: ordered items (text/json shown, files linked), header
    facts (tool, generated time, expiry, remaining downloads)."""
    from app.main import templates
    from repo.ledger import call_by_result_public_id
    from utils import format_bytes, format_ts, now
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
    expire_at = record.get("expireAt")
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
    file_pids = [it.get("publicId") for it in (record.get("items") or [])
                 if it.get("kind") == "file" and it.get("publicId")]
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
            ct = d.get("contentType") or ""
            d["is_image"] = (ct.startswith("image/")
                             and ct.lower() != "image/svg+xml")
            d["is_pdf"] = ct.lower() == "application/pdf"
            d["size_text"] = format_bytes(int(d.get("size") or 0))
            d["glyph"] = file_glyph(ct, d.get("filename") or "")
            # Remaining downloads live in ONE place: the file row.
            d["remaining"] = None
            u = uploads_by_pid.get(d.get("publicId"))
            if u is not None:
                live = storage_info(u["gokapi_id"]) if u["gokapi_id"] else None
                total = int(u["allowed_downloads"] or 0)
                count = (live or {}).get("DownloadCount")
                remaining = (live or {}).get("DownloadsRemaining")
                if isinstance(remaining, int):
                    d["remaining"] = max(0, remaining)
                elif isinstance(count, int):
                    d["remaining"] = max(0, total - count)
        items.append(d)
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
    from utils import public_result_url
    ctx["shell_class"] = "file-shell"
    ctx.update({
        "result_id": result_id,
        "tool_name": tool_name,
        "generated_text": format_ts(row["finished_at"]),
        "expire_text": format_ts(int(expire_at)),
        "items": items,
        "file_count": file_count,
        "result_url": public_result_url(result_id),
        "file_size_icon": icon_svg("file_size"),
        "expiry_icon": icon_svg("expiry"),
        "download_count_icon": icon_svg("download_count"),
    })
    return templates.TemplateResponse(request, "result.html", ctx)


@router.get("/r/{result_id}/qr.svg")
async def result_qr(result_id: str):
    """QR for the result page link (same SVG authority as /f/{id}/qr.svg;
    a QR exists only for live results — expired/unknown → 404)."""
    from repo.ledger import call_by_result_public_id
    from utils import qr_svg, now
    with db() as con:
        row = call_by_result_public_id(con, result_id)
    if row is None:
        return Response(status_code=404)
    try:
        import json as _json
        record = _json.loads(row["result_json"])
        expire_at = record.get("expireAt")
    except Exception:
        expire_at = None
    if not expire_at or int(expire_at) <= now():
        return Response(status_code=404)
    from utils import public_result_url
    return Response(content=qr_svg(public_result_url(result_id)),
                    media_type="image/svg+xml; charset=utf-8")





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
        update_profile(user["id"], display_name=display_name or None, email=email or None, bio=bio or None, payout_channel=payout_channel or None,
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
