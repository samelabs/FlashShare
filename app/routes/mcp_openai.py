"""Reviewable public MCP projection for ChatGPT/Codex.

The main /mcp endpoint remains 518's dynamic router and Tool Market surface.
This endpoint deliberately exposes only a fixed allowlist of first-party,
zero-cost file tools plus three file-lifecycle helpers. It never exposes
catalog_search, user/third-party tools, or paid tools.
"""

import copy
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.routes import mcp as base
from config import BASE_URL, PRODUCT_VERSION
from governance.catalog import list_catalog, mcp_names

router = APIRouter()

_FIRST_PARTY_IDS = (
    "share",
    "beautify",
    "compress",
    "crop_white",
    "ico",
    "img2pdf",
    "pdf_merge",
    "zip_pack",
)
_RESOURCE_METADATA = f"{BASE_URL}/.well-known/oauth-protected-resource"


def _oauth_user(request: Request):
    user = getattr(request.state, "user", None)
    via = getattr(request.state, "auth_via", "")
    return user if (user and via == "oauth") else None


def _security(scopes):
    return [{"type": "oauth2", "scopes": list(scopes)}]


def _noncommercial_description(value: str) -> str:
    desc = str(value or "").strip()
    # The general 518 MCP advertises pricing. Public Plugins cannot sell or
    # upsell digital services, so this reviewed surface removes commerce copy.
    desc = re.sub(r"(?i)\bfree;\s*", "", desc)
    desc = re.sub(r"(?i)\s*free\.?\s*$", "", desc)
    desc = re.sub(r"(?i)\s*price:\s*[^.]+\.\s*$", "", desc)
    return desc.strip()


def _review_spec(spec: dict, *, read_only: bool, destructive: bool,
                 open_world: bool, scopes, result_persists: bool = False):
    out = copy.deepcopy(spec)
    desc = _noncommercial_description(out.get("description") or "")
    if result_persists:
        desc += (
            " Creates temporary result files in the connected 518.PLUS "
            "account."
        )
    out["description"] = desc
    ann = dict(out.get("annotations") or {})
    ann.update({
        "readOnlyHint": bool(read_only),
        "destructiveHint": bool(destructive),
        "openWorldHint": bool(open_world),
        "idempotentHint": True,
    })
    out["annotations"] = ann
    out["securitySchemes"] = _security(scopes)
    return out


def _openai_specs(user: dict | None) -> list:
    from app.routes.mcp import _entitlements, _catalog_spec
    ent = _entitlements(user)
    names = mcp_names()

    specs = [
        _review_spec(
            base._CREATE_UPLOAD_SPEC, read_only=False, destructive=False,
            open_world=False, scopes=("files:write",)),
        _review_spec(
            base._LIST_UPLOADS_SPEC, read_only=True, destructive=False,
            open_world=False, scopes=("files:read",)),
        _review_spec(
            base._DELETE_UPLOAD_SPEC, read_only=False, destructive=True,
            open_world=False, scopes=("files:write",)),
    ]

    rows = list_catalog(user["id"] if user else None)
    by_id = {
        t["id"]: t for t in rows
        if not t.get("owner_handle") and int(t.get("cost") or 0) == 0
    }
    for tool_id in _FIRST_PARTY_IDS:
        tool = by_id.get(tool_id)
        if not tool:
            continue
        spec = _catalog_spec(tool, names, ent)
        specs.append(_review_spec(
            spec, read_only=False, destructive=False, open_world=True,
            scopes=("files:read", "files:write"), result_persists=True))
    return specs


def _scope_for(name: str) -> set:
    if name == "list_uploads":
        return {"files:read"}
    if name in ("create_upload", "delete_upload"):
        return {"files:write"}
    return {"files:read", "files:write"}


def _challenge(error: str = "invalid_token",
               description: str = "Connect your 518.PLUS account to continue") -> str:
    return (
        f'Bearer resource_metadata="{_RESOURCE_METADATA}", '
        f'error="{error}", error_description="{description}"'
    )


def _auth_tool_result(mid, required: set):
    scope = " ".join(sorted(required))
    challenge = _challenge(
        "insufficient_scope",
        f"Authorization required for scope: {scope}")
    return base._rpc(mid, {
        "content": [{"type": "text", "text": "Authentication required."}],
        "_meta": {"mcp/www_authenticate": [challenge]},
        "isError": True,
    })


