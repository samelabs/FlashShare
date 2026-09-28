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
from utils import format_bytes, sanitize_filename

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
        from config import BASE_URL as _base
        return JSONResponse(
            {"error": "unauthorized",
             "message": (
                 "Missing or invalid API key. Create one at "
                 f"{_base}/account/keys; guide: {_base}/llms.txt")},
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
        # The caller's real account limits, from the same entitlement
        # projection the delivery schemas advertise (one read).
        from governance.access_projection import project
        ent = project(user["id"])["entitlements"]
        limits_txt = (
            "Your account limits: "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB per input file, up "
            f"to {ent['ingress_files_24h']} files and "
            f"{format_bytes(ent['ingress_bytes_24h'])} total per rolling "
            "24 hours; your delivery options: expiry_days "
            f"{list(ent['expiry_days'])}, allowed_downloads "
            f"{list(ent['download_limits'])}."
        )
        return _rpc(mid, {
            "protocolVersion": version,
            # no server→client stream exists, so no list_changed notifications
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "518plus", "version": PRODUCT_VERSION},
            "instructions": (
                "518.PLUS makes it easy to share files and AI "
                "capabilities. To give a person a link to a file: call "
                "share; hand them result_url (one page for all files) "
                "— page_url is per file, download_url is a direct "
                "download. tools/list is your working set: the "
                "first-party tools, your own tools and your favorites "
                "(我的收藏). Find any other tool with catalog_search and "
                "call it by name; pin_tool adds one to your favorites. "
                "Pass "
                "files inline (base64), by https URL, or by reference; "
                "for large local files call create_upload first and "
                "PUT the raw bytes to the one-time URL it returns, "
                "then use the returned {public_id, source_token} as "
                "the files[] reference form (staged files are "
                "intermediates: PUT within 10 minutes, kept at most "
                "1 day, not listed by list_uploads). Text-only tools "
                "take no files — just params. Every tools/call needs "
                "a client_ref: reuse the same value to retry safely "
                "(replays return the original result without "
                "re-charging). Results come back directly: "
                "structuredContent.items holds ordered text/json/file "
                "items with download_url for each file (content also "
                "carries a text block with the JSON serialization of "
                "structuredContent); result_url is a human-viewable "
                "page. Manage your live shared files with the free "
                "meta tools list_uploads (paginated) and "
                "delete_upload (remove one by public_id — same effect "
                "as deleting on the web). Calls are metered in "
                "credits (1000 credits = 1 USD); failed calls are "
                "refunded automatically. "
                f"{limits_txt} Catalog version: "
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
        "exact declared size bound. The filename extension must already "
        "be a format 518 accepts — the same check the PUT runs. After a "
        "successful PUT you receive {public_id, source_token} — pass them "
        "as the files[] reference form of any tool. Staged files are "
        "intermediates, not shares: use the PUT within 10 minutes, the "
        "stored copy lives at most 1 day, and it never appears in "
        "list_uploads and cannot be delete_upload'ed — only its "
        "public_id/source_token reference matters. Free; consumes your "
        "daily upload quota only when the PUT succeeds.",
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


# Meta tool: list the caller's still-live staged/uploads files (free).
_LIST_UPLOADS_SPEC = {
    "name": "list_uploads",
    "title": "List Uploads",
    "description":
        "List YOUR still-live shared files on 518 — files you shared or "
        "that tool calls produced for you (create_upload staging is NOT "
        "listed: staged intermediates live at most 1 day and are "
        "referenced only by public_id). Newest first; each entry carries "
        "public_id, filename, size, content_type, expires_at (epoch "
        "seconds), downloads_remaining, page_url (human page) and "
        "download_url (direct download). Use delete_upload with "
        "public_id to remove one. Free; paginated (20 per page, pass "
        "offset).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "offset": {"type": "integer",
                       "description": "Pagination offset (default 0, "
                                      "page size 20)"},
        },
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "uploads": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "public_id": {"type": "string"},
                    "filename": {"type": "string"},
                    "size": {"type": "integer"},
                    "content_type": {"type": "string"},
                    "expires_at": {"type": "integer"},
                    "downloads_remaining": {"type": ["integer", "null"]},
                    "page_url": {"type": "string"},
                    "download_url": {"type": "string"},
                },
                "required": ["public_id", "filename", "size",
                             "content_type", "expires_at",
                             "page_url", "download_url"],
            }},
            "has_more": {"type": "boolean"},
        },
        "required": ["uploads", "has_more"],
    },
}

