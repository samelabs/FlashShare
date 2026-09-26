"""execution/pipeline — shared source custody + durable result landing.

Durable result landing. Source ingress lives on the upload surfaces
(api.py staging, mcp.py inline staging, the MCP ticket PUT); the retired shared
batch-upload pipeline went away with the v1 surface.
"""

import io

from database import db
from storage import write as storage_write
from repo.uploads import insert_record
from utils import generate_public_id, public_file_url


def save_upload_record(user_id, ip, gokapi_id, filename, content_type, size,
                       expiry_days, allowed_downloads, expire_at,
                       url_hotlink, base_url: str, con=None) -> dict:
    """Land one durable DIRECT-SHARE / non-tool result row.

    Direct-share landing only. Tool results stage via execution.results
    (items model); the gate settles them in one transaction.
    con=None owns a transaction; a caller may supply a connection to
    land a multi-share batch atomically.
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
        "page_url": dl_url,
        "download_url": f"{dl_url}?download=1",
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
