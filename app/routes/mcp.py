"""MCP server at /mcp (batch D1) — streamable-http, minimal, zero-dep.

The agent channel reuses the same boundaries as /api/v1: Bearer token
(api_tokens), the same upload pipeline (sniff/quota/gokapi), and the
same single metering gate (execute_tool). No SDK dependency — the
streamable-http transport is JSON-RPC over POST; we answer with
application/json (spec-legal; clients must accept both content types),
return 202 for notifications, 405 for GET/DELETE (stateless server,
no sessions).

Methods: initialize | notifications/initialized | ping | tools/list |
tools/call.
"""

import base64
import binascii
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import MAX_UPLOAD_BYTES
from tools import list_all_tools
from utils import sanitize_filename

router = APIRouter()

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]
MAX_B64_BYTES = MAX_UPLOAD_BYTES * 2  # b64 inflation bound, checked pre-decode


def _rpc(id_, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result if result is not None else {}
    return out


def _err(id_, code, message):
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
    # Stateless server: no server-initiated SSE stream.
    return Response(status_code=405, headers=_cors_headers())


@router.delete("/mcp")
async def mcp_delete():
    # Stateless server: no sessions to terminate.
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
        return JSONResponse(_err(None, -32700, "parse error"),
                            headers=_cors_headers())
    if isinstance(msg, list):  # 2024-11-05 batch
        out = []
        for m in msg:
            r = await _dispatch(request, user, m)
            if r is not None:
                out.append(r)
        return JSONResponse(out, headers=_cors_headers())
    resp = await _dispatch(request, user, msg)
    if resp is None:  # notification
        return Response(status_code=202, headers=_cors_headers())
    return JSONResponse(resp, headers=_cors_headers())


async def _dispatch(request: Request, user: dict, msg: dict):
    """Handle one JSON-RPC message. None ⇒ notification (202, no body)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "invalid request")
    mid = msg.get("id")
    method = msg.get("method", "")
    is_notification = "id" not in msg

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion", LATEST)
        version = requested if requested in SUPPORTED_VERSIONS else LATEST
        return _rpc(mid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "518plus", "version": "1.0.0"},
            "instructions": (
                "518.PLUS file-processing capability platform. Every tool "
                "call is metered in credits (balance in /api/v1/me). Pass "
                "client_ref on tools/call to retry safely — replays return "
                "the original result without re-charging."
            ),
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _rpc(mid, {})
    if is_notification:
        return None  # any other notification: accept silently
    if method == "tools/list":
        return _rpc(mid, {"tools": _tool_specs()})
    if method == "tools/call":
        params = msg.get("params") or {}
        return await _call_tool(request, user, mid, params)
    return _err(mid, -32601, f"method not found: {method}")


def _tool_specs() -> list:
    # list_all_tools: single catalog source (pricing overlay inside —
    # disabled hidden, cost included), identical to web home and /api/v1.
    specs = []
    for t in list_all_tools():
        multi = bool(t.get("multi"))
        if multi:
            props = {
                "files": {
                    "type": "array",
                    "description": "Source files (base64 + original filename)",
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
        else:
            mime_list = ", ".join(str(x) for x in (t.get("accept") or [])) or "any"
            props = {
                "file_b64": {
                    "type": "string",
                    "description": f"Source file content, base64. Accepted: {mime_list}",
                },
                "filename": {
                    "type": "string",
                    "description": "Original filename with extension, e.g. report.docx",
                },
            }
            required = ["file_b64", "filename"]
        props["params"] = {"type": "object", "description": "Tool-specific parameters"}
        props["client_ref"] = {
            "type": "string",
            "description": "Idempotency ref: reuse to retry after errors without double charge",
        }
        label = t["label"]
        desc = f"{label.get('en') or t['id']}"
        if label.get("zh"):
            desc += f" / {label['zh']}"
        if int(t["cost"]) > 0:
            desc += f" (cost: {int(t['cost'])} credits)"
        specs.append({
            "name": t["id"],
            "description": desc,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        })
    return specs


async def _call_tool(request: Request, user: dict, mid, params: dict):
    import secrets as _secrets
    from app.routes.apiv1 import _upload_source  # shared pipeline (v1 + MCP)
    tool_id = str(params.get("name") or "")
    args = params.get("arguments") or {}
    tool = get_tool(tool_id)
    if not tool:
        return _err(mid, -32602, f"unknown tool: {tool_id}")

    raw = {
        "tool_id": tool_id,
        "params": args.get("params") or {},
        "client_ref": str(args.get("client_ref") or "").strip() or
                      f"mcp:{_secrets.token_hex(16)}",
    }

    # Source: fresh files (b64) or a prior upload handle (public_id)
    if args.get("public_id"):
        raw["public_id"] = str(args["public_id"])
        raw["source_token"] = str(args.get("source_token") or "")
        if args.get("public_ids"):
            raw["public_ids"] = [str(x) for x in args["public_ids"]][:20]
            raw["source_tokens"] = [str(x) for x in (args.get("source_tokens") or [])][:20]
    else:
        try:
            uploads = []
            if tool.get("multi") and args.get("files"):
                for f in args["files"][:20]:
                    uploads.append(_decode_file(f.get("b64"), f.get("filename")))
            elif args.get("file_b64"):
                uploads.append(_decode_file(args["file_b64"], args.get("filename")))
            else:
                raise ValueError("no source file provided")
        except (ValueError, binascii.Error) as exc:
            return _err(mid, -32602, f"invalid arguments: {exc}")
        ip = request.headers.get("X-Real-IP",
                                 request.client.host if request.client else "").strip()
        try:
            if len(uploads) == 1:
                fn, ctype, body = uploads[0]
                info = _upload_source(user, fn, ctype, body, ip)
                raw["public_id"] = info["publicId"]
                raw["source_token"] = info["sourceToken"]
            else:
                raw["public_ids"] = []
                raw["source_tokens"] = []
                for fn, ctype, body in uploads:
                    info = _upload_source(user, fn, ctype, body, ip)
                    raw["public_ids"].append(info["publicId"])
                    raw["source_tokens"].append(info["sourceToken"])
        except Exception as exc:
            return _err(mid, -32602, f"upload rejected: {exc}")

    # Funnel through the ONE metering gate
    from app.routes.api import execute_tool as _internal_execute
    request.state._v1_raw = raw
    resp = await _internal_execute(request)
    if isinstance(resp, JSONResponse):
        payload = json.loads(resp.body)
    else:
        payload = resp
    if not payload.get("ok"):
        # Tool-level failure ⇒ MCP isError result (not a protocol error)
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
        raise ValueError("file_b64 and filename are required")
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
