"""Internal API routes: /api/*

These endpoints are called exclusively by the frontend (app.js + detail.html).
Not for external consumption — no version prefix, no public documentation.

Response format (all endpoints):
    Success: {"ok": true, ...data}
    Error:   {"ok": false, "error": "error_code"}
"""

import io
import os
import sys
import json
import asyncio
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from concurrent.futures import ProcessPoolExecutor

_CORE = Path(__file__).resolve().parent.parent.parent / "core"
sys.path.insert(0, str(_CORE))

from config import MAX_UPLOAD_BYTES, MAX_BATCH_BYTES, PUBLIC_ID_RE, BASE_URL
from database import db
from uploads import check_quota, get_upload_by_public_id, list_uploads, delete_upload
from gokapi import upload_to_gokapi, get_gokapi_file, delete_gokapi_file
from security import sniff_allowed, sign_source_token, verify_source_token
from storage import read as storage_read, write as storage_write
from tools import ToolError
from catalog import resolve as resolve_tool, get_default_preset
from auth import public_user
from utils import now, sanitize_filename, generate_public_id, public_file_url
from app.models import ToolExecuteRequest, RenameRequest
from app.worker import process_tool

logger = logging.getLogger("flashshare")
router = APIRouter()

# Worker pool for tool processing (subprocess isolation)
_process_pool = ProcessPoolExecutor(max_workers=2)


def _get_pool():
    return _process_pool


# ── Metering helpers (single gate at execute_tool; see pricing.py/billing.py) ──

def _finish_metered_call(user, tool, client_ref, cost, started,
                         request, result, upload) -> dict:
    """Record the successful call fact + return payload (with replay flag off)."""
    try:
        rid = None
        if upload is not None:
            rid = dict(upload).get("id")
        with db() as con:
            row = con.execute(
                """INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
                   cost_snapshot, upload_id, result_public_id, params_json,
                   result_json, started_at, finished_at)
                   VALUES (%s, %s, %s, 'ok', %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (client_ref, user["id"], tool["id"], cost, rid,
                 result.get("publicId"), None,
                 json.dumps(result)[:4000], started, now()),
            ).fetchone()
            call_id = row["id"]
    except Exception:
        logger.exception("metering record failed (call still returned)")
        call_id = None
    out = {"ok": True, "type": tool["type"], "result": result}
    if call_id:
        out["call_id"] = call_id
    # Authoritative post-call balance for the UI credit chip
    if cost > 0:
        try:
            from billing import get_balance
            out["balance"] = get_balance(user["id"])
        except Exception:
            logger.exception("balance fetch failed for UI chip")
    return out


def _record_failed_call(user, tool, client_ref, cost, started, error) -> None:
    try:
        with db() as con:
            con.execute(
                """INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
                   cost_snapshot, error, started_at, finished_at)
                   VALUES (%s, %s, %s, 'failed', %s, %s, %s, %s)""",
                (client_ref, user["id"], tool["id"], cost,
                 str(error)[:500], started, now()))
    except Exception:
        logger.exception("failed-call record failed")


def _refund_call(user, client_ref, tool_id, cost, reason) -> None:
    """Refund a charged call that failed — new ledger row, never an UPDATE."""
    from billing import refund
    if cost <= 0:
        return
    try:
        # Ordinal = rows recorded for this ref INCLUDING the just-failed one
        with db() as con:
            n = con.execute(
                "SELECT COUNT(*) AS n FROM tool_calls WHERE user_id = %s AND client_ref = %s",
                (user["id"], client_ref),
            ).fetchone()["n"]
        refund(user["id"], cost, ref_type="tool_call",
               ref_id=client_ref,
               idem_key=f"refund:call:{user['id']}:{client_ref}:{int(n) - 1}",
               note=f"{tool_id}: {reason}"[:200])
    except Exception:
        logger.exception("refund failed for call %s", client_ref)


def _client_ip(request: Request) -> str:
    # Nginx sets X-Real-IP from $remote_addr (overwritten, not appended),
    # so it cannot be spoofed by the client. X-Forwarded-For is append-mode
    # and its first segment is client-controlled — never trust it.
    return request.headers.get("X-Real-IP", request.client.host if request.client else "").strip()


def _require_auth(request: Request):
    """Return user dict or JSONResponse error."""
    user = request.state.user
    if not user:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    return user, None


def _save_upload_record(user_id, ip, gokapi_id, filename, content_type, size,
                         expiry_days, allowed_downloads, expire_at,
                         url_hotlink):
    """Insert a DB record for an upload and return the response dict.
    url_download is generated from public_id (not passed in)."""
    with db() as con:
        public_id = generate_public_id(con)
        dl_url = public_file_url(public_id)
        con.execute(
            """INSERT INTO uploads(user_id, ip, public_id, gokapi_id, file_name, content_type, size_bytes,
               expiry_days, allowed_downloads, expire_at, url_download, url_hotlink, created_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (user_id, ip, public_id, gokapi_id, filename, content_type, size,
             expiry_days, allowed_downloads, expire_at, dl_url, url_hotlink, now()),
        )
    return {
        "type": "share_link",
        "name": filename,
        "downloadUrl": dl_url,
        "printUrl": (f"{BASE_URL}/f/{public_id}/print" if content_type == "application/pdf" else None),
        "filename": filename,
        "contentType": content_type,
        "size": size,
        "publicId": public_id,
        "expireAt": expire_at,
        "expiryDays": expiry_days,
        "allowedDownloads": allowed_downloads,
    }


