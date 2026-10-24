"""Proxy routes: file download + file detail page + QR + zip preview."""

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
from storage import info as storage_info
from utils import file_glyph
from utils import (
    public_file_url, format_bytes, format_ts, file_format_icon, mime_base,
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
def file_route(public_id: str, request: Request):
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
        # Redirect to the gokapi hotlink — the ONE download path; file
        # transfer never touches Python. url_hotlink format varies by
        # gokapi version: /h/{id} or /downloadFile?id={id}
        hotlink = upload["url_hotlink"]
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
                from fastapi.responses import RedirectResponse
                from utils import content_disposition
                resp = RedirectResponse(url=dl_path, status_code=302)
                resp.headers["Content-Disposition"] = content_disposition(
                    "attachment", upload["file_name"], "download")
                for k, v in _share_link_headers(
                        upload["public_id"],
                        upload["content_type"] or "").items():
                    resp.headers[k] = v
                return resp
        # No hotlink: the one download path is unavailable — typed 503,
        # never a Python streaming substitute
        return JSONResponse({"ok": False, "error": "download_unavailable"},
                            status_code=503)

    if is_head:
        return Response(status_code=200, headers=_share_link_headers(
            upload["public_id"], upload["content_type"] or ""))

    # Detail page via Jinja2
    live = storage_info(upload["gokapi_id"]) if upload["gokapi_id"] else None
    expire_at = (live or {}).get("ExpireAt") or upload["expire_at"]
    total = upload["allowed_downloads"]
    # single calculator: remaining first, used = total - remaining
    from utils import remaining_downloads
    _dl_left = remaining_downloads(total, live)
    if _dl_left is None:
        _dl_left = total
    downloads = f"{max(0, total - _dl_left)}/{total}"

    pid = urllib.parse.quote(upload["public_id"])
    url = public_file_url(upload["public_id"])

    # Agents asking for JSON get the page facts, not the human page.
    from app.web_ctx import wants_json
    if wants_json(request):
        return JSONResponse({
            "public_id": upload["public_id"],
            "filename": upload["file_name"],
            "size": int(upload["size_bytes"] or 0),
            "content_type": upload["content_type"] or "",
            "expires_at": int(expire_at) if expire_at else None,
            "downloads_remaining": _dl_left,
            "page_url": url,
            "download_url": url + "?download=1",
        }, headers={"Vary": "Accept",
                    **_share_link_headers(upload["public_id"],
                                          upload["content_type"] or "")})

    from app.web_ctx import ctx_base as _ctx_base
    ctx = _ctx_base(request)
    _ct_base = mime_base(upload["content_type"] or "")
    ctx.update({
        "shell_class": "file-shell",
        # share page stays follow-able but never indexed; the share card
        # wording differs from the page title, so it goes through the
        # og_* overrides (base.html single point renders the tags)
        "seo_robots": "noindex, follow",
        "og_title": upload["file_name"],
        "share_page_url": url,
        "public_id": pid,
        "file_name": upload["file_name"],
        "content_type": upload["content_type"] or "",
        "is_image": (_ct_base.startswith("image/")
                      and _ct_base != "image/svg+xml"),
        "is_pdf": _ct_base == "application/pdf",
        "is_zip": (_ct_base in ("application/zip", "application/x-zip-compressed")
                   or (upload["file_name"] or "").lower().endswith(".zip")),
        "card_date": format_ts(expire_at),
        "size_text": format_bytes(upload["size_bytes"]),
        "expire_text": format_ts(expire_at),
        "downloads": downloads,
        "downloads_left": _dl_left,
        "file_glyph": file_glyph(upload["content_type"] or "",
                                 upload["file_name"] or ""),
        # human type from the format-family vocabulary (no raw MIME)
        "type_text": (lambda _fams, _fn:
                      texts.get("fmt_family_" + ("pdf" if ((upload["content_type"] or "").lower() == "application/pdf") else (sorted(_fams)[0] if _fams else "document")))
                      or texts.get("out_kind_text", ""))(
            __import__("governance.formats", fromlist=["sniff_ext_to_families"]).sniff_ext_to_families(
                "." + (upload["file_name"] or "").rsplit(".", 1)[-1].lower()
                if "." in (upload["file_name"] or "") else ""), upload["file_name"]),
        "is_owner": bool(user and "user_id" in upload.keys() and user["id"] == upload["user_id"]),
        "dl_url": f"/f/{pid}?download=1",
        "inline_url": f"/f/{pid}/inline",
        "file_size_icon": icon_svg("file_size"),
        "expiry_icon": icon_svg("expiry"),
        "download_count_icon": icon_svg("download_count"),
    })
    # Share card: the file's own facts; an image previews itself (inline
    # preview is the non-counting channel), other types keep the site card.
    ctx["og_desc"] = (f"{ctx['type_text']} · {ctx['size_text']} · "
                      f"{texts['card_expires']}: {ctx['expire_text']} · "
                      f"{texts['r_downloads_left']}: {_dl_left}")
    if ctx["is_image"]:
        from config import BASE_URL
        ctx["og_image"] = f"{BASE_URL}/f/{pid}/inline"
    else:
        # small card: the file facts lead, not the site banner
        ctx["og_card"] = "summary"
    ctx["file_format_icon"] = lambda: file_format_icon(upload["file_name"])
    resp = templates.TemplateResponse(request, "detail.html", ctx)
    for k, v in _share_link_headers(upload["public_id"],
                                    upload["content_type"] or "").items():
        resp.headers[k] = v
    resp.headers["Vary"] = "Accept"
    return resp


@router.get("/f/{public_id}/qr.svg")
def file_qr(public_id: str):
    if not _live_upload(public_id):
        return Response(status_code=404)
    from utils import qr_svg
    url = public_file_url(public_id)
    svg = qr_svg(url)
    return Response(content=svg, media_type="image/svg+xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/f/{public_id}/zip-list")
def file_zip_list(public_id: str, request: Request):
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
        entries = list_zip_remote(fetch_range, total)
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
def file_inline(public_id: str, request: Request):
    """Stream trusted embeddable file types inline for result preview."""
    upload = _live_upload(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    ct = mime_base(upload["content_type"])
    is_pdf = ct == "application/pdf"
    is_image = ct.startswith("image/") and ct != "image/svg+xml"
    if not (is_pdf or is_image):
        return JSONResponse({"ok": False, "error": "not_embeddable"}, 400)

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
        status, headers, stream = open_gokapi_internal_download(
            upload["gokapi_id"])
    except InternalDownloadUnavailable:
        return JSONResponse({"ok": False, "error": "preview_unavailable"}, 503)
    if status >= 400:
        stream.close()
        return JSONResponse({"ok": False, "error": "preview_unavailable"}, 503)

    from utils import content_disposition
    disposition = content_disposition(
        "inline", upload["file_name"], "document")

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
def file_print(public_id: str, request: Request):
    """Minimal print view for PDF results: embedded PDF + print button."""
    from app.main import templates
    from app.web_ctx import PAGES_VERSION

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
        "pages_version": PAGES_VERSION,
    }
    return templates.TemplateResponse(request, "print.html", ctx)
