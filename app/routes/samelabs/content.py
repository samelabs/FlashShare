"""/samelabs/content — content governance.

Find shared files (uploads) and tool results (/r) by share id / result
id / user / tool; takedown of a file reuses the sharing single point
(sharing.uploads.delete_upload) through core.operator.content with an
operator_audit fact. Live remaining-download counts come from the same
Gokapi status single point the account list reads."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import db
from repo import operator_views
from utils import format_bytes, remaining_downloads, page_number

from app.routes.samelabs.common import gate, ctx, fmt_num

router = APIRouter()

_PAGE = 30


def _content_impl(user_op: dict, request: Request, share: str, result: str,
                  user: str, tool: str, page: int):
    from app.main import templates
    page = page_number(page)
    share = share.strip()[:64]
    result = result.strip()[:64]
    user = user.strip()[:64]
    tool = tool.strip()[:100]
    with db() as con:
        uploads, up_total = operator_views.content_uploads(
            con, share or None, user or None, tool or None,
            _PAGE, (page - 1) * _PAGE)
        results, res_total = operator_views.content_results(
            con, result or None, user or None, tool or None,
            _PAGE, (page - 1) * _PAGE)
    import asyncio
    from sharing.uploads import live_status_map
    live = live_status_map()
    files = []
    for r in uploads:
        d = dict(r)
        gokapi_id = d.pop("gokapi_id", None)
        d["size_h"] = format_bytes(int(d["size_bytes"]))
        d["remaining"] = remaining_downloads(
            int(d["allowed_downloads"]),
            live.get(gokapi_id) if gokapi_id else None)
        files.append(d)
    return templates.TemplateResponse(
        request, "samelabs/content.html",
        ctx(request, user_op, files=files, results=[dict(r) for r in results],
            up_total=up_total, res_total=res_total,
            q_share=share, q_result=result, q_user=user, q_tool=tool,
            page=page,
            pages=max(1, -(-max(up_total, res_total) // _PAGE)),
            fmt_num=fmt_num, page_id="content"))

@router.get("/samelabs/content")
async def content(request: Request, share: str = "", result: str = "",
                  user: str = "", tool: str = "", page: int = 1):
    user_op, resp = gate(request)
    if resp:
        return resp
    import asyncio
    return await asyncio.to_thread(_content_impl, user_op, request, share, result, user, tool, page)


@router.post("/samelabs/content/uploads/{upload_id}/delete")
async def delete_upload(upload_id: int, request: Request):
    """File takedown = delete the mapping row + the physical Gokapi
    object (sharing single point) + one operator_audit fact. Two-step
    confirmation happens in the page JS before this endpoint is hit."""
    import asyncio
    user, resp = gate(request)
    if resp:
        return resp
    from backoffice import content as content_cmd
    try:
        out = await asyncio.to_thread(
            content_cmd.delete_upload, int(user["id"]), upload_id)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True, "id": out["id"]}