def _validate_lifecycle(params):
    """Extract and validate expiry_days + allowed_downloads from params."""
    try:
        expiry_days = int(params.get("expiry_days", 1))
    except (ValueError, TypeError):
        expiry_days = 1
    if expiry_days not in (1, 3, 7):
        expiry_days = 1
    try:
        allowed_downloads = int(params.get("allowed_downloads", 50))
    except (ValueError, TypeError):
        allowed_downloads = 50
    if allowed_downloads not in (1, 10, 50, 100, 200):
        allowed_downloads = 50
    return expiry_days, allowed_downloads


# ── Tools ──
# (GET /api/tools and /api/tools/{public_id} were removed — the frontend uses
#  server-rendered tools_json on the home page; the only consumer is the
#  execute endpoint below.)


async def _resolve_upload(public_id: str, user: dict, token: str = ""):
    """Resolve a public_id to an upload dict (DB or gokapi fallback).

    Ownership: a DB record must belong to the requesting user. A gokapi-only
    intermediate (tool flow, no DB record) requires a valid source token
    signed for (user_id, file_id) at upload time.
    Runs in a thread: the gokapi fallback is a blocking HTTP call.
    """
    return await asyncio.to_thread(_resolve_upload_sync, public_id, user, token)


def _resolve_upload_sync(public_id: str, user: dict, token: str):
    upload = get_upload_by_public_id(public_id)
    if upload:
        if upload["user_id"] != user["id"]:
            return None, "forbidden"
        return upload, None
    # Tool flow: original in gokapi but not in DB
    if not public_id.isalnum() or len(public_id) < 8 or len(public_id) > 20:
        return None, "not_found"
    if not verify_source_token(token, user["id"], public_id):
        return None, "forbidden"
    gokapi_info = get_gokapi_file(public_id)
    if not gokapi_info:
        return None, "not_found"
    return {
        "public_id": public_id,
        "gokapi_id": public_id,
        "file_name": gokapi_info.get("Name", "upload"),
        "content_type": gokapi_info.get("ContentType", "application/octet-stream"),
        "size_bytes": int(gokapi_info.get("SizeBytes", 0)),
        "url_hotlink": gokapi_info.get("UrlHotlink", ""),
        "url_download": gokapi_info.get("UrlDownload", ""),
    }, None


