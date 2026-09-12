"""execution/pipeline — shared source custody + durable result landing.

Source upload is one ingress event: validate all inputs, reserve quota once,
then upload. A failed batch rolls storage and quota reservations back. Tool
results use save_upload_record and never consume ingress quota.
"""

import io

from database import db
from gokapi import upload_to_gokapi, delete_gokapi_file
from repo.uploads import insert_record
from security import sign_source_token, sniff_allowed
from sharing.uploads import reserve_ingress, release_ingress
from governance.delivery import INTERMEDIATE_TTL_DAYS, INTERMEDIATE_DOWNLOAD_LIMIT
from utils import generate_public_id, public_file_url


class IngressBoundaryError(ValueError):
    """Platform source-ingress hard boundary denial; args[0] = denial dict."""
    def __init__(self, denial: dict):
        super().__init__(denial["error"])
        self.denial = denial


def upload_sources(user: dict, files: list[tuple[str, str, bytes]], ip: str) -> list[dict]:
    """Upload one logical source batch for v1/MCP.

    files: [(filename, content_type, bytes), ...]. All are validated before a
    single ingress reservation. Partial storage failure leaves neither source
    objects nor quota facts behind.
    """
    if not files:
        raise ValueError("err_select_file")
    checked = []
    for filename, ctype, body in files:
        ok, reason = sniff_allowed(filename, ctype, body)
        if not ok:
            raise ValueError(reason)
        if not body:
            raise ValueError("err_empty_file")
        checked.append((filename, ctype, body))

    # Platform hard boundaries (20 files / single / batch) — authoritative
    # single point, before any quota reservation or Gokapi storage.
    from governance.ingress import validate_batch
    denial = validate_batch([len(body) for _, _, body in checked])
    if denial:
        raise IngressBoundaryError(denial)

    ok, reason, usage_ids = reserve_ingress(
        user["id"], ip, [len(body) for _, _, body in checked])
    if not ok:
        raise ValueError(reason)

    uploaded_ids = []
    out = []
    try:
        for filename, ctype, body in checked:
            gokapi = upload_to_gokapi(
                io.BytesIO(body), filename, ctype,
                INTERMEDIATE_TTL_DAYS, INTERMEDIATE_DOWNLOAD_LIMIT, len(body))
            info = gokapi.get("FileInfo", {})
            fid = info.get("Id", "")
            if not fid:
                raise RuntimeError("gokapi_missing_id")
            uploaded_ids.append(fid)
            out.append({
                "publicId": fid,
                "gokapiId": fid,
                "filename": filename,
                "contentType": ctype,
                "size": len(body),
                "sourceToken": sign_source_token(user["id"], fid),
            })
        return out
    except Exception:
        for fid in uploaded_ids:
            try:
                delete_gokapi_file(fid)
            except Exception:
                pass
        release_ingress(user["id"], usage_ids)
        raise


def upload_source(user: dict, filename: str, ctype: str, body: bytes, ip: str) -> dict:
    """Single-file wrapper used by REST v1."""
    return upload_sources(user, [(filename, ctype, body)], ip)[0]


def save_upload_record(user_id, ip, gokapi_id, filename, content_type, size,
                       expiry_days, allowed_downloads, expire_at,
                       url_hotlink, base_url: str, con=None) -> dict:
    """Land one durable share/result row.

    con=None owns a transaction (normal tool result). A caller may supply a
    connection to land a multi-share batch atomically.
    """
    def _save(c):
        public_id = generate_public_id(c)
        dl_url = public_file_url(public_id)
        insert_record(c, user_id, ip, public_id, gokapi_id, filename,
                      content_type, size, expiry_days, allowed_downloads,
                      expire_at, dl_url, url_hotlink, now_ts())
        return public_id, dl_url

    if con is None:
        with db() as c:
            public_id, dl_url = _save(c)
    else:
        public_id, dl_url = _save(con)

    return {
        "type": "share_link",
        "name": filename,
        "downloadUrl": dl_url,
        "printUrl": (f"{base_url}/f/{public_id}/print"
                     if content_type == "application/pdf" else None),
        "filename": filename,
        "contentType": content_type,
        "size": size,
        "publicId": public_id,
        "expireAt": expire_at,
        "expiryDays": expiry_days,
        "allowedDownloads": allowed_downloads,
    }


from utils import now as now_ts  # noqa: E402
