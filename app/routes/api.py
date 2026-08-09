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
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from concurrent.futures import ProcessPoolExecutor

_CORE = Path(__file__).resolve().parent.parent.parent / "core"
sys.path.insert(0, str(_CORE))

from config import MAX_UPLOAD_BYTES, PUBLIC_ID_RE
from database import db
from uploads import check_quota, get_upload_by_public_id, list_uploads, delete_upload
from gokapi import upload_to_gokapi, get_gokapi_file, delete_gokapi_file
from security import sniff_allowed
from storage import read as storage_read, write as storage_write
from tools import get_tool, get_default_preset, list_all_tools, get_tools_for_upload, ToolError
from auth import public_user
from utils import now, sanitize_filename, generate_public_id, public_file_url
from app.models import ToolExecuteRequest, RenameRequest
from app.worker import process_tool

logger = logging.getLogger("flashshare")
router = APIRouter()

# Worker pool for tool processing (subprocess isolation)
_process_pool = None


def _get_pool():
    global _process_pool
    if _process_pool is None:
        _process_pool = ProcessPoolExecutor(max_workers=2)
    return _process_pool


def _client_ip(request: Request) -> str:
    return request.headers.get("X-Forwarded-For", request.client.host if request.client else "").split(",")[0].strip()


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
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, ip, public_id, gokapi_id, filename, content_type, size,
             expiry_days, allowed_downloads, expire_at, dl_url, url_hotlink, now()),
        )
    return {
        "type": "share_link",
        "name": filename,
        "downloadUrl": dl_url,
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

@router.get("/tools")
async def get_tools():
    """List all registered tools."""
    return {"ok": True, "tools": list_all_tools()}


@router.get("/tools/{public_id}")
async def get_tools_for_file(public_id: str, request: Request):
    """List tools applicable to a specific file (owner only)."""
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
    upload_dict = dict(upload)
    tools = get_tools_for_upload(upload_dict)
    return {"ok": True, "tools": tools}


async def _resolve_upload(public_id: str):
    """Resolve a public_id to an upload dict (DB or gokapi fallback)."""
    return await asyncio.to_thread(_resolve_upload_sync, public_id)


def _resolve_upload_sync(public_id: str):
    """Resolve a public_id to an upload dict (DB or gokapi fallback)."""
    upload = get_upload_by_public_id(public_id)
    if upload:
        return upload
    # Tool flow: original in gokapi but not in DB
    if not public_id.isalnum() or len(public_id) < 8 or len(public_id) > 20:
        return None
    gokapi_info = get_gokapi_file(public_id)
    if not gokapi_info:
        return None
    return {
        "public_id": public_id,
        "gokapi_id": public_id,
        "file_name": gokapi_info.get("Name", "upload"),
        "content_type": gokapi_info.get("ContentType", "application/octet-stream"),
        "size_bytes": int(gokapi_info.get("SizeBytes", 0)),
        "url_hotlink": gokapi_info.get("UrlHotlink", ""),
        "url_download": gokapi_info.get("UrlDownload", ""),
    }


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
        raw = await request.json()
        req = ToolExecuteRequest(**raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)

    tool_id = req.tool_id
    params = req.params
    tool = get_tool(tool_id)
    if not tool:
        return JSONResponse({"ok": False, "error": "tool_not_found"}, 404)

    # Resolve default preset if not specified
    if "preset" not in params:
        default = get_default_preset(tool_id)
        if default:
            params["preset"] = default

    try:
        if tool.get("multi") and req.public_ids:
            # Multi-file tool (e.g. pdf_merge)
            uploads = []
            for pid in req.public_ids:
                if not PUBLIC_ID_RE.match(pid):
                    return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
                upload = await _resolve_upload(pid)
                if not upload:
                    return JSONResponse({"ok": False, "error": "not_found"}, 404)
                uploads.append(upload)
            result = await _execute_multi_and_share(request, tool, uploads, params, user)
        else:
            # Single-file tool (existing behavior)
            public_id = req.public_id
            if not public_id:
                return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
            if not PUBLIC_ID_RE.match(public_id):
                return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
            upload = await _resolve_upload(public_id)
            if not upload:
                return JSONResponse({"ok": False, "error": "not_found"}, 404)
            result = await _execute_and_share(request, tool, upload, params, user)
        return {"ok": True, "type": tool["type"], "result": result}
    except ToolError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    except Exception:
        logger.exception("tool execution failed")
        return JSONResponse({"ok": False, "error": "tool_failed"}, 500)


async def _execute_and_share(request: Request, tool: dict, upload, params: dict, user: dict) -> dict:
    """Orchestration: read source → run tool handler → write result → DB record.

    LOCAL mode only (REMOTE WORKER_URL not deployed).
    """
    upload_dict = dict(upload)
    expiry_days, allowed_downloads = _validate_lifecycle(params)

    # 1. Read source from storage
    status, headers, stream = storage_read(upload_dict["gokapi_id"])
    source_bytes = stream.read()

    # 2. Run handler in subprocess (memory isolation)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _get_pool(),
        process_tool,
        tool["id"], source_bytes, upload_dict["file_name"],
        upload_dict["content_type"], upload_dict["size_bytes"], params,
    )

    output_bytes = result["output_bytes"]

    # 3. Write result to storage
    gokapi_result = storage_write(
        io.BytesIO(output_bytes),
        result["filename"],
        result["content_type"],
        expiry_days,
        allowed_downloads,
        len(output_bytes),
    )

    # 4. DB record
    ip = _client_ip(request)
    info = gokapi_result.get("FileInfo", {})
    record = _save_upload_record(
        user["id"], ip, info.get("Id", ""),
        result["filename"], result["content_type"], len(output_bytes),
        expiry_days, allowed_downloads,
        info.get("ExpireAt"), info.get("UrlHotlink", ""),
    )
    record["origSize"] = upload_dict["size_bytes"]
    return record