@router.post("/tools/execute")
async def execute_tool(request: Request):
    """Execute a tool on one or more uploaded files.

    Body validated by Pydantic ToolExecuteRequest.
    - Single-file: public_id (str) — existing tools (beautify, compress, etc.)
    - Multi-file: public_ids (list) — multi tools (pdf_merge)
    """
    user, err = _require_auth(request)
    if err:
        return err

    try:
        if getattr(request.state, "_v1_raw", None) is not None:
            raw = request.state._v1_raw  # dispatched from /api/v1/execute
        else:
            raw = await request.json()
        req = ToolExecuteRequest(**raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)

    tool_id = req.tool_id
    params = req.params
    # Single routing entry: DB registry row (contract) + local handler join.
    # Bare id = first-party; 'owner/tool' = user namespace (Phase 3).
    tool, terr = resolve_tool(tool_id, user["id"])
    if terr:
        # private tools are indistinguishable from missing ones (no leak)
        if terr == "disabled":
            return JSONResponse({"ok": False, "error": "tool_disabled"}, 403)
        return JSONResponse({"ok": False, "error": "tool_not_found"}, 404)
    # Normalize to the legacy in-memory shape so downstream orchestration
    # (metering, worker dispatch, result cards) is untouched.
    tool["id"] = tool["tool_key"]
    tool["type"] = tool["tool_type"]

    # ── Metering gate (single point for ALL execution paths) ──
    # pricing/enabled check + idempotent charge + tool_calls fact row.
    # Web UI passes no client_ref: a random one (no replay protection,
    # matching the UX of click-again-to-retry). /api/v1 + MCP pass
    # client_ref: replay returns the original result without re-charging.
    from pricing import get_pricing
    from billing import consume, lazy_signup_grant, InsufficientCredits, signup_amount
    price = get_pricing(tool_id)
    if not price["enabled"]:
        return JSONResponse({"ok": False, "error": "tool_disabled"}, 403)
    client_ref = req.client_ref or secrets.token_hex(16)
    attempt = 0  # set below after the prior check
    idem_key = f"call:{user['id']}:{client_ref}"  # attempt appended post-check

    # Replay check (idempotent consumers only)
    with db() as con:
        prior = con.execute(
            "SELECT * FROM tool_calls WHERE user_id = %s AND client_ref = %s",
            (user["id"], client_ref),
        ).fetchone()
        # Attempt ordinal: crash-safety + recharge-after-refund. If the
        # process died between charge and tool_calls insert, the retry
        # lands on the same attempt → consume idem replays (no double
        # charge). A retry AFTER a recorded failure lands on attempt+1
        # → fresh charge (the refund must not become a free pass).
        attempt = con.execute(
            "SELECT COUNT(*) AS n FROM tool_calls WHERE user_id = %s AND client_ref = %s",
            (user["id"], client_ref),
        ).fetchone()["n"]

    if prior:
        # Exact replay: return recorded result, no charge, no re-run
        if prior["status"] == "ok" and prior["result_json"]:
            return {"ok": True, "type": tool["type"],
                    "result": json.loads(prior["result_json"]),
                    "replay": True, "call_id": prior["id"]}
        # Failed prior: allow retry with same ref (charge happens below)

    idem_key = f"call:{user['id']}:{client_ref}:{attempt}"
    cost = int(price["cost"])
    started = now()
    # Lazy signup grant at first billable need (activities-driven amount;
    # amount=0 when the signup activity is disabled → no ledger row)
    if cost > 0:
        s_amount = signup_amount()
        if s_amount > 0:
            with db() as con:
                lazy_signup_grant(con, user["id"], s_amount)
        try:
            charge = consume(user["id"], cost, "consume",
                             ref_type="tool_call", idem_key=idem_key,
                             note=f"{tool_id}")
        except InsufficientCredits as exc:
            con_execute = None
            with db() as con:
                con.execute(
                    """INSERT INTO tool_calls (client_ref, user_id, tool_id, status,
                       cost_snapshot, error, started_at, finished_at)
                       VALUES (%s, %s, %s, 'insufficient', %s, %s, %s, %s)""",
                    (client_ref, user["id"], tool_id, cost,
                     f"balance:{exc.balance}", started, now()))
            return JSONResponse({"ok": False, "error": "err_insufficient_credits",
                                 "balance": exc.balance, "cost": exc.cost}, 402)

    # Resolve default preset if not specified
    if "preset" not in params:
        default = get_default_preset(tool_id)
        if default:
            params["preset"] = default

    try:
        if tool.get("multi") and req.public_ids:
            # Multi-file tool (e.g. pdf_merge)
            if len(req.public_ids) > 20:
                return JSONResponse({"ok": False, "error": "err_too_many_files"}, 400)
            uploads = []
            for i, pid in enumerate(req.public_ids):
                if not PUBLIC_ID_RE.match(pid):
                    return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
                token = req.source_tokens[i] if i < len(req.source_tokens) else ""
                upload, err = await _resolve_upload(pid, user, token)
                if err or not upload:
                    return JSONResponse({"ok": False, "error": err or "not_found"},
                                        403 if err == "forbidden" else 404)
                uploads.append(upload)
            result = await _execute_multi_and_share(request, tool, uploads, params, user)
            return _finish_metered_call(user, tool, client_ref, cost, started,
                                        request, result, None)
        else:
            # Single-file tool (existing behavior)
            public_id = req.public_id
            if not public_id:
                return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
            if not PUBLIC_ID_RE.match(public_id):
                return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
            upload, err = await _resolve_upload(public_id, user, req.source_token)
            if err or not upload:
                return JSONResponse({"ok": False, "error": err or "not_found"},
                                    403 if err == "forbidden" else 404)
            result = await _execute_and_share(request, tool, upload, params, user)
        return _finish_metered_call(user, tool, client_ref, cost, started,
                                    request, result, upload)
    except ToolError as exc:
        _record_failed_call(user, tool, client_ref, cost, started, str(exc))
        _refund_call(user, client_ref, tool_id, cost, str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    except Exception:
        logger.exception("tool execution failed")
        _record_failed_call(user, tool, client_ref, cost, started, "tool_failed")
        _refund_call(user, client_ref, tool_id, cost, "tool_failed")
        return JSONResponse({"ok": False, "error": "tool_failed"}, 500)


async def _execute_and_share(request: Request, tool: dict, upload, params: dict, user: dict) -> dict:
    """Orchestration: read source → run tool handler → write result → DB record → cleanup source."""
    upload_dict = dict(upload)
    source_gokapi_id = upload_dict["gokapi_id"]
    expiry_days, allowed_downloads = _validate_lifecycle(params)

    # 1. Read source from storage
    status, headers, stream = storage_read(source_gokapi_id)
    source_bytes = stream.read()
    source_size = len(source_bytes)

    # 2. Run handler in subprocess (memory isolation)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _get_pool(),
        process_tool,
        tool["id"], source_bytes, upload_dict["file_name"],
        upload_dict["content_type"], upload_dict["size_bytes"], params,
    )
    del source_bytes  # free memory before write

    output_bytes = result["output_bytes"]
    output_size = len(output_bytes)

    # 3. Write result to storage
    gokapi_result = storage_write(
        io.BytesIO(output_bytes),
        result["filename"],
        result["content_type"],
        expiry_days,
        allowed_downloads,
        output_size,
    )

    # 4. DB record
    ip = _client_ip(request)
    info = gokapi_result.get("FileInfo", {})
    record = _save_upload_record(
        user["id"], ip, info.get("Id", ""),
        result["filename"], result["content_type"], output_size,
        expiry_days, allowed_downloads,
        info.get("ExpireAt"), info.get("UrlHotlink", ""),
    )
    record["origSize"] = source_size

    # 5. Cleanup: delete original from storage (it's an intermediate)
    try:
        delete_gokapi_file(source_gokapi_id)
    except Exception:
        logger.warning("Failed to cleanup source %s after tool execution", source_gokapi_id)

    return record


