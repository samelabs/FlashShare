"""/v1 — the public tool API surface (P5).

Machine surface rules (frozen at the auth boundary in app.main):
fsk_ API keys ONLY — a session cookie or an fsot_ OAuth token never
authenticates here. Scope model = the shared capability vocabulary;
executing a tool needs tools:call.

Ordering discipline: auth → shared per-user rate limit → scope → tool
resolve → delivery/params preflight — ALL before any URL fetch or
staging.

Platform options travel as headers (Idempotency-Key, X-518-Expiry-Days,
X-518-Allowed-Downloads); the query string carries TOOL parameters only
(duplicate keys are a 400). Errors are JSON {"ok": false, "error": ...}
on every path (404/405/500 included — see main.py handlers); no CORS
headers are ever emitted.
"""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.web_auth import client_ip

router = APIRouter()
logger = logging.getLogger("flashshare")

# 正文工具请求体 (即工具输入) 的读取上限
BODY_INPUT_MAX_BYTES = 512 * 1024


def _err(status: int, code: str, headers: dict | None = None,
         **extra) -> JSONResponse:
    body = {"ok": False, "error": code}
    body.update(extra)
    return JSONResponse(body, status_code=status, headers=headers)


def _auth(request: Request):
    """fsk_-only authentication for /v1 (cookies/fsot_ already refused at
    the boundary). Returns (user, None) or (None, 401 response)."""
    from app.main import get_user_from_request
    user = get_user_from_request(request)
    if user is None:
        return None, _err(401, "unauthorized")
    return user, None


def _entry_gates(request: Request, user: dict, need: str | None = "tools:call"):
    """auth 之后的入口闸门: 与 MCP 共用的按用户滑动窗口限流 →
    能力词表 (tools:call)。Returns an error response or None."""
    from economy.antibuse import mcp_call_throttled
    if mcp_call_throttled(int(user["id"])):
        return _err(429, "err_rate_limited")
    if need is not None:
        granted = getattr(getattr(request, "state", None), "auth_scopes",
                          None)
        if granted is not None and need not in granted:
            return _err(403, "err_insufficient_scope",
                        required_scope=need,
                        granted_scopes=sorted(granted))
    return None


def _idem_key(request: Request) -> tuple[str, JSONResponse | None]:
    """Idempotency-Key: optional, exactly one occurrence, 1-128 chars.
    Missing → adapter-generated; it doubles as the staging + call
    client_ref."""
    values = request.headers.getlist("idempotency-key")
    if not values:
        return "v1:" + uuid.uuid4().hex, None
    if len(values) > 1:
        return "", _err(400, "invalid_idempotency_key",
                        message="Idempotency-Key must appear at most once")
    key = values[0].strip()
    if not key or len(key) > 128:
        return "", _err(400, "invalid_idempotency_key",
                        message="Idempotency-Key must be 1-128 characters")
    return key, None


def _delivery_of(request: Request) -> tuple[dict, JSONResponse | None]:
    """Platform delivery headers → canonical delivery (defaults fill
    absent values; an explicitly illegal value is a 400)."""
    from governance.delivery import (validate_delivery, DEFAULT_EXPIRY_DAYS,
                                     DEFAULT_ALLOWED_DOWNLOADS)
    raw = {"expiry_days": DEFAULT_EXPIRY_DAYS,
           "allowed_downloads": DEFAULT_ALLOWED_DOWNLOADS}
    for header, field in (("x-518-expiry-days", "expiry_days"),
                          ("x-518-allowed-downloads", "allowed_downloads")):
        value = request.headers.get(header)
        if value is None:
            continue
        try:
            raw[field] = int(value.strip())
        except (ValueError, AttributeError):
            return {}, _err(400, "invalid_delivery",
                            message=f"{header} must be an integer")
    try:
        return validate_delivery(raw), None
    except ValueError:
        return {}, _err(400, "invalid_delivery",
                        message="delivery value outside the legal set")


def _query_params(request: Request) -> tuple[dict, JSONResponse | None]:
    """Query string = tool params only; duplicate keys are a 400. Values
    stay raw strings — enforce_params_decl does the typed conversion."""
    params = {}
    for k, v in request.query_params.multi_items():
        if k in params:
            return {}, _err(400, "duplicate_param",
                            message=f"query parameter {k!r} appears twice")
        params[k] = v
    return params, None


async def _read_capped(request: Request, cap: int) -> bytes | None:
    """Stream the request body up to cap bytes; None on overflow."""
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > cap:
            return None
    return bytes(buf)


def _resolve(key: str, user_id: int):
    from governance.catalog import resolve
    return resolve(key, user_id)


# ── GET surfaces ──

