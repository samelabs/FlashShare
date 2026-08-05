"""Proxy routes: file download + file detail page + QR."""

import sys
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, Response, HTMLResponse

_BASE = Path(__file__).resolve().parent.parent.parent
_CORE = _BASE / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_BASE))

from uploads import get_upload_by_public_id
from gokapi import open_gokapi_download, get_gokapi_file
from utils import (
    public_file_url, format_bytes, format_ts, format_share_date, compact_filename,
    file_size_icon_svg, expiry_icon_svg, download_count_icon_svg, file_format_icon,
    html_escape,
)
from config import SOCIAL_IMAGE_URL
from i18n import LOCALE_TAGS, server_text
from icons import icon_svg, icon_path
from auth import public_user

router = APIRouter()


@router.get("/f/{public_id}")
async def file_route(public_id: str, request: Request):
    """Route: /f/{pid} → detail page, /f/{pid}?download=1 → file stream."""
    from app.main import templates

    lang = request.state.lang
    user = request.state.user
    locale = LOCALE_TAGS.get(lang, "en")
    direction = "rtl" if lang == "ar" else "ltr"
    texts = server_text(lang)

    upload = get_upload_by_public_id(public_id)
    if not upload:
        from app.routes.pages import _ctx_base
        ctx = _ctx_base(request)
        ctx["shell_class"] = "file-shell"
        return templates.TemplateResponse("missing.html", ctx, status_code=404)

    download = request.query_params.get("download")

    if download is not None:
        # Redirect to gokapi direct download — file transfer never touches Python.
        # url_hotlink format varies by gokapi version: /h/{id} or /downloadFile?id={id}
        hotlink = upload["url_hotlink"] if "url_hotlink" in upload.keys() else ""
        if hotlink:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(hotlink)
            hot_path = parsed.path
            hot_query = parsed.query

            # /h/{id} → Nginx /dh/ → gokapi /h/
            if hot_path.startswith("/h/"):
                dl_path = "/dh/" + hot_path[3:]
                if hot_query:
                    dl_path += "?" + hot_query
            # /downloadFile?id={id} → Nginx /downloadFile → gokapi /downloadFile
            elif hot_path == "/downloadFile":
                dl_path = hot_path
                if hot_query:
                    dl_path += "?" + hot_query
            else:
                dl_path = hot_path
                if hot_query:
                    dl_path += "?" + hot_query

            safe_name = upload["file_name"].replace("\\", "_").replace('"', "_")
            from fastapi.responses import RedirectResponse
            resp = RedirectResponse(url=dl_path, status_code=302)
            resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
            return resp
        # Fallback: stream via Python if no hotlink
        from gokapi import open_gokapi_download
        status, headers, stream = open_gokapi_download(upload["gokapi_id"])
        if status >= 400:
            return HTMLResponse("File unavailable", status=status)
        content_type = upload["content_type"] or "application/octet-stream"
        safe_name = upload["file_name"].replace("\\", "_").replace('"', "_")
        def generate():
            try:
                while True:
                    chunk = stream.read(1024 * 256)
                    if not chunk:
                        break
                    yield chunk
            finally:
                stream.close()
        return StreamingResponse(generate(), media_type=content_type,
                                 headers={"Content-Disposition": f'attachment; filename="{safe_name}"'})

    # Detail page via Jinja2
    live = get_gokapi_file(upload["gokapi_id"]) if upload["gokapi_id"] else None
    expire_at = (live or {}).get("ExpireAt") or upload["expire_at"]
    total = upload["allowed_downloads"]
    remaining = (live or {}).get("DownloadsRemaining")
    count = (live or {}).get("DownloadCount")
    if isinstance(count, int):
        downloads = f"{count}/{total}"
    elif isinstance(remaining, int):
        downloads = f"{max(0, total - remaining)}/{total}"
    else:
        downloads = f"0/{total}"

    pid = urllib.parse.quote(upload["public_id"])
    url = public_file_url(upload["public_id"])
    share_url = html_escape("https://x.com/intent/tweet?" + urllib.parse.urlencode({"text": texts["share_text"], "url": url}))

    from app.routes.pages import _ctx_base
    ctx = _ctx_base(request)
    ctx.update({
        "shell_class": "file-shell",
        "share_url": share_url,
        "public_id": pid,
        "file_name": html_escape(upload["file_name"]),
        "card_date": format_ts(expire_at),
        "size_text": format_bytes(upload["size_bytes"]),
        "expire_text": format_ts(expire_at),
        "downloads": downloads,
        "is_owner": bool(user and "user_id" in upload.keys() and user["id"] == upload["user_id"]),
        "escaped_url": html_escape(url),
        "dl_url": f"/f/{pid}?download=1",
        "file_size_icon": file_size_icon_svg(),
        "expiry_icon": expiry_icon_svg(),
        "download_count_icon": download_count_icon_svg(),
        "file_format_icon": file_format_icon,
    })
    # file_format_icon takes filename arg — add as callable for template
    ctx["file_format_icon"] = lambda: file_format_icon(upload["file_name"])
    return templates.TemplateResponse("detail.html", ctx)


@router.get("/f/{public_id}/qr.svg")
async def file_qr(public_id: str):
    from utils import qr_svg
    url = public_file_url(public_id)
    svg = qr_svg(url)
    return Response(content=svg, media_type="image/svg+xml; charset=utf-8")