async def _execute_multi_and_share(request: Request, tool: dict, uploads: list, params: dict, user: dict) -> dict:
    """Multi-file orchestration: read all sources → run handler → write result → DB record → cleanup sources."""
    expiry_days, allowed_downloads = _validate_lifecycle(params)
    MAX_MULTI_BYTES = 100 * 1024 * 1024  # 100MB total source limit

    # 1. Read all sources from storage
    multi_sources = []
    source_gokapi_ids = []
    total_size = 0
    for upload in uploads:
        upload_dict = dict(upload) if not isinstance(upload, dict) else upload
        total_size += upload_dict["size_bytes"]
        if total_size > MAX_MULTI_BYTES:
            raise ToolError("err_file_too_large")
        status, headers, stream = storage_read(upload_dict["gokapi_id"])
        source_bytes = stream.read()
        multi_sources.append((
            source_bytes,
            upload_dict["file_name"],
            upload_dict["content_type"],
            upload_dict["size_bytes"],
        ))
        source_gokapi_ids.append(upload_dict["gokapi_id"])

    # 2. Run handler in subprocess with multi_sources
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _get_pool(),
        process_tool,
        tool["id"], b"", "", "", 0, params,
        multi_sources,
    )
    del multi_sources  # free memory

    output_bytes = result["output_bytes"]
    output_size = len(output_bytes)

    # 3. Write result to storage
    gokapi_result = storage_write(
        io.BytesIO(output_bytes),
        result["filename"],
        result["content_type"],
        expiry_days,
        allowed_downloads,
        output_size,
    )

    # 4. DB record
    ip = _client_ip(request)
    info = gokapi_result.get("FileInfo", {})
    record = _save_upload_record(
        user["id"], ip, info.get("Id", ""),
        result["filename"], result["content_type"], output_size,
        expiry_days, allowed_downloads,
        info.get("ExpireAt"), info.get("UrlHotlink", ""),
    )

    # 5. Cleanup: delete all sources from storage (they're intermediates)
    for gid in source_gokapi_ids:
        try:
            delete_gokapi_file(gid)
        except Exception:
            logger.warning("Failed to cleanup source %s after multi-tool execution", gid)

    return record