@router.get("/tools")
async def list_tools(request: Request):
    user, err = _auth(request)
    if err:
        return err
    denial = _entry_gates(request, user, need=None)
    if denial:
        return denial
    try:
        page = int(request.query_params.get("page", "1"))
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    from governance.catalog import market_page
    data = market_page(None, page, curated=False)
    return {"ok": True, "tools": data["items"], "total": data["total"],
            "page": data["page"], "total_pages": data["total_pages"]}


@router.get("/tools/{key}")
async def tool_detail(key: str, request: Request):
    user, err = _auth(request)
    if err:
        return err
    denial = _entry_gates(request, user, need=None)
    if denial:
        return denial
    tool, terr = _resolve(key, int(user["id"]))
    if terr == "forbidden":
        return _err(403, "forbidden")
    if tool is None:
        return _err(404, "tool_not_found")
    from database import db
    from repo.catalog import pricing_row
    with db() as con:
        price = pricing_row(con, tool["tool_key"])
    return {"ok": True, "tool": _tool_projection(tool, price)}


def _tool_projection(tool: dict, price: dict | None) -> dict:
    """The API-visible declaration: the same stored columns the usage
    area and the MCP inputSchema derive from."""
    def _jload(raw, fallback):
        try:
            return json.loads(raw) if raw else fallback
        except (TypeError, ValueError):
            return fallback
    return {
        "id": tool["tool_key"],
        "label": _jload(tool.get("label_json"), {}),
        "desc": _jload(tool.get("desc_json"), None),
        "execution": tool["execution"],
        "files_min": int(tool["files_min"]),
        "files_max": int(tool["files_max"]),
        "accept": _jload(tool.get("accept_json"), None),
        "body_format": tool.get("body_format"),
        "body_placeholder": _jload(tool.get("body_placeholder_json"), None),
        "params": _jload(tool.get("params_json"), []),
        "outputs": _jload(tool.get("outputs_json"), []),
        "cost": int(price["cost"]) if price else None,
    }


@router.get("/balance")
async def balance(request: Request):
    user, err = _auth(request)
    if err:
        return err
    denial = _entry_gates(request, user, need=None)
    if denial:
        return denial
    from economy.billing import spendable_balance
    return {"ok": True, "balance": spendable_balance(int(user["id"]))}


# ── POST /v1/tools/{key} — the execution surface ──

@router.post("/tools/{key}")
async def call_tool(key: str, request: Request):
    user, err = _auth(request)
    if err:
        return err
    denial = _entry_gates(request, user)
    if denial:
        return denial
    idem, ierr = _idem_key(request)
    if ierr:
        return ierr
    delivery, derr = _delivery_of(request)
    if derr:
        return derr
    params, perr = _query_params(request)
    if perr:
        return perr

    # tool resolve BEFORE any body read / fetch / staging
    tool, terr = _resolve(key, int(user["id"]))
    if terr == "forbidden":
        return _err(403, "forbidden")
    if tool is None:
        return _err(404, "tool_not_found")

    if tool.get("body_format"):
        return await _call_body_tool(request, user, tool, idem, delivery,
                                     params)
    return await _call_file_tool(request, user, tool, idem, delivery,
                                 params)


# ── body tools: the request body IS the input ──

async def _call_body_tool(request, user, tool, idem, delivery, params):
    from remote import _is_json_media_type
    ctype = (request.headers.get("content-type") or "").split(";")[0] \
        .strip().lower()
    raw = await _read_capped(request, BODY_INPUT_MAX_BYTES)
    if raw is None:
        return _err(413, "err_request_too_large", limit=BODY_INPUT_MAX_BYTES)
    if tool["body_format"] == "json":
        if not _is_json_media_type(ctype):
            return _err(415, "err_content_type",
                        message="this tool takes application/json input")
        try:
            body_value = json.loads(raw)
        except Exception:
            return _err(400, "err_body_invalid",
                        message="request body is not valid JSON")
    else:
        if ctype != "text/plain":
            return _err(415, "err_content_type",
                        message="this tool takes text/plain input")
        try:
            body_value = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _err(400, "err_body_invalid",
                        message="request body is not valid UTF-8")

    # pre-gate preflight (entitlement + params + body 口径) — pure, typed
    from execution.dispatch import preflight
    _t, declared, _cb, pbody, pstatus = preflight(
        user, tool["tool_key"], delivery, params, body=body_value)
    if pbody is not None:
        return JSONResponse(pbody, pstatus)
    if declared is not None:
        params = declared

    payload, ebody, estatus = await _dispatch(request, user, {
        "tool_id": tool["tool_key"],
        "source": {"body": body_value},
        "params": params,
        "delivery": delivery,
        "client_ref": idem,
    })
    if ebody is not None:
        return JSONResponse(ebody, estatus)
    return await _body_tool_response(user, payload)


