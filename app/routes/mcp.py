"""MCP server at /mcp — stateless streamable HTTP (POST → JSON).

The agent channel authenticates with a Bearer identity and
canonical Catalog, source ingress pipeline, ToolInvocation and metering gate.
Methods: initialize | notifications/initialized | ping | tools/list | tools/call.

Transport rules (Streamable HTTP):
  - Origin present and not allow-listed → 403 (DNS-rebinding guard);
    absent Origin (non-browser client) is admitted.
  - MCP-Protocol-Version header present and unsupported → 400; absent →
    2025-03-26.
  - JSON-RPC batches exist only in protocol versions before 2025-06-18.
"""

import asyncio
import base64
import binascii
import json
import logging
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import BASE_URL, MAX_UPLOAD_BYTES, MCP_ALLOWED_ORIGINS, PRODUCT_VERSION
from governance.catalog import list_catalog, mcp_name, mcp_names, tool_key_for_mcp_name
from governance.tool_params import resolve_label
from utils import sanitize_filename

router = APIRouter()
_log = logging.getLogger("flashshare")

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]
# Streamable HTTP: a request without MCP-Protocol-Version is 2025-03-26.
HEADER_DEFAULT_VERSION = "2025-03-26"
# JSON-RPC batching was removed from the protocol in 2025-06-18.
BATCH_REMOVED_IN = "2025-06-18"
MAX_B64_BYTES = MAX_UPLOAD_BYTES * 2
MAX_SOURCE_URL_LEN = 2048
URL_FETCH_TIMEOUT_S = 30
# Server-defined JSON-RPC error (reserved range -32000..-32099): the
# client_ref is bound to a different input, or its ingress is running.
ERR_IDEMPOTENCY = -32009


