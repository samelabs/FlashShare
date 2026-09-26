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

from sharing.uploads import get_live_upload_by_public_id as _live_upload
from gokapi import open_gokapi_download
from storage import info as storage_info
from utils import file_glyph
from utils import (
    public_file_url, format_bytes, format_ts, file_format_icon,
    html_escape,
)
from i18n import LOCALE_TAGS, server_text
from icons import icon_svg

router = APIRouter()


def _share_link_headers(public_id: str, content_type: str) -> dict:
    """Agent-visible share metadata: the direct download as an RFC-8288
    enclosure (Link header; the page also carries <link rel=enclosure>
    and a plain visible <a>)."""
    from urllib.parse import quote
    return {
        "Link": f'</f/{quote(public_id)}?download=1>; rel="enclosure"'
                + (f'; type="{content_type}"' if content_type else ""),
    }


@router.api_route("/f/{public_id}", methods=["GET", "HEAD"])
async def file_route(public_id: str, request: Request):
    """Route: /f/{pid} → detail page, /f/{pid}?download=1 → file stream.
    HEAD answers with the same status and headers (including the
    enclosure Link) and an empty body — it never touches a counting
    Gokapi path."""
    from app.main import templates

    lang = request.state.lang
    user = request.state.user
    locale = LOCALE_TAGS.get(lang, "en")
    direction = "rtl" if lang == "ar" else "ltr"
    texts = server_text(lang)
    is_head = request.method == "HEAD"

    upload = _live_upload(public_id)
    if not upload:
        from app.web_ctx import ctx_base as _ctx_base
        ctx = _ctx_base(request)
        ctx["shell_class"] = "file-shell"
        return templates.TemplateResponse(request, "missing.html", ctx, status_code=404)

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
                # Unknown hotlink format = storage configuration drift; do not guess.
                dl_path = None

            if dl_path is not None:
                safe_name = upload["file_name"].replace("\\", "_").replace('"', "_")
                from urllib.parse import quote
                from fastapi.responses import RedirectResponse
                resp = RedirectResponse(url=dl_path, status_code=302)
                ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "download"
                resp.headers["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe_name)}"
                for k, v in _share_link_headers(
                        upload["public_id"],
                        upload["content_type"] or "").items():
                    resp.headers[k] = v
                return resp
        if is_head:
            # no hotlink → GET would stream via Python; HEAD never opens
            # a (counting) Gokapi download stream — signal GET-only
            return Response(status_code=405, headers={"Allow": "GET"})
        # Fallback: stream via Python if no hotlink (thread-pool read:
        # a hung Gokapi must not pin the event loop)
        status, headers, stream = await asyncio.to_thread(
            open_gokapi_download, upload["gokapi_id"])
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
                                 headers={"Content-Disposition": f'attachment; filename="{safe_name}"',
                                          "X-Content-Type-Options": "nosniff"})

    if is_head:
        return Response(status_code=200, headers=_share_link_headers(
            upload["public_id"], upload["content_type"] or ""))

    # Detail page via Jinja2
    live = storage_info(upload["gokapi_id"]) if upload["gokapi_id"] else None
    expire_at = (live or {}).get("ExpireAt") or upload["expire_at"]
    total = upload["allowed_downloads"]
    remaining = (live or {}).get("DownloadsRemaining")
    count = (live or {}).get("DownloadCount")
    if isinstance(count, int):
        downloads = f"{count}/{total}"
        _dl_left = max(0, total - count)
    elif isinstance(remaining, int):
        downloads = f"{max(0, total - remaining)}/{total}"
        _dl_left = max(0, remaining)
    else:
        downloads = f"0/{total}"
        _dl_left = total

    pid = urllib.parse.quote(upload["public_id"])
    url = public_file_url(upload["public_id"])
    share_url = html_escape("https://x.com/intent/tweet?" + urllib.parse.urlencode({"text": texts["share_text"], "url": url}))

    from app.web_ctx import ctx_base as _ctx_base
    ctx = _ctx_base(request)
    ctx.update({
        "shell_class": "file-shell",
        "share_url": share_url,
        "public_id": pid,
        "file_name": upload["file_name"],
        "content_type": upload["content_type"] or "",
        "is_image": ((upload["content_type"] or "").startswith("image/")
                      and (upload["content_type"] or "").lower() != "image/svg+xml"),
        "is_pdf": (upload["content_type"] or "").lower() == "application/pdf",
        "is_zip": ((upload["content_type"] or "") in ("application/zip", "application/x-zip-compressed"))
                   or (upload["file_name"] or "").lower().endswith(".zip"),
        "card_date": format_ts(expire_at),
        "size_text": format_bytes(upload["size_bytes"]),
        "expire_text": format_ts(expire_at),
        "downloads": downloads,
        "downloads_left": _dl_left,
        "file_glyph": file_glyph(upload["content_type"] or "",
                                 upload["file_name"] or ""),
        "is_owner": bool(user and "user_id" in upload.keys() and user["id"] == upload["user_id"]),
        "escaped_url": html_escape(url),
        "dl_url": f"/f/{pid}?download=1",
        "inline_url": f"/f/{pid}/inline",
        "file_size_icon": icon_svg("file_size"),
        "expiry_icon": icon_svg("expiry"),
        "download_count_icon": icon_svg("download_count"),
    })
    ctx["file_format_icon"] = lambda: file_format_icon(upload["file_name"])
    resp = templates.TemplateResponse(request, "detail.html", ctx)
    for k, v in _share_link_headers(upload["public_id"],
                                    upload["content_type"] or "").items():
        resp.headers[k] = v
    return resp


