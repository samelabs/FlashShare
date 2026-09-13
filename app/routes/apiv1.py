"""Public API v1 — /api/v1/*.

Bearer identity, canonical Catalog/ToolInvocation, shared source ingress and the
single metering gate. Result reads obey the same DB lifecycle as public /f.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from storage import read as storage_read
from governance.catalog import list_catalog
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
    from economy.billing import spendable_balance
    from governance.access_projection import project
    u = public_user(user)
    u.pop("email", None)
    # Single membership fact read: access.membership is the canonical
    # projection; "plan" is a legacy v1 payload alias of the same value
    # (never a second plan_of() read). Credits stay an independent
    # top-level balance — they never enter the access projection.
    # Projection fact-read failures propagate (no Free-masking here).
    access = project(user["id"])
    return {"ok": True, "user": u,
            "credits": spendable_balance(user["id"]),
            "plan": access["membership"],
            "access": access}


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
    from execution.pipeline import IngressQuotaError
    try:
        return {"ok": True, "file": _upload_source(user, filename, ctype, body, ip)}
    except IngressQuotaError as exc:
        # Structured rolling-quota denial relayed verbatim — no local
        # recomputation of used/remaining and no err_quota_* sniffing.
        return JSONResponse({"ok": False, **exc.denial}, 429)
    except ValueError as exc:
        code = str(exc) or "err_upload_failed"
        return JSONResponse({"ok": False, "error": code}, 415)


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


# ── Hosted first-party tools (same dispatch contract as user endpoints) ──
# South-surface: the platform dispatches {job_inputs:[{filename,
# content_type, size, url: signed fetch URL}], params}; this surface pulls
# inputs and returns {filename?, content_type?, data_b64}. First-party
# tools live under a real owner account on the user chain (execution=http
# against these URLs); handler logic stays in the pure-Pillow registry
# module. NOT part of the MCP north surface (different protocol/direction).

_INPUT_CAP = 50 * 1024 * 1024  # per-file pull cap (remote MAX_OUTPUT_BYTES spirit)


def _pull_signed_source(url: str) -> bytes | None:
    """Pull one signed fetch URL (/api/source/{gid}?u=&t=). None = reject."""
    from security import verify_fetch_token
    from urllib.parse import parse_qs
    marker = "/api/source/"
    idx = url.find(marker)
    if idx < 0:
        return None
    gid, _, query = url[idx + len(marker):].partition("?")
    q = parse_qs(query)
    u_raw = (q.get("u") or [""])[0]
    t_raw = (q.get("t") or [""])[0]
    if not u_raw.isdigit() or not t_raw:
        return None
    if not verify_fetch_token(t_raw, int(u_raw), gid):
        return None
    status, _headers, stream = storage_read(gid)
    try:
        if status >= 400:
            return None
        return stream.read(_INPUT_CAP)
    finally:
        stream.close()


@router.post("/tools/rounded-corners")
async def hosted_rounded_corners(request: Request):
    """Hosted rounded-corners tool (user-chain form). multi=false: first
    input only, mirroring the tool declaration."""
    user = getattr(request.state, "user", None)
    via = getattr(request.state, "auth_via", "session")
    if not user or via != "token":
        return JSONResponse({"ok": False, "error": "auth_required"}, 401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    inputs = payload.get("job_inputs") or []
    if not inputs:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    item = inputs[0]
    data = _pull_signed_source(str(item.get("url") or ""))
    if data is None:
        return JSONResponse({"ok": False, "error": "bad_source_url"}, 422)
    import io as _io
    from tools_corners import rounded_corners_handler
    try:
        result = rounded_corners_handler(
            {"stream": _io.BytesIO(data),
             "filename": str(item.get("filename") or "image"),
             "content_type": str(item.get("content_type") or "image/png")},
            payload.get("params") or {})
    except Exception:
        return JSONResponse({"ok": False, "error": "tool_failed"}, 422)
    import base64 as _b64
    raw = result["stream"].read()
    return {
        "filename": result["filename"],
        "content_type": result["content_type"],
        "data_b64": _b64.b64encode(raw).decode(),
    }