def _rpc(id_, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result if result is not None else {}
    return out


def _rpc_err(id_, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return _rpc(id_, error=err)


# ── Origin / CORS ──

def _origin_key(value) -> str | None:
    """Normalized scheme://host[:port] (default port omitted), or None."""
    try:
        parts = urllib.parse.urlsplit(str(value).strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + ("" if port in (None, default) else f":{port}")


_ALLOWED_ORIGINS = frozenset(
    k for k in (_origin_key(o) for o in (BASE_URL, *MCP_ALLOWED_ORIGINS)) if k)


def _origin_denied(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return _origin_key(origin) not in _ALLOWED_ORIGINS


def _cors_headers(request: Request):
    headers = {
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": ("Authorization, Content-Type, "
                                         "MCP-Protocol-Version, Mcp-Session-Id, "
                                         "Last-Event-ID"),
        "Access-Control-Expose-Headers": "WWW-Authenticate, Mcp-Session-Id",
        "Vary": "Origin",
    }
    origin = request.headers.get("origin")
    if origin is not None and not _origin_denied(request):
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def _bearer_user(request: Request):
    user = getattr(request.state, "user", None)
    via = getattr(request.state, "auth_via", "")
    return user if (user and via == "token") else None


@router.options("/mcp")
async def mcp_options(request: Request):
    if _origin_denied(request):
        return Response(status_code=403, headers=_cors_headers(request))
    return Response(status_code=204, headers=_cors_headers(request))


@router.get("/mcp")
async def mcp_get(request: Request):
    return Response(status_code=405, headers=_cors_headers(request))


@router.delete("/mcp")
async def mcp_delete(request: Request):
    return Response(status_code=405, headers=_cors_headers(request))


@router.post("/mcp")
async def mcp_post(request: Request):
    cors = _cors_headers(request)
    if _origin_denied(request):
        return Response(status_code=403, headers=cors)
    user = _bearer_user(request)
    if not user:
        return Response(
            status_code=401,
            headers={**cors, "WWW-Authenticate": 'Bearer realm="518"'},
        )
    version = request.headers.get("mcp-protocol-version")
    version = HEADER_DEFAULT_VERSION if version is None else version.strip()
    if version not in SUPPORTED_VERSIONS:
        return JSONResponse(
            _rpc_err(None, -32600, "unsupported MCP-Protocol-Version",
                     {"supported": list(SUPPORTED_VERSIONS)}),
            status_code=400, headers=cors)
    try:
        msg = json.loads(await request.body())
    except Exception:
        return JSONResponse(_rpc_err(None, -32700, "parse error"),
                            headers=cors)
    if isinstance(msg, list):
        if version >= BATCH_REMOVED_IN:
            return JSONResponse(
                _rpc_err(None, -32600,
                         f"JSON-RPC batch not supported in protocol version {version}"),
                status_code=400, headers=cors)
        if not msg:
            return JSONResponse(_rpc_err(None, -32600, "invalid request: empty batch"),
                                status_code=400, headers=cors)
        out = []
        for m in msg:
            r = await _dispatch_safe(request, user, m)
            if r is not None:
                out.append(r)
        if not out:
            # only notifications / responses: accepted, nothing to answer
            return Response(status_code=202, headers=cors)
        return JSONResponse(out, headers=cors)
    resp = await _dispatch_safe(request, user, msg)
    if resp is None:
        return Response(status_code=202, headers=cors)
    return JSONResponse(resp, headers=cors)


async def _dispatch_safe(request: Request, user: dict, msg):
    """Every failure answers as JSON-RPC — never an HTTP 500 body."""
    try:
        return await _dispatch(request, user, msg)
    except Exception:
        _log.exception("mcp dispatch failed")
        mid = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(msg, dict) and "id" not in msg:
            return None
        return _rpc_err(mid, -32603, "internal error")


async def _dispatch(request: Request, user: dict, msg):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_err(None, -32600, "invalid request")
    if "method" not in msg:
        # A JSON-RPC response from the client: this stateless server never
        # sends requests, so there is nothing to correlate — accept, ignore.
        if "result" in msg or "error" in msg:
            return None
        return _rpc_err(msg.get("id"), -32600, "invalid request")
    mid = msg.get("id")
    method = msg.get("method")
    is_notification = "id" not in msg
    if not isinstance(method, str):
        return None if is_notification else _rpc_err(mid, -32600, "invalid request")
    params = msg.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        if is_notification:
            return None
        return _rpc_err(mid, -32602, "invalid params: params must be an object")

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = (requested if isinstance(requested, str)
                   and requested in SUPPORTED_VERSIONS else LATEST)
        from governance.catalog import catalog_version
        return _rpc(mid, {
            "protocolVersion": version,
            # no server→client stream exists, so no list_changed notifications
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "518plus", "version": PRODUCT_VERSION},
            "instructions": (
                "518.PLUS is a tools platform for people and AI agents. "
                "Discover tools with tools/list (paginated — pass the "
                "returned cursor) or catalog_search. Pass files inline "
                "(base64), by https URL, or by reference; for large "
                "local files call create_upload first and PUT the raw "
                "bytes to the one-time URL it returns, then use the "
                "returned {public_id, source_token} as the files[] "
                "reference form. Text-only tools take no files — just "
                "params. Every tools/call needs a client_ref: reuse the "
                "same value to retry safely (replays return the original "
                "result without re-charging). Results come back directly: "
                "structuredContent.items holds ordered text/json/file "
                "items with download_url for each file; result_url is a "
                "human-viewable page. Calls are metered in credits "
                "(1000 credits = 1 USD); failed calls are refunded "
                "automatically. Catalog version: "
                f"{catalog_version()}."
            ),
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _rpc(mid, {})
    if is_notification:
        return None
    if method == "tools/list":
        page_size = 50
        specs = _tool_specs(user)
        cur = params.get("cursor")
        try:
            start = int(cur) if cur else 0
        except (TypeError, ValueError):
            return _rpc_err(mid, -32602, "invalid cursor")
        if start < 0:
            return _rpc_err(mid, -32602, "invalid cursor")
        out = {"tools": specs[start:start + page_size]}
        if start + page_size < len(specs):
            out["nextCursor"] = str(start + page_size)
        return _rpc(mid, out)
    if method == "tools/call":
        return await _call_tool(request, user, mid, params)
    return _rpc_err(mid, -32601, f"method not found: {method}")


_FILE_ITEM_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "title": "inline",
            "description": "Inline file: base64 bytes + original filename "
                           "(the extension decides the format).",
            "properties": {
                "filename": {"type": "string"},
                "b64": {"type": "string", "description": "Standard base64 of the file bytes"},
            },
            "required": ["filename", "b64"],
        },
        {
            "type": "object",
            "title": "url",
            "description": "Public https URL that 518 downloads server-side "
                           "(public hosts only; same size cap, quota and "
                           "client_ref idempotency as inline files). filename "
                           "defaults to the last URL path segment.",
            "properties": {
                "url": {"type": "string", "format": "uri", "pattern": "^https://"},
                "filename": {"type": "string"},
            },
            "required": ["url"],
        },
        {
            "type": "object",
            "title": "reference",
            "description": "An existing 518 upload owned by you: public_id "
                           "plus the source_token returned with it (omit the "
                           "token for your own shared uploads). No re-upload, "
                           "no ingress quota. For files too large to inline "
                           "as base64, call the meta tool 'create_upload' "
                           "and PUT the raw bytes to the returned upload_url "
                           "— its response gives you exactly this "
                           "{public_id, source_token} pair.",
            "properties": {
                "public_id": {"type": "string"},
                "source_token": {"type": "string"},
            },
            "required": ["public_id"],
        },
    ],
}


# Meta tool (official, free, no tool_call billing): stage a large local
# file without its bytes ever passing through the model.
_CREATE_UPLOAD_SPEC = {
    "name": "create_upload",
    "title": "Create Upload",
    "description":
        "Stage one local file for a later tools/call without sending its "
        "bytes through the model. Returns a one-time HTTPS PUT URL "
        "(upload_url, 10-minute expiry), the byte cap (max_bytes) and the "
        "exact declared size bound. After a successful PUT you receive "
        "{public_id, source_token} — pass them as the files[] reference "
        "form of any tool. Free; consumes your daily upload quota only "
        "when the PUT succeeds.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "filename": {"type": "string",
                         "description": "Original filename (extension "
                                        "decodes the format)"},
            "size": {"type": "integer",
                     "description": "Exact file size in bytes; the PUT is "
                                    "rejected the moment it exceeds it"},
            "content_type": {"type": "string",
                             "description": "Optional MIME hint"},
            "client_ref": {"type": "string",
                           "description": "Stable idempotency identity of "
                                          "this staging intent (same ref + "
                                          "same parameters returns the same "
                                          "unused ticket)"},
        },
        "required": ["filename", "size", "client_ref"],
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "upload_url": {"type": "string"},
            "expires_at": {"type": "integer"},
            "max_bytes": {"type": "integer"},
            "usage": {"type": "string"},
        },
        "required": ["upload_url", "expires_at", "max_bytes"],
    },
}