# ── Uploads ──

@router.get("/uploads")
async def get_uploads(request: Request):
    """List current user's uploads."""
    user, err = _require_auth(request)
    if err:
        return err
    offset = int(request.query_params.get("offset", "0"))
    uploads = list_uploads(user["id"], offset=offset, limit=20)
    return {"ok": True, "uploads": uploads, "has_more": len(uploads) == 20}


@router.post("/uploads")
async def create_upload(request: Request):
    """Upload one or more files via multipart form.

    Form fields: file (one or more), expiryDays, allowedDownloads, tool
    Returns: {ok, files: [...]} for multi-file, {ok, file: {...}} for single-file (backward compat).
    """
    user, err = _require_auth(request)
    if err:
        return err

    form = await request.form()

    # Collect all uploaded files (support both single and multi-file)
    uploads = form.getlist("file")
    if not uploads:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    ip = _client_ip(request)

    # Form fields (shared across all files)
    expiry_days, allowed_downloads = _validate_lifecycle({
        "expiry_days": form.get("expiryDays", "1"),
        "allowed_downloads": form.get("allowedDownloads", "50"),
    })
    has_tool = form.get("tool", "").strip()

    # Pre-check: read all files, validate size, check total quota upfront
    pending = []
    total_incoming = 0
    if len(uploads) > 20:
        return JSONResponse({"ok": False, "error": "err_too_many_files"}, 400)
    # Early size gate from Content-Length (multipart overhead slack included)
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > MAX_BATCH_BYTES + 1024 * 1024:
        return JSONResponse({"ok": False, "error": "err_file_too_large"}, 413)
    for file in uploads:
        if not file.filename:
            continue
        content = await file.read()
        size = len(content)
        if size > MAX_UPLOAD_BYTES:
            return JSONResponse({"ok": False, "error": "err_file_too_large"}, 413)
        if size == 0:
            return JSONResponse({"ok": False, "error": "err_empty_file"}, 400)
        filename = sanitize_filename(file.filename or "upload")
        valid, final_type = sniff_allowed(filename, file.content_type or "", content[:512])
        if not valid:
            return JSONResponse({"ok": False, "error": final_type}, 400)
        total_incoming += size
        if total_incoming > MAX_BATCH_BYTES:
            return JSONResponse({"ok": False, "error": "err_file_too_large"}, 413)
        pending.append((content, size, filename, final_type))

    if not pending:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    # Single quota check for entire batch
    ok, reason = check_quota(user["id"], ip, total_incoming)
    if not ok:
        return JSONResponse({"ok": False, "error": reason}, 429)

    files_result = []

    for content, size, filename, final_type in pending:
        gokapi = upload_to_gokapi(io.BytesIO(content), filename, final_type,
                                  expiry_days, allowed_downloads, size)
        del content
        info = gokapi.get("FileInfo", {})

        files_result.append({
            "name": filename,
            "filename": filename,
            "contentType": final_type,
            "size": size,
            "downloadUrl": "",
            "hotlinkUrl": info.get("UrlHotlink", ""),
            "publicId": info.get("Id", ""),
            "gokapiId": info.get("Id", ""),
            # Ownership proof for tool execution on this gokapi-only intermediate
            "sourceToken": sign_source_token(user["id"], info.get("Id", "")),
            "expireAt": info.get("ExpireAt"),
            "expiryDays": expiry_days,
            "allowedDownloads": allowed_downloads,
        })

    if not files_result:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    # When a tool is selected, originals are intermediates (no DB record).
    # Tool output gets its DB record via _execute_and_share.
    if has_tool and has_tool != "share":
        if len(files_result) == 1:
            return {"ok": True, "file": files_result[0]}
        return {"ok": True, "files": files_result}

    # No tool (or share): save each as a shareable DB record
    records = []
    for i, fr in enumerate(files_result):
        record = _save_upload_record(
            user["id"], ip, fr["gokapiId"],
            fr["filename"], fr["contentType"], fr["size"],
            expiry_days, allowed_downloads,
            fr["expireAt"], fr["hotlinkUrl"],
        )
        records.append(record)

    # Single-file: return "file" key (backward compat with existing frontend)
    if len(records) == 1:
        return {"ok": True, "file": records[0]}
    return {"ok": True, "files": records}


