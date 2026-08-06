"""API routes: /api/v1/*

RESTful JSON endpoints for uploads, tools, and user data.

Response format (all endpoints):
    Success: {"ok": true, ...data}
    Error:   {"ok": false, "error": "error_code"}
"""

import io
import sys
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
from gokapi import upload_to_gokapi
from security import sniff_allowed
from storage import read as storage_read, write as storage_write
from tools import get_tool, get_default_preset, list_all_tools, get_tools_for_upload, TYPE_PRODUCE, ToolError
from utils import now, sanitize_filename, generate_public_id, public_file_url

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


# ── Tools ──

@router.get("/tools")
async def get_tools():
    """List all registered tools."""
    return {"ok": True, "tools": list_all_tools()}


@router.get("/tools/{public_id}")
async def get_tools_for_file(public_id: str):
    """List tools applicable to a specific file."""
    if not PUBLIC_ID_RE.match(public_id):
        return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
    upload = get_upload_by_public_id(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    upload_dict = dict(upload)
    upload_dict["is_owner"] = False
    tools = get_tools_for_upload(upload_dict)
    return {"ok": True, "tools": tools, "is_owner": upload_dict["is_owner"]}


@router.post("/tools/execute")
async def execute_tool(request: Request):
    """Execute a tool on an uploaded file.

    Body validated by Pydantic ToolExecuteRequest.
    Runs handler in subprocess for memory isolation.
    """
    user, err = _require_auth(request)
    if err:
        return err

    try:
        raw = await request.json()
        from app.models import ToolExecuteRequest
        req = ToolExecuteRequest(**raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)

    tool_id = req.tool_id
    public_id = req.public_id
    params = req.params

    if not PUBLIC_ID_RE.match(public_id):
        return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
    upload = get_upload_by_public_id(public_id)
    if not upload:
        # Tool flow: original was uploaded to gokapi but not recorded in DB.
        # Reconstruct upload dict from gokapi metadata.
        from gokapi import get_gokapi_file
        gokapi_info = get_gokapi_file(public_id)
        if not gokapi_info:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        upload = {
            "public_id": public_id,
            "gokapi_id": public_id,
            "file_name": gokapi_info.get("Name", "upload"),
            "content_type": gokapi_info.get("ContentType", "application/octet-stream"),
            "size_bytes": int(gokapi_info.get("SizeBytes", 0)),
            "url_hotlink": gokapi_info.get("UrlHotlink", ""),
            "url_download": gokapi_info.get("UrlDownload", ""),
        }
    tool = get_tool(tool_id)
    if not tool:
        return JSONResponse({"ok": False, "error": "tool_not_found"}, 404)

    # Resolve default preset if not specified
    if "preset" not in params:
        default = get_default_preset(tool_id)
        if default:
            params["preset"] = default

    try:
        result = await _execute_and_share(request, tool, upload, params, user)
        return {"ok": True, "type": tool["type"], "result": result}
    except ToolError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    except Exception:
        logger.exception("tool execution failed")
        return JSONResponse({"ok": False, "error": "tool_failed"}, 500)


async def _execute_and_share(request: Request, tool: dict, upload, params: dict, user: dict) -> dict:
    """Orchestration layer.

    Two modes:
    - LOCAL (default): storage.read → subprocess handler → storage.write → DB
    - REMOTE (WORKER_URL set): HTTP to remote worker → worker pulls/pushes gokapi → DB only
    """
    import os
    WORKER_URL = os.environ.get("FLASHSHARE_WORKER_URL", "")
    upload_dict = dict(upload)

    # ── Lifecycle params (shared by both modes) ──
    expiry_days = params.get("expiry_days", 1)
    if expiry_days not in (1, 3, 7):
        expiry_days = 1
    allowed_downloads = params.get("allowed_downloads", 50)
    if allowed_downloads not in (1, 10, 50, 100, 200):
        allowed_downloads = 50

    if WORKER_URL:
        # ── REMOTE WORKER: main server never touches file bytes ──
        import http.client, urllib.parse
        from config import GOKAPI_URL, GOKAPI_KEY

        payload = json.dumps({
            "tool_id": tool["id"],
            "gokapi_id": upload_dict["gokapi_id"],
            "gokapi_url": GOKAPI_URL,
            "gokapi_key": GOKAPI_KEY,
            "filename": upload_dict["file_name"],
            "content_type": upload_dict["content_type"],
            "size": upload_dict["size_bytes"],
            "params": {**params, "expiry_days": expiry_days, "allowed_downloads": allowed_downloads},
        })
        parsed = urllib.parse.urlparse(WORKER_URL)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 9000, timeout=120)
        conn.request("POST", "/process", body=payload, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp_data = resp.read()
        conn.close()
        if resp.status != 200:
            raise RuntimeError(f"worker error: {resp_data.decode('utf-8', 'replace')[:200]}")
        wr = json.loads(resp_data.decode("utf-8"))
        if not wr.get("ok"):
            raise RuntimeError(f"worker failed: {wr.get('error', 'unknown')}")

        result = wr["result"]
        # Write DB record only — file already in gokapi
        ip = _client_ip(request)
        with db() as con:
            new_public_id = generate_public_id(con)
            con.execute(
                """INSERT INTO uploads(user_id, ip, public_id, gokapi_id, file_name, content_type, size_bytes,
                   expiry_days, allowed_downloads, expire_at, url_download, url_hotlink, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user["id"], ip, new_public_id, result["gokapi_id"],
                 result["filename"], result["content_type"], result["size"],
                 expiry_days, allowed_downloads, result.get("expire_at"),
                 result.get("url_download", ""), result.get("url_hotlink", ""), now()),
            )
        return {
            "type": "share_link",
            "url": public_file_url(new_public_id),
            "filename": result["filename"],
            "contentType": result["content_type"],
            "size": result["size"],
            "origSize": result.get("orig_size", upload_dict["size_bytes"]),
            "publicId": new_public_id,
            "expireAt": result.get("expire_at"),
            "expiryDays": expiry_days,
            "allowedDownloads": allowed_downloads,
        }

    # ── LOCAL WORKER: subprocess (current logic) ──
    status, headers, stream = storage_read(upload_dict["gokapi_id"])
    source_bytes = stream.read()

    loop = asyncio.get_event_loop()
    from app.worker import process_tool
    result = await loop.run_in_executor(
        _get_pool(),
        process_tool,
        tool["id"], source_bytes, upload_dict["file_name"],
        upload_dict["content_type"], upload_dict["size_bytes"], params,
    )

    output_bytes = result["output_bytes"]

    # Write to storage
    gokapi_result = storage_write(
        io.BytesIO(output_bytes),
        result["filename"],
        result["content_type"],
        expiry_days,
        allowed_downloads,
        len(output_bytes),
    )

    # DB record
    ip = _client_ip(request)
    info = gokapi_result.get("FileInfo", {})
    with db() as con:
        new_public_id = generate_public_id(con)
        con.execute(
            """INSERT INTO uploads(user_id, ip, public_id, gokapi_id, file_name, content_type, size_bytes,
               expiry_days, allowed_downloads, expire_at, url_download, url_hotlink, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user["id"], ip, new_public_id, info.get("Id", ""),
             result["filename"], result["content_type"], len(output_bytes),
             expiry_days, allowed_downloads, info.get("ExpireAt"),
             info.get("UrlDownload", ""), info.get("UrlHotlink", ""), now()),
        )

    return {
        "type": "share_link",
        "url": public_file_url(new_public_id),
        "filename": result["filename"],
        "contentType": result["content_type"],
        "size": len(output_bytes),
        "origSize": upload_dict["size_bytes"],
        "publicId": new_public_id,
        "expireAt": info.get("ExpireAt"),
        "expiryDays": expiry_days,
        "allowedDownloads": allowed_downloads,
    }


# ── Uploads ──

@router.get("/uploads")
async def get_uploads(request: Request):
    """List current user's uploads."""
    user, err = _require_auth(request)
    if err:
        return err
    uploads = list_uploads(user["id"])
    return {"ok": True, "uploads": uploads}


@router.post("/uploads")
async def create_upload(request: Request):
    """Upload a file via multipart form.

    Form fields: file (required), expiryDays, allowedDownloads
    """
    user, err = _require_auth(request)
    if err:
        return err

    form = await request.form()
    file = form.get("file")
    if not file or not file.filename:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    content = await file.read()
    size = len(content)
    if size > MAX_UPLOAD_BYTES:
        return JSONResponse({"ok": False, "error": "err_file_too_large"}, 413)
    if size == 0:
        return JSONResponse({"ok": False, "error": "err_empty_file"}, 400)

    ip = _client_ip(request)
    ok, reason = check_quota(user["id"], ip, size)
    if not ok:
        return JSONResponse({"ok": False, "error": reason}, 429)

    filename = sanitize_filename(file.filename or "upload")
    valid, final_type = sniff_allowed(filename, file.content_type or "", content[:512])
    if not valid:
        return JSONResponse({"ok": False, "error": final_type}, 400)

    # Form fields (from <select> elements)
    try:
        expiry_days = int(form.get("expiryDays", "1"))
    except (ValueError, TypeError):
        expiry_days = 1
    if expiry_days not in (1, 3, 7):
        expiry_days = 1
    try:
        allowed_downloads = int(form.get("allowedDownloads", "50"))
    except (ValueError, TypeError):
        allowed_downloads = 50
    if allowed_downloads not in (1, 10, 50, 100, 200):
        allowed_downloads = 50

    has_tool = form.get("tool", "").strip()
    gokapi = upload_to_gokapi(io.BytesIO(content), filename, final_type,
                              expiry_days, allowed_downloads, size)
    info = gokapi.get("FileInfo", {})

    # When a tool is selected, the original file is just a temporary intermediate.
    # Only the tool output should get a DB record (written by _execute_and_share).
    if has_tool and has_tool != "share":
        return {
            "ok": True,
            "file": {
                "name": filename,
                "contentType": final_type,
                "size": size,
                "downloadUrl": "",
                "hotlinkUrl": info.get("UrlHotlink", ""),
                "publicId": info.get("Id", ""),
                "gokapiId": info.get("Id", ""),
                "expireAt": info.get("ExpireAt"),
                "expiryDays": expiry_days,
                "allowedDownloads": allowed_downloads,
            },
        }

    # No tool (or share = default): save original as the shareable record
    with db() as con:
        public_id = generate_public_id(con)
        con.execute(
            """INSERT INTO uploads(user_id, ip, public_id, gokapi_id, file_name, content_type, size_bytes,
               expiry_days, allowed_downloads, expire_at, url_download, url_hotlink, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user["id"], ip, public_id, info.get("Id", ""), filename, final_type, size,
             expiry_days, allowed_downloads, info.get("ExpireAt"), info.get("UrlDownload", ""),
             info.get("UrlHotlink", ""), now()),
        )
    return {
        "ok": True,
        "file": {
            "name": filename,
            "contentType": final_type,
            "size": size,
            "downloadUrl": public_file_url(public_id),
            "hotlinkUrl": "",
            "publicId": public_id,
            "expireAt": info.get("ExpireAt"),
            "expiryDays": expiry_days,
            "allowedDownloads": allowed_downloads,
        }
    }


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
        from app.models import RenameRequest
        req = RenameRequest(**raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    new_name = req.name.strip()
    import os as _os
    original_ext = _os.path.splitext(upload["file_name"])[1]
    clean_stem = sanitize_filename(new_name)
    clean_stem = _os.path.splitext(clean_stem)[0]
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
    from auth import public_user
    return {"ok": True, "user": public_user(user)}
