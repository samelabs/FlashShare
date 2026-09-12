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
from fastapi.responses import Response

from app.web_auth import require_session_or_bearer_auth as _require_auth
from app.web_auth import require_session_auth as _require_session

_CORE = Path(__file__).resolve().parent.parent.parent / "core"
sys.path.insert(0, str(_CORE))

from config import MAX_BATCH_BYTES, PUBLIC_ID_RE, BASE_URL
from database import db
from sharing.uploads import (reserve_ingress, release_ingress,
                             get_upload_by_public_id, list_uploads, delete_upload)
from gokapi import upload_to_gokapi, get_gokapi_file, delete_gokapi_file
from security import sniff_allowed, sign_source_token, verify_source_token
from storage import read as storage_read, write as storage_write
from tools import ToolError
from execution.source import resolve_upload
from identity.auth import public_user
from utils import now, sanitize_filename, generate_public_id, public_file_url
from app.models import RenameRequest
from execution.pool import get_pool as _get_pool
from execution.pool import default_process_tool as process_tool

logger = logging.getLogger("flashshare")
router = APIRouter()

from execution.dispatch import client_ip as _client_ip


# ── Tools ──

@router.post("/tools/execute")
async def execute_tool(request: Request):
    """Canonical ToolInvocation JSON → the single dispatch/gate chain."""
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from execution.dispatch import dispatch_execute
    payload, error_body, status = await dispatch_execute(
        request, user, raw, process_tool(), _get_pool())
    if error_body is not None:
        return JSONResponse(error_body, status)
    return payload


@router.get("/source/{gokapi_id}")
async def fetch_source(request: Request, gokapi_id: str):
    """One-time signed fetch used by remote tool endpoints."""
    from security import verify_fetch_token
    token = request.query_params.get("t", "")
    user_id_raw = request.query_params.get("u", "")
    if not token or len(token) > 128 or not user_id_raw.isdigit():
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if not verify_fetch_token(token, int(user_id_raw), gokapi_id):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    info = get_gokapi_file(gokapi_id)
    if not info:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    status, headers, stream = storage_read(gokapi_id)
    if status >= 400:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    data = stream.read()
    ctype = info.get("ContentType") or "application/octet-stream"
    if ctype.startswith("text/html") or data[:15].lower().startswith(b"<!doctype html"):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return Response(
        content=data,
        media_type=ctype,
        headers={"Content-Disposition": "attachment",
                 "X-Content-Type-Options": "nosniff"},
    )


# ── Uploads ──

