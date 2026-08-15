"""Proxy routes: file download + file detail page + QR + zip preview."""

import asyncio

import sys
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, Response, HTMLResponse, JSONResponse

_BASE = Path(__file__).resolve().parent.parent.parent
_CORE = _BASE / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_BASE))

from uploads import get_upload_by_public_id
from gokapi import open_gokapi_download, get_gokapi_file
from utils import (
    public_file_url, format_bytes, format_ts,
    file_size_icon_svg, expiry_icon_svg, download_count_icon_svg, file_format_icon,
    html_escape,
)
from i18n import LOCALE_TAGS, server_text
from icons import icon_svg

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
            from urllib.parse import urlparse
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
            from urllib.parse import quote
            from fastapi.responses import RedirectResponse
            resp = RedirectResponse(url=dl_path, status_code=302)
            ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "download"
            resp.headers["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe_name)}"
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
        "file_name": upload["file_name"],
        "content_type": upload["content_type"] or "",
        "is_image": (upload["content_type"] or "").startswith("image/"),
        "is_zip": ((upload["content_type"] or "") in ("application/zip", "application/x-zip-compressed"))
                   or (upload["file_name"] or "").lower().endswith(".zip"),
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


@router.get("/f/{public_id}/zip-list")
async def file_zip_list(public_id: str, request: Request):
    """List ZIP archive contents without downloading the whole file.

    Result-end preview capability (peer of image inline render):
    pulls only the central directory from gokapi via HTTP Range and
    returns a JSON entry list. Mirrors /f/{pid} visibility rules.
    """
    upload = get_upload_by_public_id(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    ct = (upload["content_type"] or "").lower()
    name = (upload["file_name"] or "").lower()
    if ct not in ("application/zip", "application/x-zip-compressed") and not name.endswith(".zip"):
        return JSONResponse({"ok": False, "error": "not_zip"}, 400)

    total = int(upload["size_bytes"] or 0)
    if total <= 0:
        live = get_gokapi_file(upload["gokapi_id"]) if upload["gokapi_id"] else None
        total = int((live or {}).get("SizeBytes") or 0)

    import http.client
    import urllib.parse
    from gokapi import _gokapi_connection, _gokapi_path

    def fetch_range(start: int, length: int) -> bytes:
        conn, _ = _gokapi_connection(timeout=30)
        try:
            conn.request("GET", _gokapi_path(f"/downloadFile?id={urllib.parse.quote(upload['gokapi_id'])}"),
                         headers={"Range": f"bytes={start}-{start + length - 1}"})
            resp = conn.getresponse()
            if resp.status not in (200, 206):
                raise ValueError("range_unavailable")
            return resp.read()
        finally:
            conn.close()

    from zip_preview import list_zip_remote
    try:
        entries = await asyncio.to_thread(list_zip_remote, fetch_range, total)
    except ValueError as exc:
        reason = str(exc)
        if reason in ("zip64_unsupported",):
            return JSONResponse({"ok": False, "error": "zip_unsupported"}, 422)
        if reason in ("cd_too_large", "range_unavailable", "range_short_read"):
            return JSONResponse({"ok": False, "error": "zip_too_complex"}, 422)
        return JSONResponse({"ok": False, "error": "zip_invalid"}, 422)

    files = [e for e in entries if not e["is_dir"]]
    dirs = [e for e in entries if e["is_dir"]]
    return JSONResponse({
        "ok": True,
        "total": len(entries),
        "truncated": len(entries) >= 500,
        "entries": entries,
        "files": len(files),
        "dirs": len(dirs),
    })
