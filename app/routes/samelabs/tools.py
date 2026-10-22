"""/samelabs/tools — Tool Supply (merged Tools + Remote registry).

One table: every platform_tools row (Official local / Operator HTTP /
Provider Tool) + pricing overlay + call/earn aggregates, with operator
search / class / delisted / featured filters and pagination. The detail
page /samelabs/tools/{key} shows the full declaration + 30-day stats +
recent calls + audit history and drives the SAME mutation endpoints
(single points preserved: governance.pricing / repo.catalog /
governance.tool_supply)."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import db
from economy.money import millis_to_usd_str
from repo import operator_views
from utils import now, page_number

from app.routes.samelabs.common import gate, ctx, fmt_num, tool_class

router = APIRouter()

_PAGE = 50


@router.get("/samelabs/tools")
async def tools(request: Request, q: str = "", cls: str = "",
                delisted: str = "", featured: str = "", page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = page_number(page)
    q = q.strip()[:100]
    if cls not in ("", "official", "community"):
        cls = ""
    with db() as con:
        rows, total = operator_views.tools_page(
            con, q or None, cls or None,
            delisted == "1", featured == "1",
            _PAGE, (page - 1) * _PAGE)
    items = []
    for r in rows:
        d = dict(r)
        d["cls"] = tool_class(d)
        d["owner"] = d["owner_name"] or "平台"
        d["provider_earn_usd"] = (millis_to_usd_str(
            int(d["provider_earn_millis"]))
            if d["owner_id"] is not None else "")
        items.append(d)
    from governance.formats import tags_manifest
    return templates.TemplateResponse(
        request, "samelabs/tools.html",
        ctx(request, user, tools=items, fmt_num=fmt_num, total=total,
            q=q, f_class=cls,
            f_delisted=(delisted == "1"), f_featured=(featured == "1"),
            tags=tags_manifest(),
            page=page, pages=max(1, -(-total // _PAGE)),
            page_id="tools"))


def _parse_json(raw, default):
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


@router.get("/samelabs/tools/{tool_key:path}")
async def tool_detail(tool_key: str, request: Request):
    """Read-only operator view of one tool: declaration (labels/desc per
    language, endpoint without secrets, input window, formats, outputs,
    params, pricing) + 30-day usage/earnings + recent calls + this
    tool's audit history. Mutations reuse the list-page endpoints."""
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    tool_key = tool_key.strip("/")
    if not tool_key:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    ts = now()
    with db() as con:
        row = operator_views.tool_row(con, tool_key)
        if row is None:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        t = dict(row)
        t["cls"] = tool_class(t)
        t["owner"] = t["owner_name"] or "平台"
        labels = _parse_json(t["label_json"], {})
        t["labels"] = labels if isinstance(labels, dict) else {}
        descs = _parse_json(t["desc_json"], {})
        if isinstance(descs, dict):
            t["descs"] = descs
        elif descs:
            t["descs"] = {"—": str(descs)}
        else:
            t["descs"] = {}
        accept = _parse_json(t["accept_json"], None)
        t["accept"] = accept if isinstance(accept, list) else None
        outputs = _parse_json(t["outputs_json"], ["file"])
        t["outputs"] = outputs if isinstance(outputs, list) else []
        params = _parse_json(t["params_json"], [])
        t["params"] = [p for p in params if isinstance(p, dict)] \
            if isinstance(params, list) else []
        endpoint = _parse_json(t["endpoint_json"], {}) if t["execution"] == "http" else {}
        from repo.catalog import pricing_row
        pricing = pricing_row(con, tool_key)
        stats = dict(operator_views.tool_calls_window(
            con, tool_key, ts - 30 * 86400))
        earn_millis = operator_views.tool_earn_window(
            con, tool_key, ts - 30 * 86400)
        recent = [dict(r) for r in
                  operator_views.tool_recent_calls(con, tool_key, 20)]
        audit_rows = [dict(r) for r in
                      operator_views.tool_audit_history(con, tool_key, 20)]
    # endpoint projection: URL + timeout only; auth presence as a fact,
    # auth header NAME / token VALUE never render (operator or not)
    t["endpoint_url"] = endpoint.get("url", "") if t["execution"] == "http" else ""
    t["endpoint_timeout"] = endpoint.get("timeout_s", "") if t["execution"] == "http" else ""
    t["endpoint_has_auth"] = bool(endpoint.get("auth_header")
                                  or endpoint.get("auth_token"))
    total_calls = int(stats["total"] or 0)
    stats["fail_rate"] = (f"{int(stats['failed']) * 100 // total_calls}%"
                          if total_calls else "—")
    for r in recent:
        r["cost"] = int(r["net_charge"])
    return templates.TemplateResponse(
        request, "samelabs/tool_detail.html",
        ctx(request, user, t=t, pricing=(dict(pricing) if pricing else None),
            stats=stats,
            earn_usd=(millis_to_usd_str(earn_millis)
                      if t["owner_id"] is not None else ""),
            recent=recent, audit_rows=audit_rows,
            is_platform_http=(t["owner_id"] is None
                              and t["execution"] == "http"),
            fmt_num=fmt_num, page_title=f"工具 {tool_key}",
            page_id="tools"))


