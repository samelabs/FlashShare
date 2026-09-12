"""MCP server at /mcp — stateless streamable HTTP.

The agent channel reuses the same boundaries as /api/v1: Bearer identity,
canonical Catalog, source ingress pipeline, ToolInvocation and metering gate.
Methods: initialize | notifications/initialized | ping | tools/list | tools/call.
"""

import base64
import binascii
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import MAX_UPLOAD_BYTES
from governance.catalog import list_catalog, resolve as resolve_tool, mcp_name
from governance.tool_params import resolve_label
from utils import sanitize_filename

router = APIRouter()

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]
MAX_B64_BYTES = MAX_UPLOAD_BYTES * 2


def _rpc(id_, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result if result is not None else {}
    return out


def _rpc_err(id_, code, message):
    return _rpc(id_, error={"code": code, "message": message})


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Mcp-Session-Id",
    }


def _bearer_user(request: Request):
    user = getattr(request.state, "user", None)
    via = getattr(request.state, "auth_via", "")
    return user if (user and via == "token") else None


@router.options("/mcp")
async def mcp_options():
    return Response(status_code=204, headers=_cors_headers())


@router.get("/mcp")
async def mcp_get():
    return Response(status_code=405, headers=_cors_headers())


@router.delete("/mcp")
async def mcp_delete():
    return Response(status_code=405, headers=_cors_headers())


@router.post("/mcp")
async def mcp_post(request: Request):
    user = _bearer_user(request)
    if not user:
        return Response(
            status_code=401,
            headers={**_cors_headers(), "WWW-Authenticate": "Bearer"},
        )
    try:
        msg = json.loads(await request.body())
    except Exception:
        return JSONResponse(_rpc_err(None, -32700, "parse error"),
                            headers=_cors_headers())
    if isinstance(msg, list):
        out = []
        for m in msg:
            r = await _dispatch(request, user, m)
            if r is not None:
                out.append(r)
        return JSONResponse(out, headers=_cors_headers())
    resp = await _dispatch(request, user, msg)
    if resp is None:
        return Response(status_code=202, headers=_cors_headers())
    return JSONResponse(resp, headers=_cors_headers())


async def _dispatch(request: Request, user: dict, msg: dict):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_err(None, -32600, "invalid request")
    mid = msg.get("id")
    method = msg.get("method", "")
    is_notification = "id" not in msg

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion", LATEST)
        version = requested if requested in SUPPORTED_VERSIONS else LATEST
        from governance.catalog import catalog_version
        return _rpc(mid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "518plus", "version": "1.0.0"},
            "instructions": (
                "518.PLUS capability platform. tools/list is paginated "
                "(pass cursor for the next page); meta tool 'catalog_search' "
                "finds tools by keyword when the catalog is large. Every "
                "call is metered in credits (/api/v1/me); pass client_ref "
                "on tools/call to retry safely — replays return the "
                "original result without re-charging. Catalog version: "
                f"{catalog_version()} — re-list when it changes."
            ),
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _rpc(mid, {})
    if is_notification:
        return None
    if method == "tools/list":
        params = msg.get("params") or {}
        page_size = 50
        specs = _tool_specs(user)
        cur = params.get("cursor")
        try:
            start = int(cur) if cur else 0
        except (TypeError, ValueError):
            return _rpc_err(mid, -32602, "invalid cursor")
        out = {"tools": specs[start:start + page_size]}
        if start + page_size < len(specs):
            out["nextCursor"] = str(start + page_size)
        return _rpc(mid, out)
    if method == "tools/call":
        params = msg.get("params") or {}
        return await _call_tool(request, user, mid, params)
    return _rpc_err(mid, -32601, f"method not found: {method}")


