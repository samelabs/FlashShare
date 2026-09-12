"""Public API v1 — /api/v1/*.

Bearer identity, canonical Catalog/ToolInvocation, shared source ingress and the
single metering gate. Result reads obey the same DB lifecycle as public /f.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from storage import read as storage_read
from governance.catalog import list_catalog
from gokapi import open_gokapi_download
from utils import sanitize_filename
from identity.auth import public_user

router = APIRouter()


def _require_bearer(request: Request):
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
    u.pop("email", None)
    return {"ok": True, "user": u, "credits": get_balance(user["id"]),
            "plan": plan_of(user["id"])}


@router.get("/tools")
async def v1_tools(request: Request):
    user, err = _require_bearer(request)
    if err:
        return err
    return {"ok": True, "tools": list_catalog(user["id"])}


def _upload_source(user: dict, filename: str, ctype: str, body: bytes, ip: str) -> dict:
    from execution.pipeline import upload_source
    return upload_source(user, filename, ctype, body, ip)


@router.post("/uploads")
async def v1_upload(request: Request):
    """Upload one tool source file and return its intermediate handle."""
    user, err = _require_bearer(request)
    if err:
        return err
    try:
        ctype = request.headers.get("content-type", "")
        filename = sanitize_filename(request.headers.get("x-filename", "file"))
        size = int(request.headers.get("content-length", "0") or 0)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    # Authoritative boundary via the shared ingress validator (single
    # source for Web/REST/MCP); structured error contract.
    from governance.ingress import validate_batch
    denial = validate_batch([max(size, 0)])
    if denial:
        return JSONResponse({"ok": False, **denial}, 413)
    body = await request.body()
    if len(body) != size:
        size = len(body)
        denial = validate_batch([max(size, 0)])
        if denial:
            return JSONResponse({"ok": False, **denial}, 413)

    ip = request.headers.get("X-Real-IP", request.client.host if request.client else "").strip()
    try:
        return {"ok": True, "file": _upload_source(user, filename, ctype, body, ip)}
    except ValueError as exc:
        code = str(exc) or "err_upload_failed"
        status = 429 if code.startswith("err_quota") else 415
        return JSONResponse({"ok": False, "error": code}, status)


@router.post("/execute")
async def v1_execute(request: Request):
    """Run canonical ToolInvocation; v1 additionally requires client_ref."""
    user, err = _require_bearer(request)
    if err:
        return err
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    client_ref = raw.get("client_ref") if isinstance(raw, dict) else None
    client_ref = str(client_ref).strip() if isinstance(client_ref, str) else ""
    if not client_ref or len(client_ref) > 128:
        return JSONResponse({"ok": False, "error": "client_ref_required"}, 422)

    from execution.dispatch import dispatch_execute
    payload, error_body, status = await dispatch_execute(request, user, raw)
    if error_body is not None:
        return JSONResponse(error_body, status)
    return payload


@router.get("/uploads/{public_id}")
async def v1_fetch(public_id: str, request: Request):
    """Fetch a live result (or metadata with ?meta=1), owner-only."""
    user, err = _require_bearer(request)
    if err:
        return err
    from sharing.uploads import get_live_upload_by_public_id
    upload = get_live_upload_by_public_id(public_id)
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
    return Response(
        content=data,
        media_type=upload["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{upload["file_name"]}"',
                 "X-Content-Type-Options": "nosniff"},
    )


# ── Demo echo endpoint (open-platform market seed; contract-compliant) ──

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
    from urllib.parse import parse_qs
    for item in job_inputs[:5]:
        url = str(item.get("url") or "")
        marker = "/api/source/"
        idx = url.find(marker)
        if idx < 0:
            continue
        path_and_query = url[idx + len(marker):]
        gid, _, query = path_and_query.partition("?")
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