async def _dispatch(request, user, raw):
    from execution.dispatch import dispatch_execute
    from execution.pool import get_pool, default_process_tool
    return await dispatch_execute(request, user, raw,
                                  default_process_tool(), get_pool())


async def _body_tool_response(user, payload) -> Response:
    """成功 = 唯一结果项原样作为响应体。json/text 直出; file 走站内
    不计数通道回字节。X-518-Call-Id / X-518-Cost 取自 gate 成功载荷
    (cost = claim 时持久化的 cost_snapshot)。重放同路径: 结果来自
    持久化 result_json, 绝不重新执行。"""
    result = payload.get("result") or {}
    items = result.get("items") or []
    headers = {
        "X-518-Call-Id": str(payload.get("call_id")),
        "X-518-Cost": str(payload.get("cost", 0)),
    }
    if len(items) != 1:
        logger.error("v1 body tool call %s returned %d items",
                     payload.get("call_id"), len(items))
        return _err(500, "internal_error", headers=headers)
    item = items[0]
    if item.get("kind") == "json":
        from governance.body import canonical_json
        return Response(canonical_json(item.get("data")),
                        media_type="application/json", headers=headers)
    if item.get("kind") == "text":
        return Response(item.get("text", ""),
                        media_type="text/plain; charset=utf-8",
                        headers=headers)
    return await _file_item_bytes(user, item, headers)


async def _file_item_bytes(user, item, headers) -> Response:
    """File item → bytes through the internal NON-counting channel (no
    allowed_downloads consumption). Dead/expired mapping → 410
    result_expired (never re-executes, never re-charges)."""
    from sharing.uploads import get_live_upload_by_public_id
    from storage import read as storage_read
    up = get_live_upload_by_public_id(item.get("public_id") or "")
    if up is None or int(up["user_id"]) != int(user["id"]):
        return _err(410, "result_expired", headers=headers)
    try:
        status, _rh, stream = await asyncio.to_thread(
            storage_read, up["gokapi_id"])
    except Exception:
        return _err(410, "result_expired", headers=headers)
    if status >= 400:
        return _err(410, "result_expired", headers=headers)
    from config import MAX_UPLOAD_BYTES
    data = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return _err(500, "internal_error", headers=headers)
    fn = (item.get("filename") or "result").replace('"', "")
    return Response(data, media_type=item.get("content_type")
                    or "application/octet-stream",
                    headers={**headers,
                             "Content-Disposition":
                                 f'attachment; filename="{fn}"'})


# ── file tools: multipart bytes | JSON {files:[url|ref], params} ──