async def _dispatch(request: Request, user: dict | None, msg):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return base._rpc_err(None, -32600, "invalid request")
    if "method" not in msg:
        if "result" in msg or "error" in msg:
            return None
        return base._rpc_err(msg.get("id"), -32600, "invalid request")

    mid = msg.get("id")
    method = msg.get("method")
    is_notification = "id" not in msg
    if not isinstance(method, str):
        return None if is_notification else base._rpc_err(
            mid, -32600, "invalid request")
    params = msg.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return None if is_notification else base._rpc_err(
            mid, -32602, "invalid params: params must be an object")

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = (requested if isinstance(requested, str)
                   and requested in base.SUPPORTED_VERSIONS else base.LATEST)
        return base._rpc(mid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "518plus-openai",
                "version": PRODUCT_VERSION,
            },
            "instructions": (
                "Process and temporarily share files with 518.PLUS "
                "first-party tools. This reviewed endpoint exposes a fixed "
                "tool set; it does not expose the 518 Tool Market or "
                "third-party tools. Tool calls require OAuth. Use "
                "create_upload for local files too large to inline, "
                "list_uploads to inspect still-live files, and delete_upload "
                "only when the user asks to delete a specific file. Generated "
                "files and share links are temporary and follow the connected "
                "account's retention and download limits."
            ),
        })
    if is_notification:
        return None
    if method == "ping":
        return base._rpc(mid, {})
    if method == "tools/list":
        specs = _openai_specs(user)
        cur = params.get("cursor")
        try:
            start = int(cur) if cur else 0
        except (TypeError, ValueError, OverflowError):
            return base._rpc_err(mid, -32602, "invalid cursor")
        if start < 0:
            return base._rpc_err(mid, -32602, "invalid cursor")
        page_size = 50
        out = {"tools": specs[start:start + page_size]}
        if start + page_size < len(specs):
            out["nextCursor"] = str(start + page_size)
        return base._rpc(mid, out)
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return base._rpc_err(
                mid, -32602, "invalid params: name required")
        allowed = {spec["name"] for spec in _openai_specs(user)}
        if name not in allowed:
            return base._rpc_err(
                mid, -32602, "tool is not exposed by this MCP endpoint")
        needed = _scope_for(name)
        if not user:
            return _auth_tool_result(mid, needed)
        granted = set(getattr(request.state, "oauth_scopes", set()))
        if not needed.issubset(granted):
            return _auth_tool_result(mid, needed)
        return await base._call_tool(request, user, mid, params)
    return base._rpc_err(mid, -32601, f"method not found: {method}")


async def _dispatch_safe(request: Request, user, msg):
    try:
        return await _dispatch(request, user, msg)
    except Exception:
        base._log.exception("openai mcp dispatch failed")
        mid = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(msg, dict) and "id" not in msg:
            return None
        return base._rpc_err(mid, -32603, "internal error")


@router.options("/mcp/openai")
async def openai_options(request: Request):
    if base._origin_denied(request):
        return Response(
            status_code=403, headers=base._cors_headers(request))
    return Response(status_code=204, headers=base._cors_headers(request))


@router.get("/mcp/openai")
async def openai_get(request: Request):
    return Response(status_code=405, headers=base._cors_headers(request))


@router.delete("/mcp/openai")
async def openai_delete(request: Request):
    return Response(status_code=405, headers=base._cors_headers(request))


@router.post("/mcp/openai")
async def openai_post(request: Request):
    cors = base._cors_headers(request)
    if base._origin_denied(request):
        return Response(status_code=403, headers=cors)

    user = _oauth_user(request)
    if not user and request.headers.get("authorization"):
        return JSONResponse(
            {
                "error": "unauthorized",
                "message": "OAuth access token is invalid or expired.",
            },
            status_code=401,
            headers={
                **cors,
                "WWW-Authenticate": _challenge(
                    "invalid_token", "Reconnect 518.PLUS"),
            },
        )

    version = request.headers.get("mcp-protocol-version")
    version = (
        base.HEADER_DEFAULT_VERSION if version is None else version.strip())
    if version not in base.SUPPORTED_VERSIONS:
        return JSONResponse(
            base._rpc_err(
                None, -32600, "unsupported MCP-Protocol-Version",
                {"supported": list(base.SUPPORTED_VERSIONS)}),
            status_code=400, headers=cors)

    body = await base._read_body_limited(request)
    if body is None:
        return base._body_too_large(cors)
    try:
        msg = json.loads(body)
    except Exception:
        return JSONResponse(
            base._rpc_err(None, -32700, "parse error"), headers=cors)

    if isinstance(msg, list):
        if version >= base.BATCH_REMOVED_IN:
            return JSONResponse(
                base._rpc_err(
                    None, -32600,
                    f"JSON-RPC batch not supported in protocol version {version}"),
                status_code=400, headers=cors)
        if not msg:
            return JSONResponse(
                base._rpc_err(
                    None, -32600, "invalid request: empty batch"),
                status_code=400, headers=cors)
        out = []
        for item in msg:
            result = await _dispatch_safe(request, user, item)
            if result is not None:
                out.append(result)
        if not out:
            return Response(status_code=202, headers=cors)
        return JSONResponse(out, headers=cors)

    result = await _dispatch_safe(request, user, msg)
    if result is None:
        return Response(status_code=202, headers=cors)
    return JSONResponse(result, headers=cors)