def _spec_summary(t: dict) -> dict:
    """name/title/description summary (tools/list + catalog_search share
    the exact same shape). English only — MCP is a machine surface."""
    names = mcp_names()
    label = t["label"]
    title = label.get("en") or t["id"]
    mn = int(t.get("files_min", 1) or 0)
    mx = int(t.get("files_max", 1) or 1)
    accept = t.get("accept")
    files_txt = (f"{mn}" if mn == mx else f"{mn}-{mx}") + " file(s)"
    if accept:
        files_txt += f", formats: {', '.join(accept)}"
    outs = ", ".join(t.get("outputs") or ["file"])
    desc = (resolve_label(t.get("desc"), "en") or title).rstrip(".")
    price = (f" Price: {int(t['cost'])} credits per call."
             if int(t["cost"]) > 0 else " Free.")
    return {
        "name": mcp_name(t["id"], names),
        "title": title,
        "description": f"{desc}. Inputs: {files_txt}. Outputs: {outs}."
                       f"{price}",
    }


# outputSchema shared by every tool (matches structuredContent exactly)
_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "call_id": {"type": "integer"},
        "replay": {"type": "boolean"},
        "result_url": {"type": "string",
                       "description": "Human-viewable result page (/r/{id})"},
        "expires_at": {"type": ["integer", "null"],
                       "description": "When the file links expire (epoch s)"},
        "items": {
            "type": "array", "maxItems": 20,
            "description": "Ordered result items",
            "items": {"oneOf": [
                {"type": "object",
                 "properties": {"kind": {"const": "text"},
                                "text": {"type": "string"}},
                 "required": ["kind", "text"]},
                {"type": "object",
                 "properties": {"kind": {"const": "json"},
                                "data": {}},
                 "required": ["kind", "data"]},
                {"type": "object",
                 "properties": {"kind": {"const": "file"},
                                "filename": {"type": "string"},
                                "content_type": {"type": "string"},
                                "size": {"type": "integer"},
                                "download_url": {"type": "string"},
                                "page_url": {"type": "string"}},
                 "required": ["kind", "filename", "content_type", "size",
                              "download_url", "page_url"]},
            ]},
        },
    },
    "required": ["call_id", "replay", "result_url", "items"],
}


def _tool_specs(user: dict) -> list:
    # Discovery-layer projection: schema enums show THIS bearer's real
    # entitlement (one access-projection read per _tool_specs call).
    # Not an authorization surface — tools/call still runs the full
    # authoritative chain regardless of what the schema advertised.
    from governance.access_projection import project
    ent = project(user["id"])["entitlements"]
    expiry_enum = list(ent["expiry_days"])
    downloads_enum = list(ent["download_limits"])
    specs = [_CREATE_UPLOAD_SPEC]
    for t in list_catalog(user["id"]):
        summary = _spec_summary(t)
        props = {
            "files": {
                "type": "array",
                "description": "Input files (optional for text-only tools): "
                               "each item is inline base64, an https URL, or "
                               "a reference to an existing upload",
                "minItems": int(t.get("files_min", 1) or 0),
                "maxItems": int(t.get("files_max", 1) or 1),
                "items": _FILE_ITEM_SCHEMA,
            },
        }
        required = ["client_ref"]
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
            required += sub_required
        else:
            props["params"] = {"type": "object", "description": "Tool-specific parameters"}
        props["delivery"] = {
            "type": "object",
            "description": "Result delivery policy (optional; defaults 1 day / 50 downloads)",
            "properties": {
                "expiry_days": {"type": "integer", "enum": expiry_enum},
                "allowed_downloads": {"type": "integer", "enum": downloads_enum},
            },
        }
        props["client_ref"] = {
            "type": "string",
            "description": "Required stable operation identity (max 128 chars): "
                           "generate once per logical operation and reuse the "
                           "same value to retry it safely",
        }
        specs.append({
            "name": summary["name"],
            "title": summary["title"],
            "description": summary["description"],
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
            "outputSchema": _RESULT_SCHEMA,
        })
    specs.append({
        "name": "catalog_search",
        "title": "Catalog Search",
        "description": "Find tools by keyword (id and labels, all "
                       "languages). Returns the same name/title/"
                       "description summary tools/list uses.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "tools": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"},
                                   "title": {"type": "string"},
                                   "description": {"type": "string"}},
                    "required": ["name", "title", "description"]}},
            },
            "required": ["query", "tools"],
        },
    })
    return specs


