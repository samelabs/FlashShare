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
    # Discovery-layer projection: schema enums show THIS bearer's real
    # entitlement (one access-projection read per _tool_specs call).
    # Not an authorization surface — tools/call still runs the full
    # authoritative chain regardless of what the schema advertised.
    from governance.access_projection import project
    ent = project(user["id"])["entitlements"]
    expiry_enum = list(ent["expiry_days"])
    downloads_enum = list(ent["download_limits"])
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
        required = ["files", "client_ref"]
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
    from execution.pipeline import upload_sources as _upload_batch

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
        # Malformed input outranks business affordability — protocol
        # validation stays first so a low balance can never mask it.
        return _rpc_err(mid, -32602, f"invalid arguments: {exc}")

    # Advisory credits affordability BEFORE source ingress: a clear
    # insufficiency answers as an application error (isError tool
    # result) without uploading anything. Authoritative consume stays
    # in the gate; this snapshot never reserves credits.
    from governance.catalog import resolve as _resolve_tool
    from execution.affordability import check_affordability
    _tool_row, _terr = _resolve_tool(tool_key, user["id"])
    if _terr is None:
        _denial = check_affordability(user["id"], _tool_row)
        if _denial:
            return _rpc(mid, {
                "content": [{"type": "text", "text": json.dumps(_denial)}],
                "isError": True,
            })

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
        IngressConflict as _IngressConflict,
        IngressInProgress as _IngressInProgress,
        IngressQuotaDenied as _IngressQuotaDenied)
    from utils import sanitize_filename as _san
    from security import sniff_allowed as _sniff, sign_source_token
    _canon = []
    for _fn, _ct, _body in uploads:
        _ok_t, _final_t = _sniff(_fn, _ct, _body[:512])
        if not _ok_t:
            return _rpc_err(mid, -32602, f"upload rejected: {_final_t}")
        _canon.append(((_san(_fn), _final_t, _body)))
    # Authoritative platform batch boundary (single validator for
    # Web/REST/MCP) — BEFORE the receipt claim / quota / Gokapi.
    from governance.ingress import validate_batch as _vbatch
    _bdenial = _vbatch([len(_b) for _, _, _b in _canon])
    if _bdenial:
        return _rpc(mid, {
            "content": [{"type": "text", "text": json.dumps(_bdenial)}],
            "isError": True,
        })
    _req_hash = ingress_request_hash(
        "mcp_tool_source",
        [{"filename": _fn, "content_type": _ct, "size": len(_b),
          "sha256": _hl.sha256(_b).hexdigest()}
         for _fn, _ct, _b in _canon],
        {"tool_id": tool_key})
    try:
        _receipt, _usage_ids, _replay = _claim_receipt(
            user["id"], ip, client_ref, _req_hash,
            [len(_b) for _, _, _b in _canon])
    except _IngressConflict:
        return _rpc_err(mid, 32009, "idempotency_conflict")
    except _IngressInProgress:
        return _rpc_err(mid, 32009, "err_upload_in_progress")
    except _IngressQuotaDenied as exc:
        return _rpc(mid, {
            "content": [{"type": "text", "text": json.dumps(exc.denial)}],
            "isError": True,
        })

    if _replay is not None:
        infos = _replay if isinstance(_replay, list) else [_replay]
    else:
        from execution.pipeline import upload_to_gokapi as _gk_up
        from execution.pipeline import (INTERMEDIATE_TTL_DAYS as _ITTL,
                                        INTERMEDIATE_DOWNLOAD_LIMIT as _IDL)
        import io as _io
        _gids = []
        try:
            infos = []
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
            _receipt_ok(int(_receipt["id"]), infos)
        except Exception as exc:
            from gokapi import delete_gokapi_file as _gk_del
            for _gid in _gids:
                try:
                    _gk_del(_gid)
                except Exception:
                    pass
            try:
                _receipt_fail(int(_receipt["id"]), _usage_ids, user["id"])
            except Exception:
                pass
            return _rpc_err(mid, -32602, f"upload rejected: {exc}")

    source_files = [{"public_id": info["publicId"],
                     "source_token": info.get("sourceToken", "")}
                    for info in infos]

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