@router.post("/samelabs/tools/{tool_id:path}/pricing")
async def set_pricing(tool_id: str, request: Request):
    """Strict validation → command (one tx: lock, before-snapshot,
    mutate, audit). Retired-tool fallback semantics preserved."""
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        from backoffice.strict import strict_int, strict_bool
        try:
            cost = strict_int(raw.get("cost", 0))
            enabled = strict_bool(raw.get("enabled", True))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)
        if cost < 0 or cost > 1000000:
            raise ValueError
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice import tools as tools_cmd
    try:
        row = tools_cmd.set_tool_pricing(int(user["id"]), tool_id, cost,
                                         enabled)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True,
            "pricing": {k: row[k] for k in ("tool_id", "cost", "enabled")}}


@router.post("/samelabs/tools/{tool_key:path}/presentation")
@router.patch("/samelabs/tools/{tool_key:path}/presentation")
async def set_presentation(tool_key: str, request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        from backoffice.strict import strict_int, strict_bool
        try:
            featured = strict_bool(raw.get("featured", False))
            sort_order = strict_int(raw.get("sort_order", 0))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice import tools as tools_cmd
    try:
        tools_cmd.set_tool_presentation(int(user["id"]), tool_key,
                                        featured, sort_order)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True}


@router.post("/samelabs/tools/{tool_key:path}/delist")
async def set_delisted(tool_key: str, request: Request):
    """Operator takedown switch (platform_tools.delisted): the ONLY
    operator visibility control. Delisting hides the tool from every
    public surface (lists, search, market, author page, MCP, direct
    calls) regardless of the developer's own enabled flag; the
    developer cannot lift it. Strict bool → one audited transaction."""
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice.strict import strict_bool
    try:
        delisted = strict_bool(raw.get("delisted"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    from backoffice import tools as tools_cmd
    try:
        row = tools_cmd.set_tool_delisted(int(user["id"]), tool_key,
                                          delisted)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True, "tool_key": row["tool_key"],
            "delisted": row["delisted"]}


@router.post("/samelabs/tools/create")
async def create_http_tool(request: Request):
    """Operator trusted HTTP tool create (bare-id first-party
    namespace). Validation identical to the retired /samelabs/remote/create."""
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        tool_id = str(raw.get("tool_id", "")).strip()
        label = str(raw.get("label", "")).strip() or tool_id
        url = str(raw.get("url", "")).strip()
        from backoffice.strict import strict_int
        try:
            timeout_s = strict_int(raw.get("timeout_s", 60))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)

        def _norm_str(key):
            v = raw.get(key, "")
            if v is None:
                return ""
            if not isinstance(v, str):
                return None
            return v.strip()
        auth_header = _norm_str("auth_header")
        if auth_header is None:
            return JSONResponse({"ok": False, "error": "bad_binding"}, 422)
        auth_token = _norm_str("auth_token")
        if auth_token is None:
            return JSONResponse({"ok": False, "error": "bad_binding"}, 422)
        accept_raw = _norm_str("accept")
        if accept_raw is None:
            return JSONResponse({"ok": False, "error": "bad_accept"}, 422)
        files_min = raw.get("files_min", 1)
        files_max = raw.get("files_max", 1)
        if isinstance(files_min, bool) or not isinstance(files_min, int) \
                or files_min < 0:
            return JSONResponse({"ok": False, "error": "bad_files_min"}, 422)
        if isinstance(files_max, bool) or not isinstance(files_max, int) \
                or not (0 <= files_max <= 20):
            return JSONResponse({"ok": False, "error": "bad_files_max"}, 422)
        if files_min > files_max:
            return JSONResponse({"ok": False, "error": "bad_files_range"}, 422)
        if files_max == 0 and accept_raw:
            return JSONResponse({"ok": False, "error": "bad_accept"}, 422)
        # tag 声明 (契约字段): 文件工具必带九键之一; 正文工具不带
        tag_raw = raw.get("format_families")
        if files_max > 0 and not tag_raw:
            return JSONResponse({"ok": False, "error": "bad_format_family"}, 422)
        if files_max == 0 and tag_raw:
            return JSONResponse({"ok": False, "error": "bad_format_family"}, 422)
        # 正文工具输入方式: files_max=0 必填 'text'|'json'; 文件工具禁带
        from governance.body import BODY_FORMATS as _BF
        body_format = raw.get("body_format")
        if files_max == 0 and body_format not in _BF:
            return JSONResponse({"ok": False, "error": "bad_body_format"}, 422)
        if files_max > 0 and body_format is not None:
            return JSONResponse({"ok": False, "error": "bad_body_format"}, 422)
        body_placeholder = raw.get("body_placeholder")
        if files_max > 0 and body_placeholder is not None:
            return JSONResponse({"ok": False, "error": "bad_body_placeholder"}, 422)
        outputs = raw.get("outputs", ["file"])
        if not isinstance(outputs, list) or not outputs \
                or any(k not in ("text", "json", "file") for k in outputs):
            return JSONResponse({"ok": False, "error": "bad_outputs"}, 422)
        try:
            cost = strict_int(raw.get("cost", 0))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)
        params_decl = raw.get("params")
        if params_decl is not None and not isinstance(params_decl, list):
            return JSONResponse({"ok": False, "error": "bad_params"}, 422)
        if "output_filename" in raw:
            return JSONResponse({"ok": False,
                                 "error": "bad_output_filename"}, 422)
        if "output_content_type" in raw:
            return JSONResponse({"ok": False,
                                 "error": "bad_output_content_type"}, 422)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]{1,40}$", tool_id):
        return JSONResponse({"ok": False, "error": "bad_tool_id"}, 422)
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "bad_url"}, 422)
    if not (5 <= timeout_s <= 300):
        return JSONResponse({"ok": False, "error": "bad_timeout"}, 422)
    # accept 单形状: 文件工具必带显式扩展名 (无 NULL=any 哨兵)
    if files_max > 0 and not accept_raw:
        return JSONResponse({"ok": False, "error": "bad_accept"}, 422)
    accept = None
    if accept_raw:
        tokens = [x.strip() for x in accept_raw.split(",") if x.strip()]
        from governance.formats import extensions_for_accept
        try:
            canon = extensions_for_accept(tokens)
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "bad_accept"}, 422)
        accept = sorted(canon) if canon else None
    if auth_token:
        from secrets_vault import vault_enabled
        if not vault_enabled():
            return JSONResponse(
                {"ok": False, "error": "credential_vault_unavailable"}, 503)
    from backoffice import tools as tools_cmd
    from governance.tool_supply import DeclarationError
    try:
        tool_key = tools_cmd.create_http_tool(
            int(user["id"]),
            {"tool_id": tool_id, "label": label, "url": url,
             "timeout_s": timeout_s, "auth_header": auth_header,
             "auth_token": auth_token, "accept": accept,
             "format_families": tag_raw,
             "body_format": body_format,
             "body_placeholder": body_placeholder,
             "files_min": files_min, "files_max": files_max,
             "outputs": [k for k in ("text", "json", "file")
                         if k in set(outputs)],
             "params": params_decl,
             "cost": cost})
    except DeclarationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    except ValueError as exc:
        token = str(exc)
        status = 409 if token in ("duplicate_tool",) else 422
        if token in ("bad_cost",):
            return JSONResponse({"ok": False, "error": "bad_cost"}, 422)
        return JSONResponse({"ok": False, "error": token}, status)
    return {"ok": True, "tool_key": tool_key}


@router.post("/samelabs/tools/{tool_key:path}/delete")
async def delete_http_tool(tool_key: str, request: Request):
    """Platform-owned HTTP tool delete (tool row + pricing disable +
    audit, one tx)."""
    user, resp = gate(request)
    if resp:
        return resp
    from backoffice import tools as tools_cmd
    try:
        tools_cmd.delete_http_tool(int(user["id"]), tool_key)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True}
