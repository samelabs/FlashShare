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
from governance.catalog import list_catalog, resolve as _catalog_resolve
from governance.tool_params import resolve_label
from utils import MAX_LIST_OFFSET, format_bytes, sanitize_filename
from app.web_auth import client_ip

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
# Application-layer cap on one /mcp request body (inline base64 sources
# are the only payload that can grow unbounded). Larger files must go
# through the create_upload two-stage path — the rejection message says so.
MCP_MAX_BODY_BYTES = 16 * 1024 * 1024
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


async def _read_body_limited(request: Request) -> bytes | None:
    """Read the /mcp body under MCP_MAX_BODY_BYTES; None when it exceeds
    the cap. Content-Length is checked first (reject without reading);
    the stream is then read with a cumulative count so a request WITHOUT
    Content-Length (chunked) is stopped at the first overflow byte."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MCP_MAX_BODY_BYTES:
                return None
        except ValueError:
            pass  # malformed length: the stream count below still decides
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MCP_MAX_BODY_BYTES:
            return None
    return bytes(buf)


def _body_too_large(cors: dict) -> JSONResponse:
    return JSONResponse(
        _rpc_err(None, -32600,
                 "request body too large; upload large files with create_upload"),
        status_code=413, headers=cors)


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


def _auth_challenge_json(mid):
    """401 for an authenticated-only tools/call: JSON-RPC error body +
    the spec challenge header (connectors start OAuth from it)."""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        _rpc_err(mid, -32001, "unauthorized: a bearer credential is "
                 "required (fsk_ API key via the free 'register' tool, "
                 "or OAuth — see /.well-known/oauth-authorization-server)",
                 {"register": "call the register tool without an "
                  "Authorization header"}),
        status_code=401,
        headers={"WWW-Authenticate": _challenge_header()})


def _bearer_user(request: Request):
    """The /mcp caller when authenticated by a bearer credential:
    an fsk_ API key (token) or a consent-granted OAuth access token
    (oauth). Both carry their capability set on request.state."""
    user = getattr(request.state, "user", None)
    via = getattr(request.state, "auth_via", "")
    return user if (user and via in ("token", "oauth")) else None


def _challenge_header(error: str | None = None) -> str:
    """THE WWW-Authenticate builder: RFC 9728 resource_metadata (how
    connectors auto-start OAuth); RFC 6750 error code only when a token
    was presented and rejected."""
    from config import BASE_URL as _base
    ch = f'Bearer resource_metadata="{_base}' \
         '/.well-known/oauth-protected-resource"'
    if error:
        ch = f'Bearer error="{error}", ' + ch[len("Bearer "):]
    return ch


def _auth_challenge(cors: dict) -> JSONResponse:
    """Presented-but-invalid bearer → spec-canonical 401: the challenge
    names invalid_token; the body still names the register path."""
    from config import BASE_URL as _base
    return JSONResponse(
        {"error": "unauthorized",
         "message": (
             "A bearer credential is required: an fsk_ API key (get one "
             "from the free MCP tool 'register', call it without an "
             "Authorization header; humans can create keys at "
             f"{_base}/account/keys) or an OAuth access token "
             f"({_base}/.well-known/oauth-authorization-server). "
             f"Guide: {_base}/llms.txt")},
        status_code=401,
        headers={**cors,
                 "WWW-Authenticate": _challenge_header("invalid_token")})


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
    # No credentials = anonymous discovery: initialize / tools/list
    # answer, and the `register` tool hands the agent its key in-band.
    # A presented-but-invalid key is still a hard 401 (a real error the
    # caller must see, never silently downgraded to anonymous).
    if not user and request.headers.get("authorization"):
        return _auth_challenge(cors)
    version = request.headers.get("mcp-protocol-version")
    version = HEADER_DEFAULT_VERSION if version is None else version.strip()
    if version not in SUPPORTED_VERSIONS:
        return JSONResponse(
            _rpc_err(None, -32600, "unsupported MCP-Protocol-Version",
                     {"supported": list(SUPPORTED_VERSIONS)}),
            status_code=400, headers=cors)
    body = await _read_body_limited(request)
    if body is None:
        return _body_too_large(cors)
    try:
        msg = json.loads(body)
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
        passthrough = None
        for m in msg:
            r = await _dispatch_safe(request, user, m)
            if isinstance(r, Response):
                # batch + HTTP-level answer: the challenge stands for the
                # whole request (batching is MCP-2025-11-removed anyway)
                passthrough = r
                break
            if r is not None:
                out.append(r)
        if passthrough is not None:
            return passthrough
        if not out:
            # only notifications / responses: accepted, nothing to answer
            return Response(status_code=202, headers=cors)
        return JSONResponse(out, headers=cors)
    resp = await _dispatch_safe(request, user, msg)
    if resp is None:
        return Response(status_code=202, headers=cors)
    if isinstance(resp, Response):
        # an HTTP-level answer (the 401 challenge) passes through as-is
        return resp
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
        from config import BASE_URL as _base
        cat_version = await asyncio.to_thread(catalog_version)
        # The caller's real account limits, from the same entitlement
        # projection the delivery schemas advertise (one read); an
        # anonymous caller sees the Free plan it would register into.
        ent = await asyncio.to_thread(_entitlements, user)
        account_txt = "" if user else (
            "You are connected without an API key: tools/list shows "
            "the first-party tools and every other call needs an "
            "account. Call the free tool 'register' once (optional "
            "username) — it returns your API key; reconnect with the "
            "header Authorization: Bearer <key>. ")
        limits_txt = (
            ("Your account limits: " if user else "Free plan limits: ")
            + f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB per input file, up "
            f"to {ent['ingress_files_24h']} files and "
            f"{format_bytes(ent['ingress_bytes_24h'])} total per rolling "
            "24 hours; delivery options: expiry_days "
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
                f"capabilities. {account_txt}"
                "To give a person a link to a file: call "
                "share; hand them result_url (one page for all files) "
                "— page_url is per file, download_url is a direct "
                "download. tools/list is the master list: every "
                "public tool plus your own, favorites flagged "
                "(我的收藏); list_favorites lists just your pins. "
                "catalog_search filters by keyword (empty query = "
                "full catalog, paged); call any tool by name. "
                "Pass "
                "files inline (base64 — one request body is capped at "
                f"{MCP_MAX_BODY_BYTES // (1024 * 1024)} MB; larger files "
                "go through create_upload), by https URL, or by "
                "reference; "
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
                "as deleting on the web). Billing: each tool's "
                "description states its price (Free or N credits per "
                "call; 1000 credits = 1 USD) and a call is charged when "
                "it runs — no confirmation step; failed calls are "
                "refunded automatically. If credits run out, the call "
                "returns err_insufficient_credits: the account owner "
                f"tops up at {_base}/pricing (log in with the account), "
                "then retry with the same client_ref. "
                f"{limits_txt} Catalog version: "
                f"{cat_version}."
            ),
        })
    # notifications are NEVER answered — the early-out precedes every
    # responding branch, including ping
    if is_notification:
        return None
    if method == "ping":
        return _rpc(mid, {})
    if method == "tools/list":
        page_size = 50
        specs = (await asyncio.to_thread(_tool_specs, user)) if user \
            else await asyncio.to_thread(_anon_tool_specs)
        cur = params.get("cursor")
        try:
            start = int(cur) if cur else 0
        except (TypeError, ValueError, OverflowError):
            return _rpc_err(mid, -32602, "invalid cursor")
        if start < 0:
            return _rpc_err(mid, -32602, "invalid cursor")
        out = {"tools": specs[start:start + page_size]}
        if start + page_size < len(specs):
            out["nextCursor"] = str(start + page_size)
        return _rpc(mid, out)
    if method == "tools/call":
        if not user:
            # register stays anonymously callable (the zero-friction
            # onboarding path); every other tool needs a credential —
            # spec-canonical 401 + WWW-Authenticate so platform
            # connectors auto-start OAuth
            name = params.get("name") if isinstance(params, dict) else None
            if name == "register":
                return await _anon_call(request, mid, params)
            return _auth_challenge_json(mid)
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
                           "token for your own shared uploads, including the "
                           "file items a previous call returned). No re-upload, "
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
    "Add a tool to your favorites (我的收藏) — your shortlist for "
    "reuse. tools/list marks favorites with a flag; list_favorites "
    "lists just the pins. Free.")
_UNPIN_TOOL_SPEC = _pin_spec(
    "unpin_tool", "Remove from My Favorites",
    "Remove a tool from your favorites. It stays callable by name. Free.")

# The favorites list, independent of the master tools/list: exactly the
# pinned tools, pin order — the agent's shortlist.
_LIST_FAVORITES_SPEC = {
    "name": "list_favorites",
    "title": "List Favorites",
    "description":
        "List exactly YOUR favorite tools (pin order) — the shortlist "
        "you manage with pin_tool / unpin_tool. tools/list carries "
        "every callable tool with a favorite flag; this one carries "
        "just the pins. Free.",
    "inputSchema": {"type": "object", "properties": {}},
    "outputSchema": {
        "type": "object",
        "properties": {
            "tools": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["tools"],
    },
}


def _list_favorites(user: dict, mid) -> dict:
    from governance.favorites import favorite_keys
    ent = _entitlements(user)
    catalog = list_catalog(user["id"])
    by_id = {t["id"]: t for t in catalog}
    favs = favorite_keys(user["id"], catalog)
    tools = []
    for k in favs:
        spec = _catalog_spec(by_id[k], ent)
        spec["favorite"] = True
        tools.append(spec)
    lines = ([f"{len(tools)} favorite(s):"]
             + [f"- {t['name']} — {t['title']}" for t in tools]
             if tools else ["No favorites yet — pin one with pin_tool."])
    return _meta_result(mid, {"tools": tools}, text="\n".join(lines))


# ── Strict arguments: an undeclared key is REJECTED, never silently
# dropped. A wrong parameter name would otherwise run as a successful
# unfiltered/default call — the worst failure mode for an agent (it
# cannot tell the mistake from the result). Allowed keys come from each
# tool's own inputSchema (single source); platform tools take the four
# canonical invocation argument forms (files / params / delivery /
# client_ref) plus `input` (正文工具的 source.body 取值 — 文件工具不
# 声明它, 显式携带 → -32602)。Aligns the adapter with the frozen rule:
# only what is ABSENT gets a default; anything explicitly present but not
# declared is a -32602 naming the unknown key.
_CALL_ARG_KEYS = frozenset({"files", "params", "delivery", "client_ref",
                            "input"})


def _reject_unknown_args(mid, name: str, args: dict):
    spec = _META_SPECS_BY_NAME.get(name)
    if spec is not None:
        allowed = set(spec["inputSchema"]["properties"])
    elif name == "catalog_search":
        allowed = {"q", "page"}
    else:
        allowed = set(_CALL_ARG_KEYS)
    unknown = sorted(set(args) - allowed)
    if unknown:
        return _rpc_err(
            mid, -32602,
            "unknown argument key: %s (allowed: %s)"
            % (", ".join(unknown), ", ".join(sorted(allowed))))
    return None


def _spec_summary(t: dict) -> dict:
    """name/title/description summary (tools/list + catalog_search share
    the exact same shape). English only — MCP is a machine surface.
    name = tool_key 原文 (无投影, 无撞名分配)。"""
    label = t["label"]
    title = resolve_label(label, "en") or t["id"]
    mn = int(t["files_min"])
    mx = int(t["files_max"])
    accept = t.get("accept")
    if mx == 0:
        files_txt = ("text body input (source.body)"
                     if t.get("body_format") == "text"
                     else "json body input (source.body)")
    else:
        files_txt = (f"{mn}" if mn == mx else f"{mn}-{mx}") + " file(s)"
        if accept:
            # 全边界摘要 (投影层, 存储仍是单形状全清单): 逐项铺 119 格式对 agent 是噪音
            from governance.formats import VOCAB_EXTS as _vocab
            if len(accept) >= len(_vocab):
                files_txt += ", formats: all platform formats"
            else:
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
        "name": t["id"],
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
                                "public_id": {"type": "string"},
                                "download_url": {"type": "string"},
                                "page_url": {"type": "string"}},
                 "required": ["kind", "public_id", "filename",
                              "content_type", "size",
                              "download_url", "page_url"]},
            ]},
        },
    },
    "required": ["call_id", "replay", "result_url", "items"],
}


def _catalog_spec(t: dict, ent: dict) -> dict:
    """One catalog item → THE tool spec. tools/list and catalog_search
    share this exact constructor (name/title/description/inputSchema/
    outputSchema/annotations), so a search hit is directly callable
    without listing the tool first.

    `ent` = the calling bearer's
    entitlements projection (delivery enums) — the discovery layer
    advertises THIS caller's real values; tools/call still runs the
    authoritative chain regardless of what the schema advertised."""
    summary = _spec_summary(t)
    props = {}
    mn, mx = int(t["files_min"]), int(t["files_max"])
    required = ["client_ref"]
    if mx > 0:
        # file tools expose files[]; body tools (files_max=0) expose the
        # input value itself instead — text maps to string, json to any
        # legal JSON value (null included)
        props["files"] = {
            "type": "array",
            "description": "Input files: each item is inline base64, an "
                           "https URL, or a reference to an existing upload",
            "minItems": mn,
            "maxItems": mx,
            "items": _FILE_ITEM_SCHEMA,
        }
        if mn >= 1:
            required.append("files")
    elif t.get("body_format") == "text":
        props["input"] = {
            "type": "string",
            "description": "The tool's text body input (non-empty UTF-8, "
                           "max 100000 chars)",
        }
        required.append("input")
    else:
        props["input"] = {
            "description": "The tool's JSON body input: any legal JSON "
                           "value (null, string, number, boolean, array, "
                           "object)",
        }
        required.append("input")
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


def _entitlements(user: dict | None) -> dict:
    """Delivery/ingress entitlements for discovery text and schema
    enums: the bearer's projection, or the Free plan an anonymous
    caller would register into (the canonical pure Free capability)."""
    if user:
        from governance.access_projection import project
        return project(user["id"])["entitlements"]
    from governance.entitlements import resolve_from_membership
    return resolve_from_membership(None)


# The only tool an anonymous caller can run: headless registration
# (the same function as POST /auth/mcp/register).
_REGISTER_SPEC = {
    "name": "register",
    "title": "Register",
    "description":
        "Create a free 518.PLUS account for this agent and get its API "
        "key. Call once, without an Authorization header; then send "
        "every MCP request with the header Authorization: Bearer <key>. "
        "Store the key (the owner can view it again at /account/keys "
        "after logging in with the returned username/password). username "
        "is optional (letters, digits, underscore; one is generated "
        "otherwise). The returned username and password let the human "
        "owner log in on the web to top up credits for paid tools "
        "(free tools need no credits).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "username": {"type": "string",
                         "description": "Optional account name"},
        },
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "api_key": {"type": "string"},
            "username": {"type": "string"},
            "password": {"type": "string"},
            "login_url": {"type": "string"},
            "pricing_url": {"type": "string"},
            "next": {"type": "string"},
        },
        "required": ["api_key", "username", "password", "next"],
    },
}

# built after every meta spec exists (module order): the strict-args
# allowed-key map — see _reject_unknown_args above for the rationale
_META_SPECS_BY_NAME = {s["name"]: s for s in (
    _REGISTER_SPEC, _CREATE_UPLOAD_SPEC, _LIST_UPLOADS_SPEC,
    _DELETE_UPLOAD_SPEC, _PIN_TOOL_SPEC, _UNPIN_TOOL_SPEC,
    _LIST_FAVORITES_SPEC)}


def _anon_tool_specs() -> list:
    """tools/list without a key: register + the FULL public catalog
    (calling any of them needs a key; the descriptions are the same
    text a registered agent sees, without favorite flags)."""
    ent = _entitlements(None)
    return [_REGISTER_SPEC] + [_catalog_spec(t, ent)
                               for t in list_catalog(None)]


async def _anon_call(request: Request, mid, params: dict):
    """tools/call without a key: `register` runs; any other tool answers
    a tool error that says how to get a key (never an HTTP rejection)."""
    name = params.get("name")
    args = params.get("arguments")
    if args is None:
        args = {}
    elif not isinstance(args, dict):
        return _rpc_err(mid, -32602,
                        "invalid params: arguments must be an object")
    bad_args = _reject_unknown_args(mid, name, args)
    if bad_args:
        return bad_args
    from config import BASE_URL as _base
    if name == "register":
        from app.routes.auth_route import headless_register
        raw = {}
        if args.get("username") is not None:
            raw["username"] = args.get("username")
        status, body = await headless_register(request, raw)
        if not body.get("ok"):
            return _iserror(mid, {"error": body.get("error")})
        out = {
            "api_key": body["key"]["key"],
            "username": body["username"],
            "password": body["password"],
            "login_url": f"{_base}/auth/login",
            "pricing_url": f"{_base}/pricing",
            "next": ("Reconnect with the header Authorization: Bearer "
                     "<api_key>, then call tools/list. Free tools work "
                     "right away; paid tools need credits — the owner "
                     "logs in with this username/password and tops up "
                     "at pricing_url."),
        }
        # readable text first (the agent's surface); JSON rides in
        # structuredContent only
        text = ("Account ready.\n"
                f"API key: {out['api_key']}\n"
                f"Username: {out['username']}  Password: {out['password']}\n"
                f"Log in: {out['login_url']}\n"
                f"{out['next']}")
        return _rpc(mid, {
            "content": [{"type": "text", "text": text}],
            "structuredContent": out, "isError": False})
    return _iserror(mid, {"error": "err_auth_required"})


def _tool_specs(user: dict) -> list:
    # Discovery-layer projection: schema enums show THIS bearer's real
    # entitlement (one access-projection read per _tool_specs call).
    # Not an authorization surface — tools/call still runs the full
    # authoritative chain regardless of what the schema advertised.
    ent = _entitlements(user)
    specs = [_CREATE_UPLOAD_SPEC, _LIST_UPLOADS_SPEC,
             _DELETE_UPLOAD_SPEC, _PIN_TOOL_SPEC, _UNPIN_TOOL_SPEC,
             _LIST_FAVORITES_SPEC]
    # THE master list, not an access boundary: every public tool plus
    # the bearer's own tools; the favorite flag marks their pins
    # (list_favorites lists just the pins). tools/call runs the full
    # authoritative chain regardless of what the list advertised.
    from governance.favorites import favorite_keys
    catalog = list_catalog(user["id"])
    favs = set(favorite_keys(user["id"], catalog))
    for t in catalog:
        spec = _catalog_spec(t, ent)
        spec["favorite"] = t["id"] in favs
        specs.append(spec)
    specs.append({
        "name": "catalog_search",
        "title": "Catalog Search",
        "description": "Discover public tools. An EMPTY query lists "
                       "EVERY public tool page by page (total + "
                       "total_pages tell the full depth) — read the "
                       "list when you'd rather browse than guess "
                       "keywords. A non-empty query filters: every "
                       "whitespace-separated term must match the tool's "
                       "id, name or description (all AND; % and _ match "
                       "literally). Results are paged (page starts at "
                       "1). Each result carries the exact spec "
                       "tools/list shows (inputSchema included) plus "
                       "favorite — call it by name right away; pin_tool "
                       "adds it to your favorites.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"},
                           "page": {"type": "integer", "minimum": 1}},
            "required": ["q"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "page": {"type": "integer"},
                "total_pages": {"type": "integer"},
                "total": {"type": "integer"},
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
            "required": ["query", "page", "total_pages", "total",
                         "tools"],
        },
    })
    return specs


def _catalog_search_result(user: dict, q: str, page: int) -> dict:
    """catalog_search result payload: one page of the public market
    (governance.market_page — the same predicate, order and page clamp
    as the web /tools market) projected to the exact callable spec
    tools/list carries (shared _catalog_spec constructor) plus the
    caller's favorite flag — one projection read for this bearer's
    delivery enums, kept OUT of the _call_tool dispatch path (the call
    path reads no projection). An EMPTY query enumerates the FULL
    public catalog (curated=False — the featured-only browse stays a
    web-page behavior): for an agent, reading the whole list paged is
    the primary discovery path, search the accelerator."""
    from governance.catalog import market_page
    from database import db
    from repo import favorites as fav_repo
    data = market_page(q or None, page, curated=False)
    # hits are public by construction — the raw pin set flags them
    # (no visibility-filtered catalog read needed)
    with db() as con:
        favs = set(fav_repo.list_keys(con, user["id"]))
    ent = _entitlements(user)
    tools = []
    for h in data["items"]:
        spec = _catalog_spec(h, ent)
        spec["favorite"] = h["id"] in favs
        tools.append(spec)
    return {"query": q, "page": data["page"],
            "total_pages": data["total_pages"], "total": data["total"],
            "tools": tools}


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
    bad_args = _reject_unknown_args(mid, name, args)
    if bad_args:
        return bad_args
    # Entry-wide rate limit, BEFORE any branch (meta tools, catalog
    # search and URL-source fetching all count): a runaway agent is
    # stopped at the entry, not per-branch. The execution gate keeps
    # its own separate client_ref throttle.
    from economy.antibuse import mcp_call_throttled
    if mcp_call_throttled(user["id"]):
        return _iserror(mid, {"error": "err_rate_limited"})
    # Capability chokepoint — the ONE authorization comparison for every
    # bearer (fsk_ key scopes and OAuth consent grants alike)
    from identity.capabilities import op_allowed
    granted = getattr(getattr(request, "state", None), "auth_scopes", None)
    if granted is not None and not op_allowed(name, granted):
        from identity.capabilities import required_scope
        need = required_scope(name)
        return _iserror(mid, {
            "error": "err_insufficient_scope",
            "required_scope": need,
            "granted_scopes": sorted(granted),
            "message": (
                f"This credential is not allowed to do this (needs "
                f"{need}; granted: {', '.join(sorted(granted)) or 'none'})."),
        })
    if name == "catalog_search":
        # Search IS the public market (same predicate/order/projection/
        # paging as /tools); empty q lists the recommended tools. See
        # _catalog_search_result.
        q = str(args.get("q") or "").strip()[:64]
        page = args.get("page", 1)
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return _rpc_err(mid, -32602, "invalid params: page must be an "
                                         "integer >= 1")
        sc = await asyncio.to_thread(_catalog_search_result, user, q, page)
        lines = []
        if q:
            lines.append(f"{sc['total']} tool(s) match \"{q}\""
                         f" — page {sc['page']}/{sc['total_pages']}:")
        else:
            lines.append(f"{sc['total']} public tool(s)"
                         f" — page {sc['page']}/{sc['total_pages']}:")
        for t in sc["tools"]:
            fav = " · favorite" if t.get("favorite") else ""
            lines.append(f"- {t['name']} — {t['title']}{fav}")
        return _meta_result(mid, sc, text="\n".join(lines))

    if name == "create_upload":
        return await asyncio.to_thread(_create_upload, user, mid, args, request)
    if name == "list_uploads":
        return await _list_uploads(user, mid, args)
    if name == "delete_upload":
        return await _delete_upload(user, mid, args)
    if name in ("pin_tool", "unpin_tool"):
        return await asyncio.to_thread(
            _pin_tool, user, mid, args, name == "pin_tool")
    if name == "list_favorites":
        return await asyncio.to_thread(_list_favorites, user, mid)

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
            # Frozen adapter contract: defaults fill MISSING values only;
            # an explicitly illegal value is a protocol-level invalid
            # params error (-32602), never an isError tool result.
            # Legal values come from the one delivery authority.
            return _rpc_err(
                mid, -32602,
                "invalid delivery: must be an object with optional "
                f"integer fields expiry_days (one of {list(EXPIRY_DAYS)}) "
                "and allowed_downloads (one of "
                f"{list(DOWNLOAD_LIMITS)})")
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

    # 正文工具的输入值 (→ source.body)。缺失 = -32602 (inputSchema 将
    # input 标记 required); 显式 null 在 json 工具下是合法根值。
    from execution.invocation import BODY_ABSENT as _BODY_ABSENT
    body_arg = args["input"] if "input" in args else _BODY_ABSENT

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

    # Tool + delivery entitlement + stored contract + params — the SAME
    # pre-gate validators dispatch runs, here BEFORE any fetch, quota
    # claim or Gokapi upload.
    tool_key = name
    from execution.dispatch import (preflight as _preflight,
                                    source_accept_error as _accept_err)
    tool, _declared, _canon_body, _pbody, _pstatus = await asyncio.to_thread(
        _preflight, user, tool_key, delivery, tool_params, body_arg)
    if _pbody is not None:
        if _pbody.get("error") == "tool_not_found":
            return _rpc_err(mid, -32602, f"unknown tool: {name}")
        # a mode mismatch (files vs input) is protocol-level: the schema
        # never declared the argument the caller sent. ONLY this code
        # translates to "invalid input mode"; plain invalid_request keeps
        # its generic isError path below.
        if _pbody.get("error") == "err_input_mode":
            sent_input = "input" in args
            return _rpc_err(
                mid, -32602,
                "invalid input mode: this tool takes "
                + ("files[] (`files`)" if sent_input
                   else "a body input (`input`)"))
        # body 口径违规 (缺失/超长/非法) 同为调用方参数错误 — 协议级
        # -32602, 不是工具执行失败 (isError)。
        if _pbody.get("error") in ("err_body_required", "err_body_too_long",
                                   "err_body_invalid"):
            return _rpc_err(
                mid, -32602, f"invalid input: {_pbody['error']}")
        return _tool_result_error(mid, _pbody, client_ref, tool)
    # input-mode argument discipline (frozen rule: explicitly present but
    # not declared → -32602): body tools never declare files[], file tools
    # never declare input.
    if tool.get("body_format"):
        if "files" in args:
            return _rpc_err(mid, -32602,
                            "unknown argument key: files (this tool takes "
                            "a body `input`, not files[])")
        if "input" not in args:
            return _rpc_err(mid, -32602, "input required")
        if tool["body_format"] == "text" and not isinstance(body_arg, str):
            return _rpc_err(mid, -32602,
                            "invalid input: text body must be a non-empty "
                            "string")
    elif "input" in args:
        return _rpc_err(mid, -32602,
                        "unknown argument key: input (this tool takes "
                        "files[], not a body input)")

    # Advisory credits affordability BEFORE source ingress: a clear
    # insufficiency answers as an application error (isError tool
    # result) without uploading anything. Authoritative consume stays
    # in the gate; this snapshot never reserves credits.
    from execution.affordability import check_affordability
    _denial = await asyncio.to_thread(
        check_affordability, user["id"], tool, client_ref)
    if _denial:
        return _iserror(mid, _denial, client_ref)

    # 文件来源准备 + 暂存单点 (A4): 引用权限解析 → URL 钉扎拉取 →
    # sniff 收容 → accept 闸门 → 批量上限 → ingress 哈希 → stage_sources,
    # 与 /v1 同一实现 (sharing.staging.ingest_sources)。
    from sharing.staging import (ingest_sources as _ingest_sources,
                                 SourceError as _SourceError,
                                 StagingError as _StagingError)
    from sharing.ingress_receipt import (
        IngressConflict as _IngressConflict,
        IngressInProgress as _IngressInProgress,
        IngressQuotaDenied as _IngressQuotaDenied,
        IngressUnresolved as _IngressUnresolved)
    try:
        source_files, _canon = await _ingest_sources(
            user, tool, items, client_ip(request), client_ref, tool_key)
    except _SourceError as exc:
        # 调用方可修复的输入拒绝 — isError 工具结果 (IO-1 规则: 形态
        # 合法但内容非法是域级错误; -32602 只留给请求形态错误)
        return _tool_result_error(mid, {"error": exc.code, **exc.extra},
                                  client_ref, tool)
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
    except _StagingError:
        # known rollback (staging failed or finalize proven rolled back —
        # staged objects cleaned, quota released exactly)
        return _rpc_err(mid, -32603, "err_upload_failed")

    raw = {
        "tool_id": tool_key,
        "source": ({"body": body_arg} if tool.get("body_format")
                   else {"files": source_files}),
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
    import hashlib as _hl
    _input_digests = frozenset(
        _hl.sha256(_b).hexdigest() for _, _, _b in _canon)
    return _rpc(mid, await _mcp_success(result, payload, _input_digests))


def _error_message(code, tool: dict | None = None) -> str:
    """THE single human-text map: one plain-English sentence + an
    actionable hint for the codes an agent can act on. Unknown codes
    fall back to the generic sentence."""
    code = str(code or "tool_failed")
    base = {
        "err_format_not_supported": "The file format is not supported by this tool.",
        "err_file_type_blocked": "This file type is blocked on 518.",
        "err_file_count": "The number of files is outside this tool's window.",
        "err_content_mismatch_image": "The image content does not match the file's extension.",
        "err_content_mismatch_pdf": "The document content does not match the file's extension.",
        "err_content_mismatch_archive": "The archive content does not match the file's extension.",
        "err_content_mismatch_office": "The document content does not match the file's extension.",
        "err_content_mismatch_text": "The text content does not match the file's extension.",
        "err_insufficient_credits": "Not enough credits for this call.",
        "err_auth_required": "This call needs an API key.",
        "err_too_many_attempts": "Too many registrations from this address; retry later.",
        "err_username_taken": "That username is taken; pick another or omit it.",
        "err_username_invalid": "That username is not allowed; use letters, digits and underscore, or omit it.",
        "err_username_short": "That username is too short; pick a longer one or omit it.",
        "err_username_long": "That username is too long; pick a shorter one or omit it.",
        "err_username_reserved": "That username is reserved; pick another or omit it.",
        "tool_disabled": "This tool is currently disabled.",
        "err_rate_limited": "Too many requests; retry shortly.",
        "err_upload_in_progress": "An upload with this client_ref is still settling; retry shortly with the same client_ref.",
        "err_busy": "The service is busy; please retry shortly with the same client_ref.",
        "err_call_in_progress": "This operation is already running; retry the same client_ref for its result.",
        "idempotency_conflict": "This client_ref was used for different input; use a new client_ref.",
        "err_file_too_large": "A file exceeds the size limit.",
        "err_image_too_large": "The image dimensions exceed the limit.",
        "err_endpoint_failed": "The tool's service failed.",
        "err_endpoint_timeout": "The tool's service timed out.",
        "err_endpoint_blocked": "The tool's service endpoint is not allowed.",
        "err_expiry_not_entitled": "The requested delivery.expiry_days is not available on your current plan.",
        "err_download_limit_not_entitled": "The requested delivery.allowed_downloads is not available on your current plan.",
        "tool_failed": "The tool failed to produce a result.",
        "not_found": "No such file of yours.",
        "tool_not_found": "No such tool is available to you. Use catalog_search to find the exact name.",
        "invalid_id": "That public id is malformed.",
        "param_required": "A required parameter is missing.",
        "param_invalid": "A parameter value is not allowed.",
        "param_out_of_range": "A number parameter is out of range.",
        "param_too_long": "A text parameter is too long.",
        "err_output_mismatch": "The tool's service returned a result kind this tool does not declare.",
    }.get(code)
    msg = base or "The call failed."
    hint = ""
    if code == "err_format_not_supported":
        accept = tool.get("accept") if isinstance(tool, dict) else None
        if accept:
            hint = f" Accepted formats: {', '.join(accept)}."
    elif code == "err_file_count" and isinstance(tool, dict):
        mn, mx = int(tool["files_min"]), int(tool["files_max"])
        hint = (" This tool takes no files." if mx == 0
                else f" This tool accepts {mn} to {mx} files.")
    elif code == "err_insufficient_credits":
        from config import BASE_URL as _base
        hint = (" The account owner tops up at "
                f"{_base}/pricing (log in with the account); then retry "
                "with the same client_ref — it is not charged twice.")
    elif code == "err_auth_required":
        hint = (" Call the free tool 'register' (no Authorization "
                "header) to get one, then reconnect with Authorization: "
                "Bearer <key>.")
    elif code == "param_too_long":
        from governance.tool_params import TEXT_PARAM_MAX_CHARS
        hint = f" Text parameters take at most {TEXT_PARAM_MAX_CHARS} characters."
    if code.startswith("param_"):
        hint += " Check the tool's inputSchema (field names the parameter)."
    return f"{msg}{hint}"


def _iserror(mid, payload: dict, client_ref: str | None = None,
             tool: dict | None = None) -> dict:
    """The ONE isError tools/call envelope: content = one READABLE text
    block (error code, the one-sentence reason from _error_message,
    client_ref, then every structured field the denial carried, one
    per line); structuredContent = the same fields as the machine
    channel (parity with the success envelope). Existing fields are
    never dropped."""
    code = payload.get("error") or "tool_failed"
    body = {"error": code, "message": _error_message(code, tool)}
    if client_ref is not None:
        body["client_ref"] = client_ref
    body.update({k: v for k, v in payload.items()
                 if k not in ("error", "message", "client_ref", "ok")})
    lines = [f"Error {code}: {body['message']}"]
    if client_ref is not None:
        lines.append(f"client_ref: {client_ref}")
    for k, v in body.items():
        if k in ("error", "message", "client_ref"):
            continue
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return _rpc(mid, {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": body,
        "isError": True,
    })


def _pin_tool(user: dict, mid, args: dict, on: bool) -> dict:
    from governance.favorites import set_favorite
    name = str(args.get("name") or "").strip()
    tool_key = name or None
    err = set_favorite(user["id"], tool_key or "", on) if tool_key else "tool_not_found"
    if err:
        return _iserror(mid, {"error": err, "name": name})
    return _meta_result(mid, {"name": name, "favorite": on},
                        text=(f"{name} added to favorites."
                              if on else
                              f"{name} removed from favorites. "
                              "It stays callable by name."))


def _fmt_bytes(n) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GB"


def _fmt_ts(epoch) -> str:
    import datetime as _dt
    if epoch is None:
        return "—"
    return _dt.datetime.fromtimestamp(
        int(epoch), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _meta_result(mid, payload: dict, text: str | None = None,
                 isError: bool = False) -> dict:
    """The ONE envelope builder for meta tools (and any response that
    carries structuredContent without running the execution chain).

    content = a READABLE text block (the agent's primary surface —
    labels and values, not a JSON dump); structuredContent = the
    machine channel mirroring the API fields. `text` should be
    provided by every caller; the JSON-dump fallback only guards
    against a missed one and is not part of the contract."""
    return _rpc(mid, {
        "content": [{"type": "text",
                     "text": text if text is not None
                     else json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": isError,
    })


async def _mcp_success(result: dict, payload: dict,
                       input_digests: frozenset | None = None) -> dict:
    """Build the MCP tools/call success envelope: content (the agent's
    direct result) + structuredContent (the machine shape, matching
    every tool's outputSchema).

    content = text items as plain text, a preview image block for each
    new image result (≤1 MB), and one closing text block with a
    READABLE summary (result link, expiry, file list) — the JSON
    fields live once in structuredContent, not repeated as extra
    link blocks.

    input_digests = sha256 set of THIS call's input file bodies. An
    image result byte-identical to an input (a pass-through tool echoing
    what the agent just sent) gets no image block, so the agent's own
    bytes never bounce back into its context. General rule, no per-tool
    branching."""
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
            sc_items.append({"kind": "file", "public_id": pid,
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

                def _gid_of(pid: str) -> str:
                    with _db() as _c:
                        _u = _up_by_pid(_c, pid)
                        return _u["gokapi_id"] if _u else pid

                def _read_preview(gid: str):
                    """Open + bounded-read + close, ALL in the worker
                    thread — the body read is blocking I/O, never loop
                    work. Bounded read (cap+1), stream always closed,
                    same optional-preview / non-counting channel."""
                    status, _h, stream = _storage_read(gid)
                    try:
                        return status, stream.read(1_048_577)
                    finally:
                        stream.close()
                try:
                    _gid = await asyncio.to_thread(_gid_of, pid)
                    status, body = await asyncio.to_thread(
                        _read_preview, _gid)
                    if status == 200 and len(body) <= 1_048_576 \
                            and _hl.sha256(body).hexdigest() not in input_digests:
                        content.append({
                            "type": "image",
                            "data": _b64.b64encode(body).decode(),
                            "mimeType": ct})
                except (InternalDownloadUnavailable, Exception):
                    pass
    result_url = result.get("result_url") or ""
    structured = {
        "call_id": payload.get("call_id"),
        "replay": bool(payload.get("replay")),
        "result_url": result_url,
        "expires_at": expires_at,
        "items": sc_items,
    }
    # The closing text block is a READABLE summary (the agent's
    # surface); the JSON fields live in structuredContent only.
    # text/json items already rode as their own blocks above.
    lines = []
    if structured["replay"]:
        lines.append("Replay: returning the original result of this "
                     "client_ref — nothing was charged or run again.")
    lines.append(f"Result page: {result_url or '—'}")
    lines.append(f"Expires: {_fmt_ts(expires_at)}")
    files = [i for i in sc_items if i.get("kind") == "file"]
    if files:
        lines.append("Files:")
        for f in files:
            lines.append(
                f"- {f['filename']} — {f['content_type']}, "
                f"{_fmt_bytes(f['size'])}"
                f" · download: {f['download_url']}"
                f" · page: {f['page_url']}")
    content.append({"type": "text", "text": "\n".join(lines)})
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
    text = ("One-time upload URL (expires "
            f"{_fmt_ts(_payload['expires_at'])}, max "
            f"{_fmt_bytes(_payload['max_bytes'])}):\n"
            f"{_payload['upload_url']}\n"
            "PUT the raw bytes — NO Authorization header. The answer "
            "gives {public_id, source_token}; pass them as the "
            "files[] reference form in tools/call.")
    return _meta_result(mid, _payload, text=text)


async def _list_uploads(user: dict, mid, args: dict) -> dict:
    """list_uploads meta tool: the SAME single point the web GET
    /api/uploads uses (sharing.list_uploads) — projection, live
    download counters and Gokapi mapping stay one implementation."""
    from config import BASE_URL
    from sharing.uploads import list_uploads as _web_list
    offset = args.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _rpc_err(mid, -32602, "invalid params: offset must be an "
                                     "integer >= 0")
    import asyncio
    rows = await asyncio.to_thread(
        _web_list, user["id"], offset=min(offset, MAX_LIST_OFFSET),
        limit=20)
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
    lines = []
    if uploads:
        lines.append(f"{len(uploads)} live file(s)" +
                     ("" if len(rows) < 20 else " (more: offset="
                      + str(offset + len(rows)) + ")") + ":")
        for u in uploads:
            left = (f"{u['downloads_remaining']} downloads left"
                    if u["downloads_remaining"] is not None
                    else "downloads unlimited")
            lines.append(
                f"- {u['filename']} — {u['content_type']}, "
                f"{_fmt_bytes(u['size'])}"
                f" · expires {_fmt_ts(u['expires_at'])} · {left}"
                f" · page: {u['page_url']}")
    else:
        lines.append("No live files.")
    return _meta_result(mid, {"uploads": uploads,
                              "has_more": len(rows) == 20},
                        text="\n".join(lines))


async def _delete_upload(user: dict, mid, args: dict) -> dict:
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
    upload = await asyncio.to_thread(get_upload_by_public_id, public_id)
    if not upload or upload["user_id"] != user["id"]:
        # unknown OR someone else's: one shape, no existence oracle
        return _iserror(mid, {
            "deleted": False, "public_id": public_id,
            "error": "not_found"})
    await asyncio.to_thread(_web_delete, upload["id"], user["id"])
    return _meta_result(mid, {"deleted": True, "public_id": public_id},
                        text=f"Deleted {public_id}. The /f/ page 404s "
                             "immediately; bytes and mapping went "
                             "together.")


# URL-source helper lives in the staging single point (A4).
from sharing.staging import url_source_upload as _url_upload  # noqa: E402


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


from sharing.staging import _guess_ctype  # noqa: E402,_decode_file consumer
