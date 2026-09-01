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

from app.web_auth import require_session_or_bearer_auth as _require_auth  # 单点 (治理)

_CORE = Path(__file__).resolve().parent.parent.parent / "core"
sys.path.insert(0, str(_CORE))

from config import MAX_UPLOAD_BYTES, MAX_BATCH_BYTES, PUBLIC_ID_RE, BASE_URL
from database import db
from sharing.uploads import check_quota, get_upload_by_public_id, list_uploads, delete_upload
from gokapi import upload_to_gokapi, get_gokapi_file, delete_gokapi_file
from security import sniff_allowed, sign_source_token, verify_source_token
from storage import read as storage_read, write as storage_write
from tools import ToolError
from governance.catalog import resolve as resolve_tool, get_default_preset
from execution.source import resolve_upload
from identity.auth import public_user
from utils import now, sanitize_filename, generate_public_id, public_file_url
from app.models import RenameRequest
from execution.pool import get_pool as _get_pool
from execution.pool import default_process_tool as process_tool

logger = logging.getLogger("flashshare")
router = APIRouter()



# ── Metering helpers (single gate at execute_tool; see pricing/billing) ──


from execution.dispatch import client_ip as _client_ip  # 单点 (治理3)



# ── Tools ──
# (GET /api/tools and /api/tools/{public_id} were removed — the frontend uses
#  server-rendered tools_json on the home page; the only consumer is the
#  execute endpoint below.)


@router.post("/tools/execute")
async def execute_tool(request: Request):
    """执行入口 (批3a): 解析 → execution.dispatch 统一处理。

    计量/幂等/退费/编排全部在 execution 域; 本函数只管 HTTP 形状。
    v1/MCP/selftest 经 request.state._v1_raw 借道时同样收口到 dispatch
    (raw 由调用方注入)。
    """
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = getattr(request.state, "_v1_raw", None) or await request.json()
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
    """Source-fetch endpoint for remote tools (delivery verb 2).

    URL shape: /api/source/{gokapi_id}?u={user_id}&t={token}. The one-time
    HMAC token (minted at dispatch) is the credential — no session/Bearer
    semantics. It signs (user, file, expiry) and is single-use. Uniform
    404 on any failure (no oracle about which check failed).
    """
    from security import verify_fetch_token
    token = request.query_params.get("t", "")
    user_id_raw = request.query_params.get("u", "")
    if not token or len(token) > 128 or not user_id_raw.isdigit():
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if not verify_fetch_token(token, int(user_id_raw), gokapi_id):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    # gokapi returns 200 + HTML error page for missing files — verify the
    # file exists via the metadata API before streaming (404 shape on miss)
    info = get_gokapi_file(gokapi_id)
    if not info:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    status, headers, stream = storage_read(gokapi_id)
    if status >= 400:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    data = stream.read()
    ctype = info.get("ContentType") or "application/octet-stream"
    if ctype.startswith("text/html") or data[:15].lower().startswith(b"<!doctype html"):
        # defensive: never hand an HTML error page to a remote endpoint
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
    """List current user's uploads."""
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
    """Browser-side crash tap (see base-engine.js __errTap). Diagnostics only."""
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
    from execution.orchestrator import validate_lifecycle
    expiry_days, allowed_downloads = validate_lifecycle({
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

    # 🔴8 修复 (批2e): 多文件上传中途失败 → 回滚已传 gokapi 件再抛错,
    # 不再依赖 gokapi 过期兜底 (skill 记录的 Multi-file rollback 回归)。
    uploaded_gokapi_ids = []
    try:
        for content, size, filename, final_type in pending:
            gokapi = upload_to_gokapi(io.BytesIO(content), filename, final_type,
                                      expiry_days, allowed_downloads, size)
            del content
            info = gokapi.get("FileInfo", {})
            uploaded_gokapi_ids.append(info.get("Id", ""))

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
    except Exception:
        for gid in uploaded_gokapi_ids:
            try:
                delete_gokapi_file(gid)
            except Exception:
                logger.warning("rollback: gokapi cleanup failed for %s", gid)
        logger.exception("multi-upload failed, %d files rolled back",
                         len(uploaded_gokapi_ids))
        return JSONResponse({"ok": False, "error": "err_upload_failed"}, 500)

    if not files_result:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    # When a tool is selected, originals are intermediates (no DB record).
    # Tool output gets its DB record via the execution pipeline.
    if has_tool and has_tool != "share":
        if len(files_result) == 1:
            return {"ok": True, "file": files_result[0]}
        return {"ok": True, "files": files_result}

    # No tool (or share): save each as a shareable DB record
    from execution.pipeline import save_upload_record
    records = []
    for i, fr in enumerate(files_result):
        record = save_upload_record(
            user["id"], ip, fr["gokapiId"],
            fr["filename"], fr["contentType"], fr["size"],
            expiry_days, allowed_downloads,
            fr["expireAt"], fr["hotlinkUrl"], BASE_URL,
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
    from repo.uploads import rename as rename_upload_row
    with db() as con:
        rename_upload_row(con, upload["id"], final_name)
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
    from identity.apitokens import list_tokens
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
    from identity.apitokens import create_token
    tok = create_token(user["id"], name or "token")
    return {"ok": True, "token": tok}


@router.post("/tokens/{token_id}/revoke")
async def revoke_token_route(token_id: int, request: Request):
    """Soft-revoke one of own tokens."""
    user, err = _require_auth(request)
    if err:
        return err
    from identity.apitokens import revoke_token
    if not revoke_token(user["id"], token_id):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True}