async def _execute_multi_and_share(request: Request, tool: dict, uploads: list, params: dict, user: dict) -> dict:
    """Multi-file orchestration: read all sources → run handler → write result → DB record."""
    expiry_days, allowed_downloads = _validate_lifecycle(params)

    # 1. Read all sources from storage
    multi_sources = []
    for upload in uploads:
        upload_dict = dict(upload) if not isinstance(upload, dict) else upload
        status, headers, stream = storage_read(upload_dict["gokapi_id"])
        source_bytes = stream.read()
        multi_sources.append((
            source_bytes,
            upload_dict["file_name"],
            upload_dict["content_type"],
            upload_dict["size_bytes"],
        ))

    # 2. Run handler in subprocess with multi_sources
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _get_pool(),
        process_tool,
        tool["id"], b"", "", "", 0, params,
        multi_sources,
    )

    output_bytes = result["output_bytes"]

    # 3. Write result to storage
    gokapi_result = storage_write(
        io.BytesIO(output_bytes),
        result["filename"],
        result["content_type"],
        expiry_days,
        allowed_downloads,
        len(output_bytes),
    )

    # 4. DB record
    ip = _client_ip(request)
    info = gokapi_result.get("FileInfo", {})
    record = _save_upload_record(
        user["id"], ip, info.get("Id", ""),
        result["filename"], result["content_type"], len(output_bytes),
        expiry_days, allowed_downloads,
        info.get("ExpireAt"), info.get("UrlHotlink", ""),
    )
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

    files_result = []

    for file in uploads:
        if not file.filename:
            continue

        content = await file.read()
        size = len(content)
        if size > MAX_UPLOAD_BYTES:
            for fr in files_result:
                try: delete_gokapi_file(fr["gokapiId"])
                except Exception: pass
            return JSONResponse({"ok": False, "error": "err_file_too_large"}, 413)
        if size == 0:
            for fr in files_result:
                try: delete_gokapi_file(fr["gokapiId"])
                except Exception: pass
            return JSONResponse({"ok": False, "error": "err_empty_file"}, 400)

        ok, reason = check_quota(user["id"], ip, size)
        if not ok:
            for fr in files_result:
                try: delete_gokapi_file(fr["gokapiId"])
                except Exception: pass
            return JSONResponse({"ok": False, "error": reason}, 429)

        filename = sanitize_filename(file.filename or "upload")
        valid, final_type = sniff_allowed(filename, file.content_type or "", content[:512])
        if not valid:
            for fr in files_result:
                try: delete_gokapi_file(fr["gokapiId"])
                except Exception: pass
            return JSONResponse({"ok": False, "error": final_type}, 400)

        gokapi = upload_to_gokapi(io.BytesIO(content), filename, final_type,
                                  expiry_days, allowed_downloads, size)
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
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
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
        con.execute("UPDATE uploads SET file_name = ? WHERE id = ?", (final_name, upload["id"]))
    return {"ok": True, "name": final_name}


# ── User ──

@router.get("/me")
async def get_me(request: Request):
    """Get current authenticated user."""
    user, err = _require_auth(request)
    if err:
        return err
    return {"ok": True, "user": public_user(user)}