def _tool_result_error(mid, body: dict, client_ref: str, tool: dict | None = None):
    """Business denial as a tool result (isError): one plain-English
    sentence + the code + an actionable hint for the codes an agent can
    act on (format / count window / credits); anything else keeps the
    structured JSON shape (data the caller may need)."""
    code = body.get("error")
    if code in ("err_format_mismatch", "err_file_count",
                "err_insufficient_credits"):
        return _rpc(mid, {
            "content": [{"type": "text",
                         "text": _mcp_error_text(code, tool or {}, {})}],
            "isError": True,
        })
    return _rpc(mid, {
        "content": [{"type": "text",
                     "text": json.dumps({"error": code,
                                         "client_ref": client_ref,
                                         **{k: v for k, v in body.items()
                                            if k != "error"}})}],
        "isError": True,
    })


async def _call_tool(request: Request, user: dict, mid, params: dict):
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _rpc_err(mid, -32602, "invalid params: name required")
    args = params.get("arguments")
    if args is None:
        args = {}
    elif not isinstance(args, dict):
        return _rpc_err(mid, -32602, "invalid params: arguments must be an object")
    if name == "catalog_search":
        from governance.catalog import search_catalog, list_catalog
        q = str(args.get("q") or "").strip()[:64]
        hits = search_catalog(q, user["id"], limit=20) if q else []
        by_key = {t["id"]: t for t in list_catalog(user["id"])}
        summaries = [_spec_summary(by_key[h["id"]])
                     for h in hits if h["id"] in by_key]
        return _rpc(mid, {
            "content": [{"type": "text", "text": json.dumps(
                {"query": q, "tools": summaries}, ensure_ascii=False)}],
            "structuredContent": {"query": q, "tools": summaries},
            "isError": False,
        })

    if name == "create_upload":
        return _create_upload(user, mid, args, request)

    # client_ref is REQUIRED and validated BEFORE any base64 decode,
    # quota check, or Gokapi upload — a missing/invalid ref must never
    # leave the caller having uploaded anything.
    cr = args.get("client_ref")
    if not isinstance(cr, str) or not cr.strip() or len(cr) > 128:
        return _rpc_err(mid, -32602, "invalid client_ref")
    client_ref = cr.strip()

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

    # Source items: ("inline", (filename, ctype, body)) | ("url", (url,
    # filename|None)) | ("ref", (public_id, source_token)). Shape only —
    # no network, no storage.
    try:
        files_arg = args.get("files")
        if files_arg is None:
            files_arg = []          # text-only tools: 0 files is legal
        if not isinstance(files_arg, list):
            raise ValueError("files[] must be an array")
        items = [_parse_file_item(f) for f in files_arg]
    except (ValueError, binascii.Error) as exc:
        # Malformed input outranks business affordability — protocol
        # validation stays first so a low balance can never mask it.
        return _rpc_err(mid, -32602, f"invalid arguments: {exc}")

    # Platform files/request boundary (the count part of the single batch
    # validator; sizes are checked once the bytes are known).
    from governance.ingress import validate_batch as _vbatch
    _cdenial = _vbatch([0] * len(items))
    if _cdenial:
        return _rpc(mid, {
            "content": [{"type": "text", "text": json.dumps(_cdenial)}],
            "isError": True,
        })

    # Tool + delivery entitlement + stored contract + params — the SAME
    # pre-gate validators dispatch runs, here BEFORE any fetch, quota
    # claim or Gokapi upload.
    tool_key = tool_key_for_mcp_name(name)
    if tool_key is None:
        return _rpc_err(mid, -32602, f"unknown tool: {name}")
    from execution.dispatch import (preflight as _preflight,
                                    source_count_error as _count_err,
                                    source_accept_error as _accept_err)
    tool, _declared, _pbody, _pstatus = _preflight(user, tool_key, delivery,
                                                   tool_params)
    if _pbody is not None:
        if _pbody.get("error") == "tool_not_found":
            return _rpc_err(mid, -32602, f"unknown tool: {name}")
        return _tool_result_error(mid, _pbody, client_ref, tool)
    _cerr = _count_err(tool, len(items))
    if _cerr:
        return _tool_result_error(mid, {"error": _cerr},
                                   client_ref, tool)

    # Advisory credits affordability BEFORE source ingress: a clear
    # insufficiency answers as an application error (isError tool
    # result) without uploading anything. Authoritative consume stays
    # in the gate; this snapshot never reserves credits.
    from execution.affordability import check_affordability
    _denial = check_affordability(user["id"], tool)
    if _denial:
        return _rpc(mid, {
            "content": [{"type": "text", "text": json.dumps(_denial)}],
            "isError": True,
        })

    # References: ownership (DB owner or signed source token) resolved by
    # the execution-boundary resolver, before anything is ingested.
    from execution.source import resolve_upload
    from config import PUBLIC_ID_RE
    ref_names = {}
    for i, (kind, val) in enumerate(items):
        if kind != "ref":
            continue
        _pid, _tok = val
        if not PUBLIC_ID_RE.match(_pid):
            return _rpc_err(mid, -32602, "invalid arguments: invalid public_id")
        _up, _rerr = await resolve_upload(_pid, user, _tok)
        if _rerr or not _up:
            return _rpc_err(mid, -32602,
                            f"invalid arguments: source reference rejected: "
                            f"{_rerr or 'not_found'}")
        ref_names[i] = _up.get("file_name", "")

    # URL sources: fetched through the pinned SSRF-guarded transport.
    from remote import RemoteError
    uploads = {}
    for i, (kind, val) in enumerate(items):
        if kind == "inline":
            uploads[i] = val
        elif kind == "url":
            _url, _fname = val
            try:
                _body = await asyncio.to_thread(_fetch_url_source, _url)
            except RemoteError as exc:
                return _rpc_err(mid, -32602,
                                f"invalid arguments: url source rejected: {exc.code}")
            uploads[i] = _url_upload(_url, _fname, _body)

    ip = request.headers.get("X-Real-IP",
                             request.client.host if request.client else "").strip()

    # Ingress receipt: client_ref IS the request_ref. Same ref + same
    # validated content replays the stored source handles (zero new
    # Gokapi, zero new usage) so a response-loss retry flows into the
    # SAME ToolInvocation fingerprint → gate replay.
    import hashlib as _hl
    from sharing.ingress_identity import ingress_request_hash
    from sharing.ingress_receipt import (
        claim_ingress_quota as _claim_receipt,
        finalize_ok as _receipt_ok,
        settle_failure as _receipt_fail,
        resolve_outcome as _resolve_outcome,
        IngressConflict as _IngressConflict,
        IngressInProgress as _IngressInProgress,
        IngressQuotaDenied as _IngressQuotaDenied,
        IngressUnresolved as _IngressUnresolved,
        OUTCOME_DURABLE_OK, OUTCOME_PROVEN_ROLLBACK)
    from utils import sanitize_filename as _san
    from security import sniff_allowed as _sniff, sign_source_token
    _ingest_idx = sorted(uploads)
    _canon = []
    for i in _ingest_idx:
        _fn, _ct, _body = uploads[i]
        _ok_t, _final_t = _sniff(_fn, _ct, _body[:512])
        if not _ok_t:
            if _final_t.startswith("err_content_mismatch") \
                    or _final_t == "err_file_type_blocked":
                msg = _mcp_error_text("err_format_mismatch", tool, {})
                if tool.get("accept"):
                    msg = msg.replace(
                        " (code: err_format_mismatch).",
                        " (code: err_format_mismatch).")
                return _rpc_err(mid, -32602, msg)
            return _rpc_err(mid, -32602, f"upload rejected: {_final_t}")
        _canon.append(((_san(_fn), _final_t, _body)))
    _names = {**ref_names,
              **{i: c[0] for i, c in zip(_ingest_idx, _canon)}}
    _aerr = _accept_err(tool, [_names[i] for i in range(len(items))])
    if _aerr:
        return _tool_result_error(mid, {"error": _aerr},
                                   client_ref, tool)
    # Authoritative platform batch boundary (single validator for
    # Web/MCP) — BEFORE the receipt claim / quota / Gokapi.
    _bdenial = _vbatch([len(_b) for _, _, _b in _canon])
    if _bdenial:
        return _rpc(mid, {
            "content": [{"type": "text", "text": json.dumps(_bdenial)}],
            "isError": True,
        })

    infos = []
    if _canon:
        _ctx = {"tool_id": tool_key}
        if any(kind != "inline" for kind, _ in items):
            # URL / reference forms are part of the operation identity:
            # same client_ref + a different source list → conflict.
            _ctx["sources"] = [
                {"kind": "url", "url": val[0]} if kind == "url"
                else {"kind": "ref", "public_id": val[0]} if kind == "ref"
                else {"kind": "inline"}
                for kind, val in items]
        _req_hash = ingress_request_hash(
            "mcp_tool_source",
            [{"filename": _fn, "content_type": _ct, "size": len(_b),
              "sha256": _hl.sha256(_b).hexdigest()}
             for _fn, _ct, _b in _canon],
            _ctx)
        try:
            _receipt, _usage_ids, _replay = _claim_receipt(
                user["id"], ip, client_ref, _req_hash,
                [len(_b) for _, _, _b in _canon])
        except _IngressConflict:
            return _rpc_err(mid, ERR_IDEMPOTENCY, "idempotency_conflict")
        except _IngressInProgress:
            return _rpc_err(mid, ERR_IDEMPOTENCY, "err_upload_in_progress")
        except _IngressQuotaDenied as exc:
            return _rpc(mid, {
                "content": [{"type": "text", "text": json.dumps(exc.denial)}],
                "isError": True,
            })
        except _IngressUnresolved:
            # unresolvable durable replay — server-side error, zero side
            # effects; never leaks out of _call_tool
            return _rpc_err(mid, -32000, "err_ingress_unresolved")

        if _replay is not None:
            infos = _replay if isinstance(_replay, list) else [_replay]
        else:
            from storage import write as _gk_up
            from execution.pipeline import (INTERMEDIATE_TTL_DAYS as _ITTL,
                                            INTERMEDIATE_DOWNLOAD_LIMIT as _IDL)
            import io as _io
            _gids = []
            # Phase 1 — Gokapi staging. A staging failure is a KNOWN
            # rollback (objects half-uploaded, receipt still running) and
            # takes the generic cleanup below.
            try:
                for _fn, _ct, _body in _canon:
                    _gk = _gk_up(_io.BytesIO(_body), _fn, _ct, _ITTL, _IDL,
                                 len(_body))
                    _fid = (_gk.get("FileInfo", {}) or {}).get("Id", "")
                    if not _fid:
                        raise RuntimeError("gokapi_missing_id")
                    _gids.append(_fid)
                    infos.append({
                        "name": _fn, "filename": _fn, "contentType": _ct,
                        "size": len(_body), "downloadUrl": "",
                        "hotlinkUrl": _gk["FileInfo"].get("UrlHotlink", ""),
                        "publicId": _fid, "gokapiId": _fid,
                        "sourceToken": sign_source_token(user["id"], _fid),
                        "expireAt": _gk["FileInfo"].get("ExpireAt"),
                        "expiryDays": _ITTL, "allowedDownloads": _IDL,
                    })
            except Exception:
                _log.exception("mcp source staging failed")
                from storage import delete as _gk_del
                for _gid in _gids:
                    try:
                        _gk_del(_gid)
                    except Exception:
                        pass
                try:
                    _receipt_fail(int(_receipt["id"]), _usage_ids, user["id"])
                except Exception:
                    pass
                # server-side storage failure — not invalid arguments
                return _rpc_err(mid, -32603, "err_upload_failed")

            # Phase 2 — finalize with an EXPLICIT three-way outcome. The
            # unresolved branch must NEVER fall into generic cleanup: zero
            # Gokapi delete, zero quota release, zero receipt mutation.
            from database import db as _db
            try:
                with _db() as con:
                    _receipt_ok(con, int(_receipt["id"]), infos)
            except Exception:
                _out = _resolve_outcome(int(_receipt["id"]))
                if _out["outcome"] == OUTCOME_DURABLE_OK:
                    infos = (_out["result"] if isinstance(_out["result"], list)
                             else [_out["result"]])
                elif _out["outcome"] == OUTCOME_PROVEN_ROLLBACK:
                    # the ONLY finalize-driven cleanup path: receipt still
                    # running proves the success transaction never landed.
                    from storage import delete as _gk_del
                    for _gid in _gids:
                        try:
                            _gk_del(_gid)
                        except Exception:
                            pass
                    try:
                        _receipt_fail(int(_receipt["id"]), _usage_ids, user["id"])
                    except Exception:
                        pass
                    return _rpc_err(mid, -32603, "err_upload_failed")
                else:
                    # UNRESOLVED — server-side error, not invalid arguments
                    return _rpc_err(mid, -32000, "err_ingress_unresolved")

    # canonical source order = request order (ingested handles + references)
    _ingested = iter(infos)
    source_files = []
    for kind, val in items:
        if kind == "ref":
            source_files.append({"public_id": val[0], "source_token": val[1]})
        else:
            info = next(_ingested)
            source_files.append({"public_id": info["publicId"],
                                 "source_token": info.get("sourceToken", "")})

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
                         "text": _mcp_error_text(payload.get("error"),
                                                  tool, delivery)}],
            "isError": True,
        })
    result = payload.get("result") or {}
    return _rpc(mid, _mcp_success(result, payload))


