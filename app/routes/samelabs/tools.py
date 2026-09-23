"""/samelabs/tools — Tool Supply (merged Tools + Remote registry).

One table: every platform_tools row (Official local / Operator HTTP /
Provider Tool) + pricing overlay + call/earn aggregates. Mutations go
through core.operator.tools (single points preserved: governance.pricing
/ repo.catalog / governance.tool_supply)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import db
from economy.money import millis_to_usd_str
from repo import operator_views

from app.routes.samelabs.common import gate, ctx, fmt_num, tool_class

router = APIRouter()


@router.get("/samelabs/tools")
async def tools(request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    with db() as con:
        rows = operator_views.tools_page(con)
    items = []
    for r in rows:
        d = dict(r)
        d["cls"] = tool_class(d)
        d["owner"] = d["owner_name"] or "平台"
        d["provider_earn_usd"] = (millis_to_usd_str(
            int(d["provider_earn_millis"]))
            if d["owner_id"] is not None else "")
        items.append(d)
    return templates.TemplateResponse(
        request, "samelabs/tools.html",
        ctx(request, user, tools=items, fmt_num=fmt_num, page_id="tools"))


@router.post("/samelabs/tools/{tool_id}/pricing")
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


@router.post("/samelabs/tools/{tool_key}/presentation")
@router.patch("/samelabs/tools/{tool_key}/presentation")
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
        auth_header = str(raw.get("auth_header", "")).strip()
        auth_token = str(raw.get("auth_token", "")).strip()
        accept_raw = str(raw.get("accept", "")).strip()
        multi = raw.get("multi", False)
        if not isinstance(multi, bool):
            return JSONResponse({"ok": False, "error": "bad_multi"}, 422)
        try:
            cost = strict_int(raw.get("cost", 0))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)
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
             "auth_token": auth_token, "accept": accept, "multi": multi,
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


@router.post("/samelabs/tools/{tool_key}/delete")
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
