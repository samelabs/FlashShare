"""Public API v1 — /api/v1/* (batch C3).

The programmatic surface of the capability platform. Same identity
(Bearer token), same single metering gate (execute funnels through the
internal pipeline), same storage. Five endpoints:

    GET  /api/v1/me          — identity + credit balance
    GET  /api/v1/tools       — capability catalog (register() minus
                               handlers + pricing)
    POST /api/v1/uploads     — upload source (gokapi intermediate +
                               sourceToken; tool flow parity with web)
    POST /api/v1/execute     — run a tool (client_ref idempotent)
    GET  /api/v1/uploads/{id} — fetch result bytes or metadata

Response contract mirrors the internal API ({"ok": true, ...}) with one
addition: programmatic errors never leak stack traces, and execute
accepts/echoes client_ref for retry-safe agents.
"""

import io
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import MAX_UPLOAD_BYTES
from database import db
from security import sniff_allowed
from uploads import check_quota
from gokapi import upload_to_gokapi
from storage import read as storage_read
from tools import get_tool, _registry
from utils import now, sanitize_filename
from auth import public_user

router = APIRouter()


def _require_bearer(request: Request):
    """v1 is token-only. Session cookies are NOT accepted here — the
    programmatic surface must not accidentally depend on browser state."""
    user = getattr(request.state, "user", None)
    via = getattr(request.state, "auth_via", "session")
    if not user:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    if via != "token":
        return None, JSONResponse({"ok": False, "error": "bearer_required"}, 401)
    return user, None


@router.get("/me")
async def v1_me(request: Request):
    user, err = _require_bearer(request)
    if err:
        return err
    from billing import get_balance
    u = public_user(user)
    u.pop("email", None)  # token channel: no PII beyond what's needed
    return {"ok": True, "user": u, "credits": get_balance(user["id"])}


@router.get("/tools")
async def v1_tools(request: Request):
    user, err = _require_bearer(request)
    if err:
        return err
    from pricing import get_pricing
    out = []
    for t in _registry:
        p = get_pricing(t["id"])
        if not p["enabled"]:
            continue  # catalog hides disabled tools
        out.append({
            "id": t["id"],
            "type": t["type"],
            "label": t["label"],
            "icon": t["icon"],
            "accept": sorted(t["accept"]) if t["accept"] else None,
            "multi": t.get("multi", False),
            "cost": int(p["cost"]),
        })
    return {"ok": True, "tools": out}


@router.post("/uploads")
async def v1_upload(request: Request):
    """Upload a source file. Returns the gokapi intermediate handle +
    sourceToken — the same object the web frontend gets after upload
    when a tool is selected. Parity keeps execute identical."""
    user, err = _require_bearer(request)
    if err:
        return err
    try:
        ctype = request.headers.get("content-type", "")
        filename = sanitize_filename(request.headers.get("x-filename", "file"))
        size = int(request.headers.get("content-length", "0") or 0)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if size <= 0 or size > MAX_UPLOAD_BYTES:
        return JSONResponse({"ok": False, "error": "err_file_too_large"}, 413)
    body = await request.body()
    if len(body) != size:
        # streaming/unknown length fell through — accept actual bytes
        size = len(body)
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            return JSONResponse({"ok": False, "error": "err_file_too_large"}, 413)

    # Same security pipeline as web uploads
    ok, reason = sniff_allowed(filename, ctype, body)
    if not ok:
        return JSONResponse({"ok": False, "error": reason}, 415)
    ip = request.headers.get("X-Real-IP", request.client.host if request.client else "").strip()
    ok, reason = check_quota(user["id"], ip, size)
    if not ok:
        return JSONResponse({"ok": False, "error": reason}, 429)

    from security import sign_source_token
    expiry_days = 1
    gokapi = upload_to_gokapi(io.BytesIO(body), filename, ctype,
                              expiry_days, 10, size)
    info = gokapi.get("FileInfo", {})
    fid = info.get("Id", "")
    return {"ok": True, "file": {
        "publicId": fid,
        "gokapiId": fid,
        "filename": filename,
        "contentType": ctype,
        "size": size,
        "sourceToken": sign_source_token(user["id"], fid),
    }}


@router.post("/execute")
async def v1_execute(request: Request):
    """Run a tool. Identical metering gate to the web path; client_ref is
    REQUIRED here (agent retries must not double-charge)."""
    user, err = _require_bearer(request)
    if err:
        return err
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    client_ref = str(raw.get("client_ref") or "").strip()
    if not client_ref or len(client_ref) > 128:
        return JSONResponse({"ok": False, "error": "client_ref_required"}, 422)

    # Funnel through the internal endpoint's logic by internal dispatch:
    # the metering gate, replay handling, and refunds live in ONE place.
    from app.routes.api import execute_tool as _internal_execute
    request.state._v1_raw = raw
    request.state._v1_client_ref = client_ref
    return await _internal_execute(request)


@router.get("/uploads/{public_id}")
async def v1_fetch(public_id: str, request: Request):
    """Fetch result bytes (or metadata with ?meta=1). Owner-only —
    tokens are scoped to the same user as sessions."""
    user, err = _require_bearer(request)
    if err:
        return err
    from uploads import get_upload_by_public_id
    upload = get_upload_by_public_id(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if upload["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "error": "forbidden"}, 403)
    if request.query_params.get("meta") == "1":
        return {"ok": True, "file": {
            "publicId": upload["public_id"],
            "filename": upload["file_name"],
            "contentType": upload["content_type"],
            "size": upload["size_bytes"],
            "expireAt": upload["expire_at"],
        }}
    status, headers, stream = storage_read(upload["gokapi_id"])
    if status >= 400:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    data = stream.read()
    return Response(content=data, media_type=upload["content_type"] or
                    "application/octet-stream",
                    headers={"Content-Disposition":
                             f'attachment; filename="{upload["file_name"]}"'})