# Meta tool: delete one of the caller's own files by public_id (free).
_DELETE_UPLOAD_SPEC = {
    "name": "delete_upload",
    "title": "Delete Upload",
    "description":
        "Delete ONE of your own files by public_id. Same effect as "
        "deleting on the web: the stored file and its page mapping are "
        "removed together — the /f/{public_id} page 404s immediately. "
        "Only your own files; references to the file from earlier "
        "results stop resolving. Free.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "public_id": {"type": "string",
                          "description": "The file's public id "
                                         "(from list_uploads or a "
                                         "tools/call result item)"},
        },
        "required": ["public_id"],
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "deleted": {"type": "boolean"},
            "public_id": {"type": "string"},
        },
        "required": ["deleted", "public_id"],
    },
}


def _pin_spec(name: str, title: str, description: str) -> dict:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {"name": {
                "type": "string",
                "description": "The tool's MCP name (from catalog_search "
                               "or tools/list)"}},
            "required": ["name"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "favorite": {"type": "boolean"}},
            "required": ["name", "favorite"],
        },
    }


_PIN_TOOL_SPEC = _pin_spec(
    "pin_tool", "Add to My Favorites",
    "Add a tool to your favorites (我的收藏). tools/list carries the "
    "first-party tools, your own tools and your favorites; any other "
    "tool stays callable by name after catalog_search. Pin the tools you "
    "reuse, then call tools/list again to see them. Free.")
_UNPIN_TOOL_SPEC = _pin_spec(
    "unpin_tool", "Remove from My Favorites",
    "Remove a tool from your favorites. It stays callable by name. Free.")


def _spec_summary(t: dict, names: dict | None = None) -> dict:
    """name/title/description summary (tools/list + catalog_search share
    the exact same shape). English only — MCP is a machine surface.
    `names` = a precomputed mcp_names() map — computing it per tool made
    tools/list O(tools²) DB+CPU; every caller passes one shared map."""
    names = names if names is not None else mcp_names()
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
    # community tools carry an explicit third-party marker so an agent
    # reading the description knows the party it is calling; first-party
    # tools (no owner) stay unannotated
    author = (f" Third-party tool by @{t['owner_handle']}."
              if t.get("owner_handle") else "")
    return {
        "name": mcp_name(t["id"], names),
        "title": title,
        "description": f"{desc}.{author} Inputs: {files_txt}."
                       f" Outputs: {outs}.{price}",
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


def _catalog_spec(t: dict, names: dict, ent: dict) -> dict:
    """One catalog item → THE tool spec. tools/list and catalog_search
    share this exact constructor (name/title/description/inputSchema/
    outputSchema/annotations), so a search hit is directly callable
    without listing the tool first.

    `names` = a shared mcp_names() map; `ent` = the calling bearer's
    entitlements projection (delivery enums) — the discovery layer
    advertises THIS caller's real values; tools/call still runs the
    authoritative chain regardless of what the schema advertised."""
    summary = _spec_summary(t, names)
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
    if int(t.get("files_min", 1) or 0) >= 1:
        required.append("files")
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
        # required parameter NAMES live at params.required — the
        # top level requires params itself when any is mandatory,
        # never the individual names.
        if sub_required:
            required.append("params")
    else:
        props["params"] = {"type": "object", "description": "Tool-specific parameters"}
    props["delivery"] = {
        "type": "object",
        "description": "Result delivery policy (optional; defaults 1 day / 50 downloads)",
        "properties": {
            "expiry_days": {"type": "integer", "enum": list(ent["expiry_days"])},
            "allowed_downloads": {"type": "integer", "enum": list(ent["download_limits"])},
        },
    }
    props["client_ref"] = {
        "type": "string",
        "description": "Required stable operation identity (max 128 chars): "
                       "generate once per logical operation and reuse the "
                       "same value to retry it safely",
    }
    return {
        "name": summary["name"],
        "title": summary["title"],
        "description": summary["description"],
        "annotations": {"title": summary["title"]},
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
        },
        "outputSchema": _RESULT_SCHEMA,
    }


