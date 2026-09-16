"""/samelabs back-office: access control + shared helpers (batch C4).

Gate model: users.status in ('admin','samelabs') grants back-office
access (status 'admin' kept as a legacy alias; the path is /samelabs
only). Sessions AND Bearer both work — the gate is the user row, not
the channel.

Pages (server-rendered, no SPA):
    /samelabs            overview  — users count, calls, credits flow
    /samelabs/users      user list + search + credit balance
    /samelabs/tools      tool pricing + enable switch
    /samelabs/plans      subscription tiers
    /samelabs/packages   credit packs
    /samelabs/ledger     global ledger stream
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse

from database import db
from utils import now

from app.web_ctx import fmt_ts as _fmt_ts  # 单点 (治理4)

router = APIRouter()

BACKOFFICE_STATUSES = ("admin", "samelabs")

# ── samelabs.css cache-busting (same pattern as pages.py _CSS_VERSION) ──
import hashlib as _hashlib
import re as _re
from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS
from pathlib import Path as _Path

_SL_CSS_VERSION = _hashlib.md5(
    (_Path(__file__).resolve().parent.parent.parent / "static" / "samelabs.css").read_bytes()
).hexdigest()[:8]


def is_operator(user: dict | None) -> bool:
    return bool(user) and user.get("status") in BACKOFFICE_STATUSES


def _gate(request: Request):
    """Return (user, None) for operators, else (None, response)."""
    user = getattr(request.state, "user", None)
    if not is_operator(user):
        return None, RedirectResponse("/auth/login?next=/samelabs", status_code=302)
    return user, None


def _ctx(request: Request, user: dict, **extra) -> dict:
    """Full page context: shared vars + samelabs.css version + operator row."""
    from app.web_ctx import ctx_base as _ctx_base
    ctx = _ctx_base(request)
    ctx["op"] = user
    ctx["fmt_ts"] = _fmt_ts
    ctx["sl_css_version"] = _SL_CSS_VERSION
    ctx.update(extra)
    return ctx




def _fmt_num(v) -> str:
    """Thousands separators for stat/table numbers ('' → 0)."""
    try:
        return f"{int(v or 0):,}"
    except (TypeError, ValueError):
        return str(v)


@router.get("/samelabs")
async def samelabs_overview(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.reports import overview_stats, recent_calls, top_tools
    with db() as con:
        stats = overview_stats(con)
        recent = recent_calls(con, 15)
        top_tools = top_tools(con, 10)
    ctx = _ctx(request, user, stats=stats,
               recent=[dict(r) for r in recent],
               top_tools=[dict(r) for r in top_tools],
               fmt_num=_fmt_num, page="overview")
    return templates.TemplateResponse(request, "samelabs/overview.html", ctx)


@router.get("/samelabs/users")
async def samelabs_users(request: Request, q: str = ""):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.reports import users_overview
    with db() as con:
        rows = users_overview(con, q or None)
    ctx = _ctx(request, user, users=[dict(r) for r in rows],
               q=q, fmt_num=_fmt_num, page="users")
    return templates.TemplateResponse(request, "samelabs/users.html", ctx)


@router.get("/samelabs/tools")
async def samelabs_tools(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from governance.pricing import list_pricing
    from governance.catalog import list_catalog
    # Registry truth is the data-state catalog (platform_tools + pricing
    # overlay). 'registered' = present in the live catalog; retired rows
    # show as unregistered so history stays interpretable.
    reg = {t["id"]: t for t in list_catalog(None)}
    rows = []
    for p in list_pricing():
        t = reg.get(p["tool_id"])
        rows.append({
            **p,
            "registered": t is not None,
            "label": (t["label"].get("zh") or t["label"].get("en")) if t else "",
            "icon": t["icon"] if t else "",
            "featured": bool(t["featured"]) if t else False,
            "sort_order": int(t["sort_order"]) if t else 0,
        })
    ctx = _ctx(request, user, tools=rows, fmt_num=_fmt_num, page="tools")
    return templates.TemplateResponse(request, "samelabs/tools.html", ctx)


@router.post("/samelabs/tools/{tool_id}/pricing")
async def samelabs_set_pricing(tool_id: str, request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        cost = int(raw.get("cost", 0))
        enabled = bool(raw.get("enabled", True))
        if cost < 0 or cost > 1000000:
            raise ValueError
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from governance.pricing import set_pricing
    row = set_pricing(tool_id, cost, enabled, admin_user_id=user["id"])
    return {"ok": True, "pricing": {k: row[k] for k in ("tool_id", "cost", "enabled")}}


@router.get("/samelabs/plans")
async def samelabs_plans(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.orders import all_plans
    with db() as con:
        rows = [r for r in all_plans(con) if r["code"] != "free"]  # 管理面: 全部 paid 行
        # 订阅计数: current memberships per plan (含 pending 排期目标) —
        # 下架/改配前的事实依据
        counts = {r["plan_id"]: int(r["n"]) for r in con.execute(
            "SELECT plan_id, COUNT(*) n FROM memberships"
            " WHERE status IN ('active','past_due','canceled') GROUP BY plan_id")}
        pend = {r["target_plan_id"]: int(r["n"]) for r in con.execute(
            "SELECT target_plan_id, COUNT(*) n FROM pending_downgrades"
            " GROUP BY target_plan_id")}
    plans = []
    for r in rows:
        d = dict(r)
        d["member_count"] = int(counts.get(r["id"], 0))
        d["pending_count"] = int(pend.get(r["id"], 0))
        plans.append(d)
    ctx = _ctx(request, user, plans=plans, fmt_num=_fmt_num, page="plans")
    return templates.TemplateResponse(request, "samelabs/plans.html", ctx)


_PLAN_INT_FIELDS = ("monthly_credits", "period_days", "price_cents",
                    "sort_order", "ingress_files_24h", "ingress_bytes_24h")


def _parse_plan_fields(raw: dict, *, creating: bool = False) -> dict | None:
    """operator plan 字段解析 + 值域 fail-closed。返回 None = 请求非法。
    权益枚举必须 ⊆ 平台协议 (delivery 单源); 越界值 422 拒绝, 绝不裁剪。"""
    fields: dict = {}
    if creating:
        code = str(raw.get("code", "")).strip().lower()[:32]
        if not code or not _re.fullmatch(r"[a-z][a-z0-9_]*", code) or code == "free":
            return None
        fields["code"] = code
        for k in ("name",):
            v = str(raw.get(k, "")).strip()[:64]
            if not v:
                return None
            fields[k] = v
        for k in _PLAN_INT_FIELDS:
            v = raw.get(k, 0)
            if not isinstance(v, int) or v < 0:
                return None
            fields[k] = v
    else:
        if "name" in raw:
            v = str(raw["name"]).strip()[:64]
            if not v:
                return None
            fields["name"] = v
        for k in _PLAN_INT_FIELDS:
            if k in raw:
                v = raw[k]
                if not isinstance(v, int) or v < 0:
                    return None
                fields[k] = v
    if "creem_product_id" in raw or creating:
        fields["creem_product_id"] = str(raw.get("creem_product_id", "")).strip()[:64] or None
    if "active" in raw or creating:
        if not isinstance(raw.get("active", True), bool):
            return None
        fields["active"] = bool(raw.get("active", True))
    for k, allowed in (("expiry_days", EXPIRY_DAYS),
                       ("download_limits", DOWNLOAD_LIMITS)):
        if k in raw or (creating and raw.get(k) is not None):
            v = raw.get(k)
            if not isinstance(v, list) or not all(isinstance(x, int) for x in v):
                return None
            if any(x not in allowed for x in v):
                return None  # 越界值 fail-closed, 不静默裁剪
            fields[k] = v
    return fields


@router.post("/samelabs/plans")
async def samelabs_create_plan(request: Request):
    """创建新 paid tier 行 (plans 行 = 商品事实源; 新档不硬编码)。"""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        fields = _parse_plan_fields(raw, creating=True)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if fields is None:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.orders import insert_plan
    from database import db as _db
    import psycopg2
    try:
        with _db() as con:
            row = insert_plan(
                con, fields["code"], fields["name"], fields["monthly_credits"],
                fields["price_cents"], now(),
                period_days=fields["period_days"] or 30,
                sort_order=fields["sort_order"],
                creem_product_id=fields["creem_product_id"],
                ingress_files_24h=fields["ingress_files_24h"],
                ingress_bytes_24h=fields["ingress_bytes_24h"],
                expiry_days=fields.get("expiry_days") or (),
                download_limits=fields.get("download_limits") or ())
    except psycopg2.IntegrityError as exc:
        # 唯一索引兜底: code 冲突 / paid rank 冲突 / product 已绑它档
        msg = str(getattr(exc, "diag", None) and exc.diag.message_primary or exc)
        if "plans_paid_sort_order_uk" in msg or "sort_order" in msg:
            return JSONResponse({"ok": False, "error": "sort_order_taken"}, 409)
        if "plans_product_binding_uk" in msg or "creem_product_id" in msg:
            return JSONResponse({"ok": False, "error": "product_already_bound"}, 409)
        return JSONResponse({"ok": False, "error": "code_taken"}, 409)
    from governance.plan_catalog import validate_config
    validate_config()  # refresh + fail-closed 校验
    return {"ok": True, "plan": dict(row)}


@router.patch("/samelabs/plans/{plan_id}")
async def samelabs_update_plan(plan_id: int, request: Request):
    """配置 paid tier 行: 名称/积分/周期/价格/product/权益/上下架/排序
    (code 不可改; free 行是基础档非商品, 不可经此修改)。"""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        fields = _parse_plan_fields(raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if fields is None or not fields:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.orders import update_plan, plan_by_id
    from governance.plan_catalog import is_paid_tier
    with db() as con:
        row = plan_by_id(con, plan_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if not is_paid_tier(row["code"]):
        return JSONResponse({"ok": False, "error": "not_editable_tier"}, 422)
    import psycopg2
    try:
        with db() as con:
            row = update_plan(con, plan_id, fields, now())
    except psycopg2.IntegrityError as exc:
        # 唯一索引兜底 (并发/撞既有行): rank 冲突 / product 已绑它档
        msg = str(getattr(exc, "diag", None) and exc.diag.message_primary or exc)
        if "plans_paid_sort_order_uk" in msg or "sort_order" in msg:
            return JSONResponse({"ok": False, "error": "sort_order_taken"}, 409)
        if "plans_product_binding_uk" in msg or "creem_product_id" in msg:
            return JSONResponse({"ok": False, "error": "product_already_bound"}, 409)
        raise
    # plans 行是 Tier 唯一事实源, 进程内目录缓存必须随运营写入失效
    from governance.plan_catalog import validate_config
    validate_config()  # refresh=True + fail-closed 校验新配置
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True, "plan": dict(row)}


@router.patch("/samelabs/packages/{package_id}")
async def samelabs_update_package(package_id: int, request: Request):
    """配置积分包行: 名称/积分/价格/上下架 (code 不可改)。"""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        fields = {}
        if "name" in raw:
            v = str(raw["name"]).strip()[:64]
            if not v:
                raise ValueError
            fields["name"] = v
        for k in ("credits", "price_cents"):
            if k in raw:
                v = raw[k]
                if not isinstance(v, int) or v < 0:
                    raise ValueError
                fields[k] = v
        if "active" in raw:
            if not isinstance(raw["active"], bool):
                raise ValueError
            fields["active"] = raw["active"]
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if not fields:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.orders import update_package
    with db() as con:
        row = update_package(con, package_id, fields, now())
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True, "package": dict(row)}


@router.patch("/samelabs/tools/{tool_key}/presentation")
async def samelabs_set_tool_presentation(tool_key: str, request: Request):
    """管理后台遴选: 推荐标记 + 排序 (首页/市场呈现序的权威)。"""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        featured = bool(raw.get("featured", False))
        sort_order = int(raw.get("sort_order", 0))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.catalog import set_tool_presentation
    with db() as con:
        row = set_tool_presentation(con, tool_key, featured, sort_order, now())
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True, "tool": dict(row)}


@router.get("/samelabs/packages")
async def samelabs_packages(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.orders import all_packages
    with db() as con:
        rows = all_packages(con)
    ctx = _ctx(request, user, packages=[dict(r) for r in rows], fmt_num=_fmt_num, page="packages")
    return templates.TemplateResponse(request, "samelabs/packages.html", ctx)


@router.post("/samelabs/packages")
async def samelabs_create_package(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        code = str(raw.get("code", "")).strip().lower()[:32]
        name = str(raw.get("name", "")).strip()[:64]
        credits = int(raw.get("credits", 0))
        price = int(raw.get("price", 0))
        if not code or not name or credits <= 0 or price < 0:
            raise ValueError
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    import psycopg2
    from repo.orders import insert_package
    try:
        with db() as con:
            row = insert_package(con, code, name, credits, price, now())
    except psycopg2.IntegrityError:
        return JSONResponse({"ok": False, "error": "code_taken"}, 409)
    return {"ok": True, "package": dict(row)}


@router.get("/samelabs/ledger")
async def samelabs_ledger(request: Request, page: int = 1):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.reports import ledger_page
    page = max(1, min(page, 10000))
    size = 50
    with db() as con:
        rows, total = ledger_page(con, size, (page - 1) * size)
    ctx = _ctx(request, user, entries=[dict(r) for r in rows],
               total=total, pg=page, pages=(total + size - 1) // size,
               fmt_num=_fmt_num, page="ledger")
    return templates.TemplateResponse(request, "samelabs/ledger.html", ctx)


@router.post("/samelabs/users/{user_id}/credits")
async def samelabs_adjust_credits(user_id: int, request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        amount = int(raw.get("amount", 0))
        note = str(raw.get("note", ""))[:200]
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from economy.billing import grant, consume, InsufficientCredits
    # Each manual adjustment is a distinct event — idem must never collide
    # (two ops in the same second would share a now()-second key).
    import secrets as _secrets
    nonce = f"{now()}:{_secrets.token_hex(8)}"
    try:
        if amount > 0:
            row = grant(user_id, amount, "adjust",
                        note=note or "samelabs adjust",
                        idem_key=f"adjust:{user['id']}:{nonce}")
        elif amount < 0:
            row = consume(user_id, -amount, "adjust",
                          note=note or "samelabs adjust",
                          idem_key=f"adjust:{user['id']}:{nonce}")
        else:
            return JSONResponse({"ok": False, "error": "zero_amount"}, 422)
    except InsufficientCredits as exc:
        return JSONResponse({"ok": False, "error": "insufficient",
                             "balance": exc.balance}, 409)
    return {"ok": True, "balance_after": row["balance_after"]}


# ── Withdrawals (operator payout queue) ──

@router.get("/samelabs/withdrawals")
async def samelabs_withdrawals(request: Request):
    """Withdrawal queue: pending first, operator approve/reject."""
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from economy.earnings import list_all_pending
    rows = list_all_pending()
    for r in rows:
        r["created_fmt"] = _fmt_ts(r["created_at"])
    ctx = _ctx(request, user, withdrawals=rows, page="withdrawals")
    return templates.TemplateResponse(request, "samelabs/withdrawals.html", ctx)


@router.post("/samelabs/withdrawals/{wid}/process")
async def samelabs_process_withdrawal(wid: int, request: Request):
    """approve=1 → ledger consume + status approved (one tx); else reject."""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    approve = bool(raw.get("approve"))
    txn_ref = str(raw.get("txn_ref") or "").strip()[:120] or None
    from economy.earnings import process_withdrawal
    try:
        row = process_withdrawal(wid, approve, admin_user_id=user["id"],
                                 txn_ref=txn_ref)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "status": row["status"]}


@router.get("/samelabs/remote")
async def samelabs_remote(request: Request):
    """Remote tool registry: trusted endpoints (owner_id NULL)."""
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from governance.catalog import list_catalog
    # remote rows only, all visibilities (operator view)
    from repo.catalog import all_remote_joined
    with db() as con:
        rows = all_remote_joined(con)
    items = []
    for r in rows:
        decl = {}
        label = r["tool_id"]
        try:
            import json as _json
            decl = _json.loads(r["endpoint_json"] or "{}")
            lab = _json.loads(r["label_json"] or "{}")
            label = lab.get("zh") or lab.get("en") or r["tool_id"]
        except Exception:
            pass
        items.append({
            "tool_key": r["tool_key"], "execution": r["execution"],
            "label": label,
            "icon": r["icon"], "visibility": r["visibility"],
            "enabled": r["enabled"], "owner": r["owner_name"] or "平台",
            "url": decl.get("url", ""), "timeout_s": decl.get("timeout_s", 60),
            "has_auth": bool(decl.get("auth_token")),
        })
    ctx = _ctx(request, user, remote_tools=items, page="remote")
    return templates.TemplateResponse(request, "samelabs/remote.html", ctx)


@router.post("/samelabs/remote/create")
async def samelabs_remote_create(request: Request):
    """Create a trusted remote tool row. tool_key must be a bare id
    (operator namespace = first-party)."""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        tool_id = str(raw.get("tool_id", "")).strip()
        execution = str(raw.get("execution", "http")).strip()
        label = str(raw.get("label", "")).strip() or tool_id
        url = str(raw.get("url", "")).strip()
        timeout_s = int(raw.get("timeout_s", 60))
        auth_header = str(raw.get("auth_header", "")).strip()
        auth_token = str(raw.get("auth_token", "")).strip()
        accept_raw = str(raw.get("accept", "")).strip()
        # 4C-2-close: multi 与 user ToolDeclaration 同语义 —
        # 缺失 → false; 显式值必须是真 JSON boolean, 非 bool → bad_multi
        multi = raw.get("multi", False)
        if not isinstance(multi, bool):
            return JSONResponse({"ok": False, "error": "bad_multi"}, 422)
        cost = int(raw.get("cost", 0))
        output_filename = str(raw.get("output_filename", "")).strip()
        output_content_type = str(raw.get("output_content_type", "")).strip()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]{1,40}$", tool_id):
        return JSONResponse({"ok": False, "error": "bad_tool_id"}, 422)
    # Supply contract: single execution class (http). mcp forwarding
    # removed 2026-08-26 — operator rows follow the same API contract.
    execution = "http"
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "bad_url"}, 422)
    if not (5 <= timeout_s <= 300):
        return JSONResponse({"ok": False, "error": "bad_timeout"}, 422)
    # Raw-bytes delivery needs a whitelisted landing identity: sniff
    # validates the filename extension, so output_filename is REQUIRED
    # with a whitelisted extension when the endpoint may return raw bytes.
    if output_filename:
        import os as _os
        ext = _os.path.splitext(output_filename)[1].lower()
        from config import ALLOWED_EXT
        if ext not in ALLOWED_EXT:
            return JSONResponse({"ok": False, "error": "bad_output_filename"}, 422)
    # 4C-2: 逗号分隔输入同时兼容 canonical ext (.png,.pdf) 与 legacy MIME
    # (image/png); 统一过 extensions_for_accept, 存库只写 canonical ext。
    # operator 受信供给允许跨 family; unknown token → bad_accept。
    accept = None
    if accept_raw:
        tokens = [x.strip() for x in accept_raw.split(",") if x.strip()]
        from governance.formats import extensions_for_accept
        try:
            canon = extensions_for_accept(tokens)
        except (KeyError, TypeError):
            return JSONResponse({"ok": False, "error": "bad_accept"}, 422)
        accept = sorted(canon) if canon else None

    # 阶段 B: operator policy 之上构造 canonical declaration →
    # tool_supply 单点 (与 user 同一 create_tool / binding shape)。
    # HTTP binding composition + auth seal 唯一入口 = build_http_binding
    # (旧 endpoint 组装段已删除, 不再存在第二份 seal)。
    from repo.catalog import tool_key_exists
    from governance.tool_supply import create_tool, build_http_binding, DeclarationError
    try:
        with db() as con:
            # typed duplicate check (unique index would 500 otherwise)
            if tool_key_exists(con, tool_id):
                return JSONResponse({"ok": False, "error": "duplicate_tool"}, 409)
        declaration = {
            "tool_key": tool_id,
            "tool_id": tool_id,
            "owner_id": None,
            "execution": "http",
            "tool_type": "produce",
            "label": {"en": label, "zh": label},
            "desc": None,
            "icon": "wand",
            "accept": accept,
            "multi": multi,
            "params": None,
            "pass_through": False,
            "visibility": "public",
            "enabled": True,
            "binding": build_http_binding({
                "url": url, "timeout_s": timeout_s,
                **({"auth_header": auth_header} if auth_header else {}),
                **({"auth_token": auth_token} if auth_token else {}),
                **({"output_filename": output_filename} if output_filename else {}),
                **({"output_content_type": output_content_type} if output_content_type else {}),
            }),
        }
        try:
            create_tool(declaration)
        except DeclarationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)
    except Exception:
        return JSONResponse({"ok": False, "error": "duplicate_or_db"}, 409)
    # seed pricing row so the metering gate can charge it
    from governance.pricing import seed_pricing
    seed_pricing([tool_id])
    if cost > 0:
        from governance.pricing import set_pricing
        set_pricing(tool_id, cost, True, admin_user_id=user["id"])
    return {"ok": True, "tool_key": tool_id}


@router.post("/samelabs/remote/{tool_key}/delete")
async def samelabs_remote_delete(tool_key: str, request: Request):
    """Remove a remote tool row (platform-owned rows only)."""
    user, resp = _gate(request)
    if resp:
        return resp
    from repo.catalog import id_of_platform_row, delete_tool_row
    with db() as con:
        row = id_of_platform_row(con, tool_key, owner_id_is_null=True)
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        delete_tool_row(con, row["id"])
    from governance.pricing import set_pricing
    set_pricing(tool_key, 0, False, admin_user_id=user["id"])
    return {"ok": True}
