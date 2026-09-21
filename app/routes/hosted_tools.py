"""Hosted tool endpoints — southbound execution surface (/tool-endpoints/*).

NOT a public machine API: these URLs are the execution=http targets of
first-party tools living on the user chain. The platform dispatches
{job_inputs:[{filename, content_type, size, url: signed fetch URL}],
params}; this surface pulls inputs via verify_fetch_token and returns
{filename?, content_type?, data_b64}. Handler logic stays in the pure
registry modules (e.g. tools_corners).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

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
    from gokapi import InternalDownloadUnavailable
    from storage import read as storage_read
    try:
        status, _headers, stream = storage_read(gid)
    except InternalDownloadUnavailable:
        return None
    try:
        if status >= 400:
            return None
        return stream.read(_INPUT_CAP)
    finally:
        stream.close()


@router.post("/rounded-corners")
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