@router.get("/uploads")
async def get_uploads(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    try:
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        offset = 0
    uploads = list_uploads(user["id"], offset=offset, limit=20)
    return {"ok": True, "uploads": uploads, "has_more": len(uploads) == 20}


@router.post("/client-error")
async def client_error(request: Request):
    """Browser-side crash tap. Diagnostics only."""
    import logging
    try:
        body = await request.json()
        logging.getLogger("flashshare.client").error(
            "CLIENT-CRASH %s %s | %s", body.get("lang"), body.get("url"), body.get("msg"))
        stack = (body.get("stack") or "").replace("\n", " | ")[:1500]
        if stack:
            logging.getLogger("flashshare.client").error("CLIENT-STACK %s", stack)
    except Exception:
        pass
    return {"ok": True}


@router.post("/uploads")
async def create_upload(request: Request):
    """Upload one or more source files.

    Daily quota measures ingress only. A selected tool keeps its uploaded source
    as a short-lived intermediate so the existing retry path can reuse it;
    direct share files become durable uploads rows. Multi-share DB landing is
    all-or-nothing.
    """
    user, err = _require_auth(request)
    if err:
        return err

    form = await request.form()
    uploads = form.getlist("file")
    if not uploads:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    ip = _client_ip(request)
    from governance.delivery import (coerce_share_delivery,
                                     INTERMEDIATE_TTL_DAYS,
                                     INTERMEDIATE_DOWNLOAD_LIMIT)
    expiry_days, allowed_downloads = coerce_share_delivery(
        form.get("expiryDays", "1"), form.get("allowedDownloads", "50"))
    has_tool = form.get("tool", "").strip()
    is_tool_source = bool(has_tool and has_tool != "share")

    # Durable direct share: delivery entitlement check BEFORE any quota
    # reservation or Gokapi storage. Tool-source uploads skip this — their
    # custody is INTERMEDIATE_* platform policy and the canonical execute
    # path owns result-delivery entitlement.
    if not is_tool_source:
        from governance.entitlements import check_delivery
        denial = check_delivery(user["id"], {
            "expiry_days": expiry_days,
            "allowed_downloads": allowed_downloads})
        if denial:
            body = {"ok": False}
            body.update(denial)
            return JSONResponse(body, 403)

    pending = []
    # Transport safety only: a declared Content-Length far beyond the batch
    # cap never needs reading. This is NOT a second batch algorithm — the
    # authoritative source-byte contract stays in governance.ingress
    # validate_batch() below. `incoming` on this early-reject path means the
    # transport-declared request bytes (headers included), never the
    # authoritative source batch total.
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > MAX_BATCH_BYTES + 1024 * 1024:
        return JSONResponse({"ok": False, "error": "err_batch_too_large",
                             "limit": MAX_BATCH_BYTES,
                             "incoming": declared}, 413)

    for file in uploads:
        if not file.filename:
            continue
        content = await file.read()
        size = len(content)
        if size == 0:
            return JSONResponse({"ok": False, "error": "err_empty_file"}, 400)
        filename = sanitize_filename(file.filename or "upload")
        valid, final_type = sniff_allowed(filename, file.content_type or "", content[:512])
        if not valid:
            return JSONResponse({"ok": False, "error": final_type}, 400)
        pending.append((content, size, filename, final_type))

    if not pending:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    # Authoritative platform boundaries: one validator for Web/REST/MCP.
    from governance.ingress import validate_batch
    denial = validate_batch([size for _, size, _, _ in pending])
    if denial:
        status = 400 if denial["error"] == "err_too_many_files" else 413
        return JSONResponse({"ok": False, **denial}, status)

    ok, reason, usage_ids = reserve_ingress(
        user["id"], ip, [size for _, size, _, _ in pending])
    if not ok:
        return JSONResponse({"ok": False, "error": reason}, 429)

    source_expiry = INTERMEDIATE_TTL_DAYS if is_tool_source else expiry_days
    source_downloads = (INTERMEDIATE_DOWNLOAD_LIMIT
                        if is_tool_source else allowed_downloads)

    files_result = []
    uploaded_gokapi_ids = []
    try:
        for content, size, filename, final_type in pending:
            gokapi = upload_to_gokapi(io.BytesIO(content), filename, final_type,
                                      source_expiry, source_downloads, size)
            info = gokapi.get("FileInfo", {})
            gid = info.get("Id", "")
            if not gid:
                raise RuntimeError("gokapi_missing_id")
            uploaded_gokapi_ids.append(gid)
            files_result.append({
                "name": filename,
                "filename": filename,
                "contentType": final_type,
                "size": size,
                "downloadUrl": "",
                "hotlinkUrl": info.get("UrlHotlink", ""),
                "publicId": gid,
                "gokapiId": gid,
                "sourceToken": sign_source_token(user["id"], gid),
                "expireAt": info.get("ExpireAt"),
                "expiryDays": source_expiry,
                "allowedDownloads": source_downloads,
            })
    except Exception:
        for gid in uploaded_gokapi_ids:
            try:
                delete_gokapi_file(gid)
            except Exception:
                logger.warning("rollback: gokapi cleanup failed for %s", gid)
        release_ingress(user["id"], usage_ids)
        logger.exception("multi-upload failed, %d files rolled back",
                         len(uploaded_gokapi_ids))
        return JSONResponse({"ok": False, "error": "err_upload_failed"}, 500)

    if is_tool_source:
        if len(files_result) == 1:
            return {"ok": True, "file": files_result[0]}
        return {"ok": True, "files": files_result}

    # Direct-share landing: all public rows commit together. If DB landing
    # fails, storage and ingress reservation are rolled back as one request.
    from execution.pipeline import save_upload_record
    try:
        records = []
        with db() as con:
            for fr in files_result:
                records.append(save_upload_record(
                    user["id"], ip, fr["gokapiId"],
                    fr["filename"], fr["contentType"], fr["size"],
                    expiry_days, allowed_downloads,
                    fr["expireAt"], fr["hotlinkUrl"], BASE_URL,
                    con=con,
                ))
    except Exception:
        for gid in uploaded_gokapi_ids:
            try:
                delete_gokapi_file(gid)
            except Exception:
                logger.warning("share rollback: gokapi cleanup failed for %s", gid)
        release_ingress(user["id"], usage_ids)
        logger.exception("multi-share DB landing failed")
        return JSONResponse({"ok": False, "error": "err_upload_failed"}, 500)

    if len(records) == 1:
        return {"ok": True, "file": records[0]}
    return {"ok": True, "files": records}


@router.delete("/uploads/{public_id}")
async def delete_upload_route(public_id: str, request: Request):
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
    """Rename an upload (only stem, extension preserved)."""
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
    from repo.uploads import rename as rename_upload_row
    with db() as con:
        rename_upload_row(con, upload["id"], final_name)
    return {"ok": True, "name": final_name}


# ── User ──

@router.get("/me")
async def get_me(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    return {"ok": True, "user": public_user(user)}


# ── API token management: account-control surface, session only ──

@router.get("/tokens")
async def list_tokens_route(request: Request):
    user, err = _require_session(request)
    if err:
        return err
    from identity.apitokens import list_tokens
    return {"ok": True, "tokens": list_tokens(user["id"])}


@router.post("/tokens")
async def create_token_route(request: Request):
    user, err = _require_session(request)
    if err:
        return err
    try:
        raw = await request.json()
        name = str(raw.get("name") or "").strip()[:64]
    except Exception:
        name = ""
    from identity.apitokens import create_token
    tok = create_token(user["id"], name or "token")
    return {"ok": True, "token": tok}


@router.post("/tokens/{token_id}/revoke")
async def revoke_token_route(token_id: int, request: Request):
    user, err = _require_session(request)
    if err:
        return err
    from identity.apitokens import revoke_token
    if not revoke_token(user["id"], token_id):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True}