def _mcp_error_text(code, tool: dict, delivery: dict) -> str:
    """One plain-English sentence + the code + an actionable hint."""
    code = str(code or "tool_failed")
    base = {
        "err_format_mismatch": "The file format is not supported by this tool.",
        "err_file_count": "The number of files is outside this tool's window.",
        "err_insufficient_credits": "Not enough credits for this call.",
        "tool_disabled": "This tool is currently disabled.",
        "err_rate_limited": "Too many requests; retry shortly.",
        "err_call_in_progress": "This operation is already running; retry the same client_ref for its result.",
        "idempotency_conflict": "This client_ref was used for different input; use a new client_ref.",
        "err_file_too_large": "A file exceeds the size limit.",
        "err_endpoint_failed": "The tool's service failed.",
        "err_endpoint_timeout": "The tool's service timed out.",
        "err_endpoint_blocked": "The tool's service endpoint is not allowed.",
        "tool_failed": "The tool failed to produce a result.",
    }.get(code)
    msg = base or "The call failed."
    hint = ""
    if code == "err_format_mismatch":
        accept = tool.get("accept") if isinstance(tool, dict) else None
        if accept:
            hint = f" Accepted formats: {', '.join(accept)}."
    elif code == "err_file_count":
        mn = int((tool or {}).get("files_min", 1) or 0)
        mx = int((tool or {}).get("files_max", 1) or 1)
        hint = f" This tool accepts {mn} to {mx} files."
    elif code == "err_insufficient_credits":
        hint = " Top up credits and retry with the same client_ref."
    return f"{msg} (code: {code}).{hint}"