async def _call_file_tool(request, user, tool, idem, delivery, params):
    from remote import _is_json_media_type
    ctype = (request.headers.get("content-type") or "").split(";")[0] \
        .strip().lower()

    # items: ("new", (filename, ctype, bytes)) | ("ref", (public_id,
    # source_token)) | ("url", (url, filename|None)) — request order
    # preserved end to end
    items = []
    if ctype.startswith("multipart/"):
        form = await request.form()
        for part in form.getlist("file"):
            if not part.filename:
                continue
            data = await part.read()
            items.append(("new", (part.filename,
                                  part.content_type or "", data)))
        if not items:
            return _err(400, "err_select_file")
    elif _is_json_media_type(ctype):
        try:
            body = await request.json()
        except Exception:
            return _err(400, "invalid_request")
        if not isinstance(body, dict):
            return _err(400, "invalid_request")
        spec = body.get("files")
        if not isinstance(spec, list) or not spec:
            return _err(400, "err_select_file")
        for entry in spec:
            if not isinstance(entry, dict):
                return _err(400, "invalid_request")
            keys = set(entry.keys())
            if keys == {"url"}:
                if not isinstance(entry["url"], str) \
                        or not entry["url"].startswith("https://"):
                    return _err(400, "invalid_request")
                items.append(("url", (entry["url"], None)))
            elif keys <= {"public_id", "source_token"} \
                    and "public_id" in keys:
                tok = entry.get("source_token", "")
                if not isinstance(entry["public_id"], str) \
                        or not isinstance(tok, str):
                    return _err(400, "invalid_request")
                items.append(("ref", (entry["public_id"], tok)))
            else:
                return _err(400, "invalid_request")
        if "params" in body:
            if not isinstance(body["params"], dict):
                return _err(400, "invalid_request")
            params = dict(body["params"])
    else:
        return _err(415, "err_content_type",
                    message="multipart/form-data or application/json")

    # pre-gate preflight (entitlement + params) BEFORE any fetch/staging
    from execution.invocation import BODY_ABSENT
    from execution.dispatch import preflight
    _t, declared, _cb, pbody, pstatus = preflight(
        user, tool["tool_key"], delivery, params, body=BODY_ABSENT)
    if pbody is not None:
        return JSONResponse(pbody, pstatus)
    if declared is not None:
        params = declared

    from config import PUBLIC_ID_RE
    from execution.source import resolve_upload
    ref_names = {}
    for i, (kind, val) in enumerate(items):
        if kind != "ref":
            continue
        pid, tok = val
        if not PUBLIC_ID_RE.match(pid):
            return _err(400, "invalid_id")
        upload, rerr = await resolve_upload(pid, user, tok)
        if rerr or not upload:
            status = {"not_found": 404, "forbidden": 403}.get(rerr, 400)
            return _err(status, rerr or "not_found")
        ref_names[i] = upload.get("file_name", "")

    # URL fetch (pinned transport) — AFTER the prechecks above
    from remote import RemoteError
    from app.routes.mcp import _fetch_url_source, _url_upload
    staged_specs = {}   # index → (filename, ctype, body)
    for i, (kind, val) in enumerate(items):
        if kind == "new":
            staged_specs[i] = val
        elif kind == "url":
            url, fname = val
            try:
                data = await asyncio.to_thread(_fetch_url_source, url)
            except RemoteError as exc:
                return _err(400, exc.code,
                            message=str(exc.detail)[:200])
            staged_specs[i] = _url_upload(url, fname, data)

    # sniff 收容 + accept 闸门 + batch 上限 (与 MCP 同一规则)
    from governance.ingress import validate_batch
    from security import mismatch_detail, sniff_allowed
    from execution.dispatch import source_accept_error
    from utils import sanitize_filename
    canon = []
    for i in sorted(staged_specs):
        fn, ct, data = staged_specs[i]
        ok, final_t, final_fn = sniff_allowed(fn, ct, data[:512])
        if not ok:
            return _err(400, final_t,
                        **mismatch_detail(sanitize_filename(fn), data[:512]))
        canon.append((sanitize_filename(final_fn), final_t, data))
    names = {**ref_names,
             **{i: c[0] for i, c in zip(sorted(staged_specs), canon)}}
    aerr = source_accept_error(tool, [names.get(i, "")
                                      for i in range(len(items))])
    if aerr:
        return _err(400, aerr)
    bdenial = validate_batch([len(b) for _, _, b in canon])
    if bdenial:
        status = 400 if bdenial.get("error") == "err_too_many_files" else 413
        return JSONResponse({"ok": False, **bdenial}, status)

    # 暂存单点 (P4): idem key = staging client_ref
    infos = []
    if canon:
        import hashlib as _hl
        from sharing.ingress_identity import ingress_request_hash
        from sharing.ingress_receipt import (IngressConflict,
                                             IngressInProgress,
                                             IngressQuotaDenied,
                                             IngressUnresolved)
        from sharing.staging import stage_sources, StagingError
        ctx = {"tool_id": tool["tool_key"]}
        if any(kind != "new" for kind, _ in items):
            ctx["sources"] = [
                {"kind": "url", "url": val[0]} if kind == "url"
                else {"kind": "ref", "public_id": val[0]} if kind == "ref"
                else {"kind": "new"}
                for kind, val in items]
        req_hash = ingress_request_hash(
            "v1_tool_source",
            [{"filename": fn, "content_type": ct, "size": len(b),
              "sha256": _hl.sha256(b).hexdigest()} for fn, ct, b in canon],
            ctx)
        try:
            staged = await stage_sources(
                int(user["id"]), client_ip(request), idem,
                tool["tool_key"], req_hash, canon)
        except IngressConflict:
            return _err(409, "idempotency_conflict")
        except IngressInProgress:
            return _err(409, "err_upload_in_progress")
        except IngressQuotaDenied as exc:
            return JSONResponse({"ok": False, **exc.denial}, 429)
        except IngressUnresolved:
            return _err(500, "err_ingress_unresolved")
        except StagingError:
            logger.exception("v1 source staging failed")
            return _err(500, "err_upload_failed")
        infos = staged if isinstance(staged, list) else [staged]

    # canonical source order = request order (staged handles + references)
    ingested = iter(infos)
    source_files = []
    for kind, val in items:
        if kind == "ref":
            source_files.append({"public_id": val[0],
                                 "source_token": val[1]})
        else:
            info = next(ingested)
            source_files.append({"public_id": info["stagingId"],
                                 "source_token": info.get("sourceToken", "")})

    payload, ebody, estatus = await _dispatch(request, user, {
        "tool_id": tool["tool_key"],
        "source": {"files": source_files},
        "params": params,
        "delivery": delivery,
        "client_ref": idem,
    })
    if ebody is not None:
        return JSONResponse(ebody, estatus)
    result = payload.get("result") or {}
    return JSONResponse({
        "ok": True,
        "call_id": payload.get("call_id"),
        "cost": payload.get("cost", 0),
        "items": result.get("items") or [],
    })
