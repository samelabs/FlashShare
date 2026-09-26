"""Hosted tool endpoints — southbound execution surface (/tool-endpoints/*).

NOT a public machine API: these URLs are the execution=http targets of
first-party tools living on the user chain. The platform dispatches
{files:[{filename, content_type, size, url: signed fetch URL}],
params}; this surface pulls inputs via verify_fetch_token and returns
the IO-1 items contract: {items:[{kind:text|json|file,...}]}. Handler
logic stays in the pure registry modules (e.g. tools_corners).
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

_INPUT_CAP = 50 * 1024 * 1024  # per-file pull cap (remote MAX_OUTPUT_BYTES spirit)


async def _pull_signed_source(url: str) -> bytes | None:
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
        status, _headers, stream = await asyncio.to_thread(
            storage_read, gid)
    except InternalDownloadUnavailable:
        return None
    try:
        if status >= 400:
            return None
        return await asyncio.to_thread(stream.read, _INPUT_CAP)
    finally:
        stream.close()


@router.post("/rounded-corners")
async def hosted_rounded_corners(request: Request):
    """Hosted rounded-corners tool (user-chain form). Files window 1..1:
    first input only, mirroring the tool declaration.

    Authority: the short-lived HMAC hosted-execution capability minted by
    canonical trusted dispatch for this exact path + outbound payload.
    Ordinary API Bearer tokens are NOT authorization here, and the source
    capability inside the payload is never touched before hosted auth
    passes."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from security import verify_hosted_exec_token
    cap = request.headers.get("x-hosted-execution", "")
    if not cap or not verify_hosted_exec_token(cap, "/tool-endpoints/rounded-corners", payload):
        return JSONResponse({"ok": False, "error": "auth_required"}, 401)
    inputs = payload.get("files") or []
    if not inputs:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    item = inputs[0]
    data = await _pull_signed_source(str(item.get("url") or ""))
    if data is None:
        return JSONResponse({"ok": False, "error": "bad_source_url"}, 422)
    import io as _io
    from tools_corners import rounded_corners_handler
    try:
        result = await asyncio.to_thread(
            rounded_corners_handler,
            {"stream": _io.BytesIO(data),
             "filename": str(item.get("filename") or "image"),
             "content_type": str(item.get("content_type") or "image/png")},
            payload.get("params") or {})
    except Exception:
        return JSONResponse({"ok": False, "error": "tool_failed"}, 422)
    import base64 as _b64
    items = result["items"]
    out_items = []
    for it in items:
        if it["kind"] == "file":
            raw = it["stream"].read()
            out_items.append({"kind": "file",
                              "filename": it["filename"],
                              "content_type": it["content_type"],
                              "data_b64": _b64.b64encode(raw).decode()})
        else:
            out_items.append(it)
    return {"items": out_items}