def _mcp_success(result: dict, payload: dict) -> dict:
    """Build the MCP tools/call success envelope: content (the agent's
    direct result) + structuredContent (the machine shape, matching
    every tool's outputSchema)."""
    from config import BASE_URL
    items_in = result.get("items") or []
    content = []
    sc_items = []
    # expires_at: max file expiry (epoch s); None when no file items
    expires_at = None
    for it in items_in:
        if it.get("kind") == "text":
            content.append({"type": "text", "text": it["text"]})
            sc_items.append({"kind": "text", "text": it["text"]})
        elif it.get("kind") == "json":
            content.append({"type": "text",
                            "text": json.dumps(it["data"],
                                               ensure_ascii=False)})
            sc_items.append({"kind": "json", "data": it["data"]})
        elif it.get("kind") == "file":
            pid = it["publicId"]
            dl = f"{BASE_URL}/f/{pid}?download=1"
            page = f"{BASE_URL}/f/{pid}"
            ct = it.get("contentType") or "application/octet-stream"
            size = int(it.get("size") or 0)
            sc_items.append({"kind": "file",
                             "filename": it.get("filename"),
                             "content_type": ct, "size": size,
                             "download_url": dl, "page_url": page})
            if ct.startswith("image/") \
                    and ct.lower() != "image/svg+xml" and size <= 1_048_576:
                import base64 as _b64
                from gokapi import InternalDownloadUnavailable
                from storage import read as _storage_read
                try:
                    status, _h, stream = _storage_read(
                        it.get("gokapiId") or pid)
                    body = stream.read(1_048_577)
                    stream.close()
                    if status == 200 and len(body) <= 1_048_576:
                        content.append({
                            "type": "image",
                            "data": _b64.b64encode(body).decode(),
                            "mimeType": ct})
                        content.append({
                            "type": "resource_link", "uri": dl,
                            "name": it.get("filename") or pid})
                        continue
                except (InternalDownloadUnavailable, Exception):
                    pass
            content.append({"type": "resource_link", "uri": dl,
                            "name": it.get("filename") or pid,
                            "mimeType": ct, "size": size})
            exp = it.get("expireAt")
            if exp and (expires_at is None or int(exp) > expires_at):
                expires_at = int(exp)
    if not content:
        content.append({"type": "text", "text": "(no output items)"})
    result_url = result.get("resultUrl") or ""
    structured = {
        "call_id": payload.get("call_id"),
        "replay": bool(payload.get("replay")),
        "result_url": result_url,
        "expires_at": expires_at,
        "items": sc_items,
    }
    return {"content": content, "structuredContent": structured,
            "isError": False}