def _tool_specs(user: dict) -> list:
    specs = []
    for t in list_catalog(user["id"]):
        multi = bool(t.get("multi"))
        props = {
            "files": {
                "type": "array",
                "description": "Source files (base64 + original filename)",
                "minItems": 1,
                "maxItems": 20 if multi else 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "b64": {"type": "string"},
                    },
                    "required": ["filename", "b64"],
                },
            },
        }
        required = ["files"]
        decl = t.get("params") or []
        if decl:
            from governance.tool_params import to_input_schema_properties
            sub_props, sub_required = to_input_schema_properties(decl)
            props["params"] = {
                "type": "object",
                "description": "Tool parameters (validated server-side)",
                "properties": sub_props,
                **({"required": sub_required} if sub_required else {}),
            }
        else:
            props["params"] = {"type": "object", "description": "Tool-specific parameters"}
        props["delivery"] = {
            "type": "object",
            "description": "Result delivery policy (optional; defaults 1 day / 50 downloads)",
            "properties": {
                "expiry_days": {"type": "integer", "enum": [1, 3, 7]},
                "allowed_downloads": {"type": "integer", "enum": [1, 10, 50, 100, 200]},
            },
        }
        props["client_ref"] = {
            "type": "string",
            "description": "Idempotency ref: reuse to retry this tool call safely",
        }
        label = t["label"]
        desc = f"{label.get('en') or t['id']}"
        if label.get("zh"):
            desc += f" / {label['zh']}"
        tool_desc = resolve_label(t.get("desc"), "en") if t.get("desc") else ""
        if tool_desc:
            desc += f" — {tool_desc}"
        if int(t["cost"]) > 0:
            desc += f" (cost: {int(t['cost'])} credits)"
        specs.append({
            "name": mcp_name(t["id"]),
            "description": desc,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        })
    specs.append({
        "name": "catalog_search",
        "description": "Find tools by keyword (matches id and labels, all languages). Returns matching tool names.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    })
    return specs


async def _call_tool(request: Request, user: dict, mid, params: dict):
    import secrets as _secrets
    from execution.pipeline import upload_sources as _upload_batch
    from execution.pipeline import IngressBoundaryError, IngressQuotaError

    tool_id = str(params.get("name") or "")
    args = params.get("arguments") or {}
    if tool_id == "catalog_search":
        from governance.catalog import search_catalog
        q = str(args.get("q") or "").strip()[:64]
        hits = search_catalog(q, user["id"], limit=20) if q else []
        return _rpc(mid, {
            "content": [{"type": "text", "text": json.dumps(
                {"query": q, "tools": [h["id"] for h in hits]}, ensure_ascii=False)}],
            "isError": False,
        })
    tool_key = tool_id.replace("__", "/") if "__" in tool_id else tool_id

    if "client_ref" in args:
        cr = args.get("client_ref")
        if not isinstance(cr, str) or not cr.strip() or len(cr) > 128:
            return _rpc_err(mid, -32602, "invalid client_ref")
        client_ref = cr.strip()
    else:
        client_ref = f"mcp:{_secrets.token_hex(16)}"

    from governance.delivery import (validate_delivery, DEFAULT_EXPIRY_DAYS,
                                     DEFAULT_ALLOWED_DOWNLOADS)
    if "delivery" in args:
        d = args.get("delivery")
        if not isinstance(d, dict):
            return _rpc_err(mid, -32602, "invalid delivery")
        if set(d.keys()) - {"expiry_days", "allowed_downloads"}:
            return _rpc_err(mid, -32602, "invalid delivery")
        try:
            delivery = validate_delivery({
                "expiry_days": d.get("expiry_days", DEFAULT_EXPIRY_DAYS),
                "allowed_downloads": d.get("allowed_downloads",
                                           DEFAULT_ALLOWED_DOWNLOADS)})
        except ValueError:
            return _rpc_err(mid, -32602, "invalid delivery")
    else:
        delivery = {"expiry_days": DEFAULT_EXPIRY_DAYS,
                    "allowed_downloads": DEFAULT_ALLOWED_DOWNLOADS}

    if "params" in args:
        tool_params = args.get("params")
        if not isinstance(tool_params, dict):
            return _rpc_err(mid, -32602, "invalid params")
    else:
        tool_params = {}

    try:
        files_arg = args.get("files")
        if not isinstance(files_arg, list) or not files_arg:
            raise ValueError("files[] required")
        uploads = []
        for f in files_arg:
            if not isinstance(f, dict):
                raise ValueError("files[] items must be objects")
            uploads.append(_decode_file(f.get("b64"), f.get("filename")))
    except (ValueError, binascii.Error) as exc:
        return _rpc_err(mid, -32602, f"invalid arguments: {exc}")

    ip = request.headers.get("X-Real-IP",
                             request.client.host if request.client else "").strip()
    try:
        infos = _upload_batch(user, uploads, ip)
        source_files = [{"public_id": info["publicId"],
                         "source_token": info.get("sourceToken", "")}
                        for info in infos]
    except (IngressBoundaryError, IngressQuotaError) as exc:
        # Platform business limits — hard boundaries and rolling quota —
        # are tool results, not JSON-RPC syntax errors: they answer as
        # isError content carrying the structured denial.
        return _rpc(mid, {
            "content": [{"type": "text", "text": json.dumps(exc.denial)}],
            "isError": True,
        })
    except ValueError as exc:
        return _rpc_err(mid, -32602, f"upload rejected: {exc}")
    except Exception as exc:
        # Generic upload/storage failure fallback (pre-existing behavior):
        # only IngressBoundaryError is specialized above; every other
        # exception keeps the original rejection semantics.
        return _rpc_err(mid, -32602, f"upload rejected: {exc}")

    raw = {
        "tool_id": tool_key,
        "source": {"files": source_files},
        "params": tool_params,
        "delivery": delivery,
        "client_ref": client_ref,
    }

    from execution.dispatch import dispatch_execute
    payload, error_body, status = await dispatch_execute(request, user, raw)
    if error_body is not None:
        payload = error_body
    if not payload.get("ok"):
        return _rpc(mid, {
            "content": [{"type": "text",
                         "text": json.dumps({"error": payload.get("error"),
                                             "client_ref": raw["client_ref"]})}],
            "isError": True,
        })
    out = {
        "client_ref": raw["client_ref"],
        "call_id": payload.get("call_id"),
        "replay": bool(payload.get("replay")),
        "result": payload.get("result"),
    }
    return _rpc(mid, {
        "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
        "isError": False,
    })


def _decode_file(b64, filename) -> tuple:
    if not b64 or not filename:
        raise ValueError("files[] items need b64 and filename")
    if len(b64) > MAX_B64_BYTES:
        raise ValueError("file exceeds size limit")
    body = base64.b64decode(b64, validate=True)
    if not body:
        raise ValueError("empty file")
    fn = sanitize_filename(str(filename))
    ctype = _guess_ctype(fn)
    return fn, ctype, body


def _guess_ctype(filename: str) -> str:
    import mimetypes
    ct, _ = mimetypes.guess_type(filename)
    return ct or "application/octet-stream"