@router.delete("/uploads/{public_id}")
async def delete_upload_route(public_id: str, request: Request):
    """Delete an upload by public_id."""
    user, err = _require_auth(request)
    if err:
        return err
    if not PUBLIC_ID_RE.match(public_id):
        return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
    upload = get_upload_by_public_id(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if upload["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "error": "forbidden"}, 403)
    try:
        delete_upload(upload["id"], user["id"])
    except Exception:
        logger.exception("delete failed for %s", public_id)
        return JSONResponse({"ok": False, "error": "delete_failed"}, 500)
    return {"ok": True}


@router.patch("/uploads/{public_id}")
async def rename_upload_route(public_id: str, request: Request):
    """Rename an upload (only stem, extension preserved).

    Body: {"name": "new_name"}
    """
    user, err = _require_auth(request)
    if err:
        return err
    if not PUBLIC_ID_RE.match(public_id):
        return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
    upload = get_upload_by_public_id(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if upload["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "error": "forbidden"}, 403)

    try:
        raw = await request.json()
        req = RenameRequest(**raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    new_name = req.name.strip()
    original_ext = os.path.splitext(upload["file_name"])[1]
    clean_stem = sanitize_filename(new_name)
    clean_stem = os.path.splitext(clean_stem)[0]
    if not clean_stem or len(clean_stem) > 200:
        return JSONResponse({"ok": False, "error": "invalid_name"}, 400)
    final_name = clean_stem + original_ext
    with db() as con:
        con.execute("UPDATE uploads SET file_name = %s WHERE id = %s", (final_name, upload["id"]))
    return {"ok": True, "name": final_name}


# ── User ──

@router.get("/me")
async def get_me(request: Request):
    """Get current authenticated user."""
    user, err = _require_auth(request)
    if err:
        return err
    return {"ok": True, "user": public_user(user)}


# ── API tokens (agent/MCP channel management; session-authenticated) ──

@router.get("/tokens")
async def list_tokens_route(request: Request):
    """List own tokens (prefix only, no secrets)."""
    user, err = _require_auth(request)
    if err:
        return err
    from apitokens import list_tokens
    return {"ok": True, "tokens": list_tokens(user["id"])}


@router.post("/tokens")
async def create_token_route(request: Request):
    """Create a token. The plaintext is returned exactly once."""
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
        name = str(raw.get("name") or "").strip()[:64]
    except Exception:
        name = ""
    from apitokens import create_token
    tok = create_token(user["id"], name or "token")
    return {"ok": True, "token": tok}


@router.post("/tokens/{token_id}/revoke")
async def revoke_token_route(token_id: int, request: Request):
    """Soft-revoke one of own tokens."""
    user, err = _require_auth(request)
    if err:
        return err
    from apitokens import revoke_token
    if not revoke_token(user["id"], token_id):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True}