def _parse_file_item(f) -> tuple:
    """One files[] item → (kind, value). Exactly one source form per item;
    a missing optional field takes its default, an explicit invalid value
    is rejected."""
    if not isinstance(f, dict):
        raise ValueError("files[] items must be objects")
    forms = [k for k in ("b64", "url", "public_id") if k in f]
    if len(forms) != 1:
        raise ValueError("files[] items need exactly one of b64 | url | public_id")
    if forms[0] == "b64":
        return "inline", _decode_file(f.get("b64"), f.get("filename"))
    if forms[0] == "url":
        url = f.get("url")
        if (not isinstance(url, str) or not url.startswith("https://")
                or len(url) > MAX_SOURCE_URL_LEN):
            raise ValueError("files[].url must be an https URL")
        fname = f.get("filename")
        if "filename" in f and (not isinstance(fname, str) or not fname.strip()):
            raise ValueError("files[].filename must be a non-empty string")
        return "url", (url, fname)
    pid = f.get("public_id")
    token = f.get("source_token", "")
    if not isinstance(pid, str) or not pid:
        raise ValueError("files[].public_id must be a non-empty string")
    if not isinstance(token, str):
        raise ValueError("files[].source_token must be a string")
    return "ref", (pid, token)


def _create_upload(user: dict, mid, args: dict, request: Request):
    """Meta tool: issue one one-time PUT credential (B4b two-stage
    upload). Official + free — no tool_calls row, no consume; the daily
    ingress quota is only touched by the PUT itself. Every validation
    (size bound, quota headroom) runs BEFORE a ticket is issued."""
    from utils import sanitize_filename
    filename = args.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return _rpc_err(mid, -32602, "invalid params: filename required")
    filename = sanitize_filename(filename.strip())
    if not filename:
        return _rpc_err(mid, -32602, "invalid params: filename invalid")
    size = args.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return _rpc_err(mid, -32602, "invalid params: size must be a "
                                     "positive integer byte count")
    if size > MAX_UPLOAD_BYTES:
        return _rpc_err(mid, -32602, "invalid params: size exceeds the "
                                     f"per-file limit ({MAX_UPLOAD_BYTES})")
    content_type = args.get("content_type")
    if content_type is not None and (not isinstance(content_type, str)
                                     or not content_type.strip()):
        return _rpc_err(mid, -32602, "invalid params: content_type must "
                                     "be a non-empty string")
    content_type = (content_type or "").strip() or None
    cr = args.get("client_ref")
    if not isinstance(cr, str) or not cr.strip() or len(cr) > 128:
        return _rpc_err(mid, -32602, "invalid client_ref")
    client_ref = cr.strip()

    # read-only quota headroom check BEFORE issuing (single decision
    # point; the PUT still runs the authoritative claim)
    from sharing.uploads import check_quota
    ok, denial = check_quota(user["id"], "", size, 1)
    if not ok:
        return _rpc_err(mid, -32602, "upload quota exceeded", denial)

    from repo import upload_tickets as tickets
    from utils import now
    try:
        row = tickets.issue(user["id"], filename, content_type, size,
                            client_ref, now())
    except ValueError as exc:
        return _rpc_err(mid, ERR_IDEMPOTENCY, str(exc))
    return _rpc(mid, {
        "content": [{"type": "text", "text": json.dumps({
            "upload_url": f"{BASE_URL}/mcp/upload/{row['ticket']}",
            "expires_at": int(row["expires_at"]),
            "max_bytes": MAX_UPLOAD_BYTES,
            "usage": "PUT the raw file bytes to upload_url with NO "
                     "Authorization header (the URL is the one-time "
                     "credential; exact declared size, 10-minute expiry, "
                     "single use). The JSON response gives {public_id, "
                     "source_token} — use them as the files[] reference "
                     "form in tools/call.",
        }, ensure_ascii=False)}],
        "isError": False,
    })