def _tool_specs(user: dict) -> list:
    # Discovery-layer projection: schema enums show THIS bearer's real
    # entitlement (one access-projection read per _tool_specs call).
    # Not an authorization surface — tools/call still runs the full
    # authoritative chain regardless of what the schema advertised.
    from governance.access_projection import project
    ent = project(user["id"])["entitlements"]
    specs = [_CREATE_UPLOAD_SPEC, _LIST_UPLOADS_SPEC,
             _DELETE_UPLOAD_SPEC, _PIN_TOOL_SPEC, _UNPIN_TOOL_SPEC]
    _names = mcp_names()
    # Working set, not an access boundary: first-party + own + favorites;
    # everything else is found via catalog_search and called by name.
    from governance.favorites import mcp_working_set
    for t in mcp_working_set(user, list_catalog(user["id"])):
        specs.append(_catalog_spec(t, _names, ent))
    specs.append({
        "name": "catalog_search",
        "title": "Catalog Search",
        "description": "Find public tools by keyword: every whitespace-"
                       "separated term must match the tool's id, name or "
                       "description (all AND; % and _ match literally). An "
                       "empty query browses the most-used public tools. "
                       "Each result carries the exact spec tools/list "
                       "shows (inputSchema included) plus favorite — "
                       "call it by name right away; pin_tool adds it to "
                       "your working set.",
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
                    "properties": {
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "inputSchema": {"type": "object"},
                        "outputSchema": {"type": "object"},
                        "annotations": {"type": "object"},
                        "favorite": {"type": "boolean"}},
                    "required": ["name", "title", "description",
                                 "inputSchema", "outputSchema",
                                 "favorite"]}},
            },
            "required": ["query", "tools"],
        },
    })
    return specs


def _catalog_search_result(user: dict, q: str) -> dict:
    """catalog_search result payload: public search hits projected to the
    exact callable spec tools/list carries (shared _catalog_spec
    constructor) plus the caller's favorite flag — one projection read
    for this bearer's delivery enums, kept OUT of the _call_tool
    dispatch path (the call path reads no projection)."""
    from governance.catalog import search_catalog
    from governance.favorites import favorite_keys
    from governance.access_projection import project
    hits = search_catalog(q, limit=20)
    favs = set(favorite_keys(user["id"]))
    _names = mcp_names()
    ent = project(user["id"])["entitlements"]
    tools = []
    for h in hits:
        spec = _catalog_spec(h, _names, ent)
        spec["favorite"] = h["id"] in favs
        tools.append(spec)
    return {"query": q, "tools": tools}