@router.get("/f/{public_id}/qr.svg")
async def file_qr(public_id: str):
    if not _live_upload(public_id):
        return Response(status_code=404)
    from utils import qr_svg
    url = public_file_url(public_id)
    svg = qr_svg(url)
    return Response(content=svg, media_type="image/svg+xml; charset=utf-8")


@router.get("/f/{public_id}/zip-list")
async def file_zip_list(public_id: str, request: Request):
    """List ZIP archive contents without downloading the whole file."""
    upload = _live_upload(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    ct = (upload["content_type"] or "").lower()
    name = (upload["file_name"] or "").lower()
    if ct not in ("application/zip", "application/x-zip-compressed") and not name.endswith(".zip"):
        return JSONResponse({"ok": False, "error": "not_zip"}, 400)

    total = int(upload["size_bytes"] or 0)
    if total <= 0:
        live = storage_info(upload["gokapi_id"]) if upload["gokapi_id"] else None
        total = int((live or {}).get("SizeBytes") or 0)

    import urllib.parse
    from gokapi import open_gokapi_internal_download, InternalDownloadUnavailable

    def fetch_range(start: int, length: int) -> bytes:
        # Internal non-counting read primitive (authenticated API,
        # increaseCounter=false) — preview must never consume a public
        # download allowance. InternalDownloadUnavailable propagates
        # UNTRANSLATED: it is a channel failure (503), never a ZIP
        # complexity classification (422).
        status, _headers, resp = open_gokapi_internal_download(
            upload["gokapi_id"],
            range_header=f"bytes={start}-{start + length - 1}")
        try:
            if status not in (200, 206):
                raise InternalDownloadUnavailable(f"gokapi_api_{status}")
            return resp.read()
        finally:
            resp.close()

    from zip_preview import list_zip_remote
    try:
        entries = await asyncio.to_thread(list_zip_remote, fetch_range, total)
    except InternalDownloadUnavailable:
        # authenticated non-counting channel unavailable → typed 503;
        # NEVER fall back to the counted public download path.
        return JSONResponse({"ok": False, "error": "preview_unavailable"}, 503)
    except ValueError as exc:
        reason = str(exc)
        if reason == "empty_archive":
            entries = []
        else:
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


@router.get("/f/{public_id}/inline")
async def file_inline(public_id: str, request: Request):
    """Stream trusted embeddable file types inline for result preview."""
    upload = _live_upload(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    ct = (upload["content_type"] or "").lower()
    is_pdf = ct == "application/pdf"
    is_image = ct.startswith("image/") and ct != "image/svg+xml"
    if not (is_pdf or is_image):
        return JSONResponse({"ok": False, "error": "not_embeddable"}, 400)

    from urllib.parse import quote
    from gokapi import open_gokapi_internal_download, InternalDownloadUnavailable
    # In-site preview must NOT consume allowed_downloads (decided product
    # semantics). Preview reads ride the authenticated non-counting
    # internal primitive ONLY — if that channel is unavailable the preview
    # fails typed (503 preview_unavailable); it NEVER falls back to the
    # counted public download path.
    # The internal read (connect + headers) runs in a worker thread: a
    # hung Gokapi must pin one thread-pool slot, never the event loop —
    # other requests keep being served while this preview waits.
    try:
        status, headers, stream = await asyncio.to_thread(
            open_gokapi_internal_download, upload["gokapi_id"])
    except InternalDownloadUnavailable:
        return JSONResponse({"ok": False, "error": "preview_unavailable"}, 503)
    if status >= 400:
        stream.close()
        return JSONResponse({"ok": False, "error": "preview_unavailable"}, 503)

    safe_name = upload["file_name"].replace("\\", "_").replace('"', "_")
    ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "document"
    disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe_name)}"

    def generate():
        # Body chunks read on the loop's default executor — a stalled
        # Gokapi body slows only this response's generator, not the loop.
        try:
            while True:
                chunk = stream.read(1024 * 256)
                if not chunk:
                    break
                yield chunk
        finally:
            stream.close()

    return StreamingResponse(
        generate(),
        media_type=ct,
        headers={"Content-Disposition": disposition,
                 "Cache-Control": "no-store",
                 "X-Content-Type-Options": "nosniff"},
    )


@router.get("/f/{public_id}/print", response_class=HTMLResponse)
async def file_print(public_id: str, request: Request):
    """Minimal print view for PDF results: embedded PDF + print button."""
    from app.main import templates

    upload = _live_upload(public_id)
    if not upload:
        return HTMLResponse("Not found", status_code=404)
    ct = (upload["content_type"] or "").lower()
    if ct != "application/pdf":
        return HTMLResponse("Not a PDF", status_code=400)

    lang = request.state.lang
    texts = server_text(lang)
    direction = "rtl" if lang == "ar" else "ltr"
    ctx = {
        "request": request,
        "lang": lang,
        "locale": LOCALE_TAGS.get(lang, "en"),
        "direction": direction,
        "file_name": upload["file_name"],
        "inline_url": f"/f/{urllib.parse.quote(public_id)}/inline",
        "download_url": f"/f/{urllib.parse.quote(public_id)}?download=1",
        "t": texts,
    }
    return templates.TemplateResponse(request, "print.html", ctx)