def _fetch_url_source(url: str) -> bytes:
    """GET one https source over the pinned SSRF-guarded transport
    (every hop: https only, all resolved addresses public, connect pinned
    to a validated address; redirects re-validated). Body cap = the
    per-file ingress cap."""
    from remote import RemoteError, pinned_request
    status, data = pinned_request(url, method="GET",
                                  timeout_s=URL_FETCH_TIMEOUT_S,
                                  max_bytes=MAX_UPLOAD_BYTES)
    if status != 200:
        raise RemoteError("http_status", str(status))
    if len(data) > MAX_UPLOAD_BYTES:
        raise RemoteError("too_large", f"over {MAX_UPLOAD_BYTES} bytes")
    if not data:
        raise RemoteError("empty", "empty body")
    return data


def _url_upload(url: str, filename, body: bytes) -> tuple:
    """(filename, ctype, body) for a fetched URL source — same shape as an
    inline file; the name comes from the caller or the URL path."""
    import os
    if not filename:
        path = urllib.parse.urlsplit(url).path
        filename = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    fn = sanitize_filename(str(filename or "download"))
    if not os.path.splitext(fn)[1]:
        from remote import _sniff_ext
        fn += _sniff_ext(body[:64]) or ""
    return fn, _guess_ctype(fn), body


def _decode_file(b64, filename) -> tuple:
    if not b64 or not filename:
        raise ValueError("files[] items need b64 and filename")
    if not isinstance(b64, str) or not isinstance(filename, str):
        raise ValueError("files[] b64 and filename must be strings")
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
