"""execution/pipeline — v1/MCP 共享上传管线 + 落地写库 (批2e, 从 apiv1/api 收编).

🔴8 修复落位: multi_upload_pipeline 中途失败回滚已传 gokapi 件。
"""

import io

from database import db
from gokapi import upload_to_gokapi
from repo.uploads import insert_record
from security import sign_source_token, sniff_allowed
from sharing.uploads import check_quota
from utils import generate_public_id, public_file_url


def upload_source(user: dict, filename: str, ctype: str, body: bytes, ip: str) -> dict:
    """单个源文件: sniff → quota → gokapi。ValueError(i18n key) 拒绝。"""
    ok, reason = sniff_allowed(filename, ctype, body)
    if not ok:
        raise ValueError(reason)
    ok, reason = check_quota(user["id"], ip, len(body))
    if not ok:
        raise ValueError(reason)
    gokapi = upload_to_gokapi(io.BytesIO(body), filename, ctype, 1, 10, len(body))
    info = gokapi.get("FileInfo", {})
    fid = info.get("Id", "")
    return {
        "publicId": fid,
        "gokapiId": fid,
        "filename": filename,
        "contentType": ctype,
        "size": len(body),
        "sourceToken": sign_source_token(user["id"], fid),
    }


def save_upload_record(user_id, ip, gokapi_id, filename, content_type, size,
                       expiry_days, allowed_downloads, expire_at,
                       url_hotlink, base_url: str) -> dict:
    """落地 DB 记录 + 响应 dict (从 api._save_upload_record 收编)。"""
    with db() as con:
        public_id = generate_public_id(con)
        dl_url = public_file_url(public_id)
        insert_record(con, user_id, ip, public_id, gokapi_id, filename,
                      content_type, size, expiry_days, allowed_downloads,
                      expire_at, dl_url, url_hotlink, now_ts())
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