def _tool_result_error(mid, body: dict, client_ref: str, tool: dict | None = None):
    """Business denial as a tool result (isError): the unified
    single-JSON-block envelope — error code + one English sentence
    (from the single human-text map) + client_ref + every structured
    field the denial carried (delivery entitlement answers keep
    field/requested/allowed so the agent can self-correct)."""
    return _iserror(mid, body, client_ref, tool)


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
        # Search IS the public market predicate (same predicate/order/
        # projection as the home selector and /tools); empty q browses
        # the most-used public tools. See _catalog_search_result.
        q = str(args.get("q") or "").strip()[:64]
        return _meta_result(mid, _catalog_search_result(user, q))

    if name == "create_upload":
        return _create_upload(user, mid, args, request)
    if name == "list_uploads":
        return _list_uploads(user, mid, args)
    if name == "delete_upload":
        return _delete_upload(user, mid, args)
    if name in ("pin_tool", "unpin_tool"):
        return _pin_tool(user, mid, args, on=(name == "pin_tool"))

    # client_ref is REQUIRED and validated BEFORE any base64 decode,
    # quota check, or Gokapi upload — a missing/invalid ref must never
    # leave the caller having uploaded anything.
    cr = args.get("client_ref")
    if not isinstance(cr, str) or not cr.strip() or len(cr) > 128:
        return _rpc_err(mid, -32602, "invalid client_ref")
    client_ref = cr.strip()

    from governance.delivery import (validate_delivery, DEFAULT_EXPIRY_DAYS,
                                     DEFAULT_ALLOWED_DOWNLOADS,
                                     EXPIRY_DAYS, DOWNLOAD_LIMITS)
    if "delivery" in args:
        d = args.get("delivery")
        _bad = (not isinstance(d, dict)
                or bool(set(d.keys()) - {"expiry_days",
                                         "allowed_downloads"}))
        delivery = None
        if not _bad:
            try:
                delivery = validate_delivery({
                    "expiry_days": d.get("expiry_days", DEFAULT_EXPIRY_DAYS),
                    "allowed_downloads": d.get("allowed_downloads",
                                               DEFAULT_ALLOWED_DOWNLOADS)})
            except ValueError:
                _bad = True
        if _bad:
            # Input contract violation → isError tool result (never a
            # JSON-RPC protocol error); legal values come from the one
            # delivery authority, never re-copied.
            return _meta_result(mid, {
                "ok": False, "error": "invalid_delivery",
                "legal_values": {
                    "expiry_days": list(EXPIRY_DAYS),
                    "allowed_downloads": list(DOWNLOAD_LIMITS)},
                "message": (
                    "delivery must be an object with optional integer "
                    f"fields expiry_days (one of {list(EXPIRY_DAYS)}) "
                    "and allowed_downloads (one of "
                    f"{list(DOWNLOAD_LIMITS)})."),
            }, isError=True)
        # entitlement denial (valid values, above the plan tier) keeps
        # the existing structured denial → isError path below via
        # _preflight; nothing to do here.
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
        if "files" in args:
            files_arg = args.get("files")
            if files_arg is None:
                # explicit null is NOT a missing key — same rejection as
                # params:null (the adapter defaults only what is absent)
                return _rpc_err(mid, -32602, "invalid files")
        else:
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
            return _tool_result_error(mid, {"error": "invalid_id"},
                                      client_ref, tool)
        _up, _rerr = await resolve_upload(_pid, user, _tok)
        if _rerr or not _up:
            return _tool_result_error(
                mid, {"error": _rerr or "not_found"}, client_ref, tool)
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
                return _tool_result_error(mid, {"error": exc.code},
                                          client_ref, tool)
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
            # Pre-execution input errors are isError tool results — the
            # SAME expression as the accept gate below (IO-1 rule: a
            # well-formed request carrying a bad file is domain-level
            # and agent-fixable; -32602 stays for malformed request
            # shape only).
            code = ("err_format_mismatch"
                    if _final_t.startswith("err_content_mismatch")
                    or _final_t == "err_file_type_blocked" else _final_t)
            return _tool_result_error(mid, {"error": code},
                                      client_ref, tool)
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
        return _iserror(mid, _bdenial, client_ref, tool)

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
                [len(_b) for _, _, _b in _canon], scope=tool_key)
        except _IngressConflict:
            return _rpc_err(mid, ERR_IDEMPOTENCY, "idempotency_conflict")
        except _IngressInProgress:
            return _rpc_err(mid, ERR_IDEMPOTENCY, "err_upload_in_progress")
        except _IngressQuotaDenied as exc:
            return _iserror(mid, exc.denial, client_ref)
        except _IngressUnresolved:
            # unresolvable durable replay — server-side error, zero side
            # effects; never leaks out of _call_tool
            return _rpc_err(mid, -32000, "err_ingress_unresolved")

        if _replay is not None:
            infos = _replay if isinstance(_replay, list) else [_replay]
        else:
            from storage import write as _gk_up
            from governance.delivery import (INTERMEDIATE_TTL_DAYS as _ITTL,
                                            INTERMEDIATE_DOWNLOAD_LIMIT as _IDL)
            import io as _io
            _gids = []
            # Phase 1 — Gokapi staging. A staging failure is a KNOWN
            # rollback (objects half-uploaded, receipt still running) and
            # takes the generic cleanup below.
            try:
                for _fn, _ct, _body in _canon:
                    _gk = await asyncio.to_thread(
                        _gk_up, _io.BytesIO(_body), _fn, _ct, _ITTL, _IDL,
                        len(_body))
                    _fid = (_gk.get("FileInfo", {}) or {}).get("Id", "")
                    if not _fid:
                        raise RuntimeError("gokapi_missing_id")
                    _gids.append(_fid)
                    infos.append({
                        "name": _fn, "filename": _fn, "contentType": _ct,
                        "size": len(_body),
                        "stagingId": _fid,
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
            source_files.append({"public_id": info["stagingId"],
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
        return _iserror(mid, payload, client_ref, tool)
    result = payload.get("result") or {}
    _input_digests = frozenset(
        _hl.sha256(_b).hexdigest() for _, _, _b in _canon)
    return _rpc(mid, await _mcp_success(result, payload, _input_digests))


def _error_message(code, tool: dict | None = None) -> str:
    """THE single human-text map: one plain-English sentence + an
    actionable hint for the codes an agent can act on. Unknown codes
    fall back to the generic sentence."""
    code = str(code or "tool_failed")
    base = {
        "err_format_mismatch": "The file format is not supported by this tool.",
        "err_format_not_supported": "The file format is not accepted by 518.",
        "err_file_type_blocked": "This file type is blocked on 518.",
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
        "err_expiry_not_entitled": "The requested delivery.expiry_days is not available on your current plan.",
        "err_download_limit_not_entitled": "The requested delivery.allowed_downloads is not available on your current plan.",
        "tool_failed": "The tool failed to produce a result.",
        "not_found": "No such file of yours.",
        "tool_not_found": "No such tool is available to you. Use catalog_search to find the exact name.",
        "invalid_id": "That public id is malformed.",
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
    return f"{msg}{hint}"


def _iserror(mid, payload: dict, client_ref: str | None = None,
             tool: dict | None = None) -> dict:
    """The ONE isError tools/call envelope: a single text block whose
    text is JSON — {"error": code, "message": one English sentence
    (from _error_message, the single human-text map), "client_ref": ...,
    ...every structured field the denial already carried}. Existing
    fields are never dropped."""
    code = payload.get("error") or "tool_failed"
    body = {"error": code, "message": _error_message(code, tool)}
    if client_ref is not None:
        body["client_ref"] = client_ref
    body.update({k: v for k, v in payload.items()
                 if k not in ("error", "message", "client_ref", "ok")})
    return _rpc(mid, {
        "content": [{"type": "text",
                     "text": json.dumps(body, ensure_ascii=False)}],
        "isError": True,
    })


def _pin_tool(user: dict, mid, args: dict, on: bool) -> dict:
    from governance.favorites import set_favorite
    name = str(args.get("name") or "").strip()
    tool_key = tool_key_for_mcp_name(name) if name else None
    err = set_favorite(user["id"], tool_key or "", on) if tool_key else "tool_not_found"
    if err:
        return _iserror(mid, {"error": err, "name": name})
    return _meta_result(mid, {"name": name, "favorite": on})


def _meta_result(mid, payload: dict, isError: bool = False) -> dict:
    """The ONE envelope builder for meta tools (and any response that
    carries structuredContent without running the execution chain):
    content always carries a text block with the JSON serialization of
    structuredContent — the MCP-spec backward-compat form every
    tools/call response now shares (create_upload, catalog_search,
    list_uploads, delete_upload and _mcp_success all route through
    this shape)."""
    return _rpc(mid, {
        "content": [{"type": "text",
                     "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": isError,
    })


async def _mcp_success(result: dict, payload: dict,
                       input_digests: frozenset | None = None) -> dict:
    """Build the MCP tools/call success envelope: content (the agent's
    direct result) + structuredContent (the machine shape, matching
    every tool's outputSchema).

    input_digests = sha256 set of THIS call's input file bodies. An
    image result byte-identical to an input (a pass-through tool echoing
    what the agent just sent) gets no image block — only the
    resource_link — so the agent's own bytes never bounce back into its
    context. General rule, no per-tool branching."""
    from config import BASE_URL
    import hashlib as _hl
    input_digests = input_digests or frozenset()
    items_in = result.get("items") or []
    content = []
    sc_items = []
    # Result expiry is the record's delivery-owned expires_at — every
    # result shape carries one (text/json-only results expire with the
    # delivery window; file results inherit the latest file expiry).
    expires_at = result.get("expires_at")
    if expires_at is not None:
        expires_at = int(expires_at)
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
            # the record is the single snake_case source; addresses come
            # from the one builder
            pid = it["public_id"]
            dl = it["download_url"]
            ct = it.get("content_type") or "application/octet-stream"
            size = int(it.get("size") or 0)
            sc_items.append({"kind": "file",
                             "filename": it.get("filename"),
                             "content_type": ct, "size": size,
                             "download_url": dl, "page_url": it["page_url"]})
            if ct.startswith("image/") \
                    and ct.lower() != "image/svg+xml" and size <= 1_048_576:
                import base64 as _b64
                from gokapi import InternalDownloadUnavailable
                from storage import read as _storage_read
                from repo.uploads import by_public_id as _up_by_pid
                from database import db as _db
                try:
                    with _db() as _c:
                        _u = _up_by_pid(_c, pid)
                        _gid = _u["gokapi_id"] if _u else pid
                    status, _h, stream = await asyncio.to_thread(
                        _storage_read, _gid)
                    body = stream.read(1_048_577)
                    stream.close()
                    if status == 200 and len(body) <= 1_048_576 \
                            and _hl.sha256(body).hexdigest() not in input_digests:
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
    if not content:
        content.append({"type": "text", "text": "(no output items)"})
    result_url = result.get("result_url") or ""
    structured = {
        "call_id": payload.get("call_id"),
        "replay": bool(payload.get("replay")),
        "result_url": result_url,
        "expires_at": expires_at,
        "items": sc_items,
    }
    # MCP-spec backward-compat: a text block carrying the JSON
    # serialization of structuredContent rides along with the item
    # blocks (image previews / resource_links stay untouched).
    content.append({"type": "text",
                    "text": json.dumps(structured, ensure_ascii=False)})
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

    # format pre-check with the SAME single-point word lists the PUT
    # runs (sniff_allowed's extension part: BLOCKED_EXT / ALLOWED_EXT —
    # no second vocabulary): a bad extension must not consume a ticket
    # and only surface after the agent has shipped the bytes.
    import os as _os
    from config import ALLOWED_EXT as _ALLOWED, BLOCKED_EXT as _BLOCKED
    _ext = _os.path.splitext(filename.lower())[1]
    if _ext in _BLOCKED:
        return _iserror(mid, {"error": "err_file_type_blocked"}, client_ref)
    if _ext not in _ALLOWED:
        return _iserror(mid, {"error": "err_format_not_supported"},
                        client_ref)

    # read-only quota headroom check BEFORE issuing (single decision
    # point; the PUT still runs the authoritative claim)
    from sharing.uploads import check_quota
    ok, denial = check_quota(user["id"], "", size, 1)
    if not ok:
        return _iserror(mid, denial, client_ref)

    from repo import upload_tickets as tickets
    from utils import now
    try:
        row = tickets.issue(user["id"], filename, content_type, size,
                            client_ref, now())
    except ValueError as exc:
        return _rpc_err(mid, ERR_IDEMPOTENCY, str(exc))
    _payload = {
        "upload_url": f"{BASE_URL}/mcp/upload/{row['ticket']}",
        "expires_at": int(row["expires_at"]),
        "max_bytes": MAX_UPLOAD_BYTES,
        "usage": "PUT the raw file bytes to upload_url with NO "
                 "Authorization header (the URL is the one-time "
                 "credential; exact declared size, 10-minute expiry, "
                 "single use). The JSON response gives {public_id, "
                 "source_token} — use them as the files[] reference "
                 "form in tools/call.",
    }
    return _meta_result(mid, _payload)


def _list_uploads(user: dict, mid, args: dict) -> dict:
    """list_uploads meta tool: the SAME single point the web GET
    /api/uploads uses (sharing.list_uploads) — projection, live
    download counters and Gokapi mapping stay one implementation."""
    from config import BASE_URL
    from sharing.uploads import list_uploads as _web_list
    offset = args.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = 0
    rows = _web_list(user["id"], offset=offset, limit=20)
    uploads = [{
        "public_id": r["public_id"],
        "filename": r["file_name"],
        "size": int(r.get("size_bytes") or 0),
        "content_type": r.get("content_type") or "application/octet-stream",
        "expires_at": int(r["expire_at"]) if r.get("expire_at") else None,
        "downloads_remaining": (int(r["downloads_remaining"])
                                if r.get("downloads_remaining") is not None
                                else None),
        "page_url": f"{BASE_URL}/f/{r['public_id']}",
        "download_url": f"{BASE_URL}/f/{r['public_id']}?download=1",
    } for r in rows]
    return _meta_result(mid, {"uploads": uploads,
                              "has_more": len(rows) == 20})


def _delete_upload(user: dict, mid, args: dict) -> dict:
    """delete_upload meta tool: resolves public_id → ownership the same
    way the web DELETE /api/uploads/{public_id} route does, then rides
    the SAME sharing.delete_upload single point (Gokapi physical file +
    518 mapping in one call — /f/{public_id} 404s immediately after)."""
    from config import PUBLIC_ID_RE
    from sharing.uploads import (get_upload_by_public_id,
                                 delete_upload as _web_delete)
    public_id = str(args.get("public_id") or "")
    if not PUBLIC_ID_RE.match(public_id):
        return _iserror(mid, {
            "deleted": False, "public_id": public_id,
            "error": "invalid_id"})
    upload = get_upload_by_public_id(public_id)
    if not upload or upload["user_id"] != user["id"]:
        # unknown OR someone else's: one shape, no existence oracle
        return _iserror(mid, {
            "deleted": False, "public_id": public_id,
            "error": "not_found"})
    _web_delete(upload["id"], user["id"])
    return _meta_result(mid, {"deleted": True, "public_id": public_id})


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
