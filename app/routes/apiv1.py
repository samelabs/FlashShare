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
from sharing.uploads import check_quota
from gokapi import upload_to_gokapi
from storage import read as storage_read
from governance.catalog import list_catalog
from gokapi import open_gokapi_download
from utils import now, sanitize_filename
from identity.auth import public_user

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
    from economy.billing import spendable_balance as get_balance
    from economy.subscription import plan_of
    u = public_user(user)
    u.pop("email", None)  # token channel: no PII beyond what's needed
    return {"ok": True, "user": u, "credits": get_balance(user["id"]),
            "plan": plan_of(user["id"])}


@router.get("/tools")
async def v1_tools(request: Request):
    user, err = _require_bearer(request)
    if err:
        return err
    # list_catalog is the single catalog source (data-state registry +
    # pricing overlay inside): cost included, disabled tools hidden —
    # identical to web home and MCP. Viewer-scoped (own private tools).
    return {"ok": True, "tools": list_catalog(user["id"])}


def _upload_source(user: dict, filename: str, ctype: str, body: bytes, ip: str) -> dict:
    """Shared source-upload pipeline (v1 + MCP) — 薄壳, 实现在 execution.pipeline."""
    from execution.pipeline import upload_source
    return upload_source(user, filename, ctype, body, ip)


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

    ip = request.headers.get("X-Real-IP", request.client.host if request.client else "").strip()
    try:
        return {"ok": True, "file": _upload_source(user, filename, ctype, body, ip)}
    except ValueError as exc:
        code = str(exc) or "err_upload_failed"
        status = 429 if code.startswith("err_quota") else 415
        return JSONResponse({"ok": False, "error": code}, status)


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

    # 统一执行入口 (批3a): 直接走 execution 域 dispatch, 不再借道路由层。
    from execution.dispatch import dispatch_execute
    raw.setdefault("client_ref", client_ref)
    payload, error_body, status = await dispatch_execute(request, user, raw)
    if error_body is not None:
        return JSONResponse(error_body, status)
    return payload


@router.get("/uploads/{public_id}")
async def v1_fetch(public_id: str, request: Request):
    """Fetch result bytes (or metadata with ?meta=1). Owner-only —
    tokens are scoped to the same user as sessions."""
    user, err = _require_bearer(request)
    if err:
        return err
    from sharing.uploads import get_upload_by_public_id
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

# ── Demo echo endpoint (open-platform market seed; contract-compliant) ──
# Implements the user-tool HTTP contract: receives job_inputs[].url (signed
# fetch URLs), returns {filename, content_type, data_b64}. Exists so the
# market always has working examples and so the remote-call pipeline can be
# exercised end-to-end without third-party infrastructure.

@router.post("/demo-echo")
async def demo_echo(request: Request):
    import base64 as _b64
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    job_inputs = payload.get("job_inputs") or []
    outputs = []
    from security import verify_fetch_token
    from urllib.parse import urlparse, parse_qs
    for item in job_inputs[:5]:
        url = str(item.get("url") or "")
        # fetch_url = /api/source/{gokapi_id}?u=..&t=.. — read the file
        # directly from local Gokapi storage (no HTTP loop-back: the
        # single uvicorn worker would deadlock on itself).
        marker = "/api/source/"
        idx = url.find(marker)
        if idx < 0:
            continue
        path_and_query = url[idx + len(marker):]
        gid, _, query = path_and_query.partition("?")
        # 🔴6 修复 (批2e): 与 /api/source 同闸 — 单次签名 fetch token 必须有效。
        # 原实现裸读 gokapi, 匿名者知道 gid 即可绕过签名闸取件。
        q = parse_qs(query)
        u_raw = (q.get("u") or [""])[0]
        t_raw = (q.get("t") or [""])[0]
        if not u_raw.isdigit() or not t_raw:
            continue
        if not verify_fetch_token(t_raw, int(u_raw), gid):
            continue
        try:
            status, headers, resp = open_gokapi_download(gid)
            data = resp.read(50 * 1024 * 1024)
            resp.close()
        except Exception:
            continue
        if status != 200:
            continue
        outputs.append({
            "filename": "echoed-" + str(item.get("filename") or "file"),
            "content_type": str(item.get("content_type") or "application/octet-stream"),
            "data_b64": _b64.b64encode(data).decode(),
        })
    body = outputs[0] if len(outputs) == 1 else {"files": outputs}
    body["echo"] = True
    return body

