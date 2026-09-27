"""execution/source — 输入解析 (the execution-domain split, 从 api.py 收编).

_resolve_upload: public_id → upload dict。
  DB 记录: 属主校验 (user_id 匹配)。
  gokapi 中间件 (tool flow, 无 DB 记录): HMAC source token 校验。
"""

from security import verify_source_token
from sharing.uploads import get_upload_by_public_id
from storage import info as storage_info


def resolve_upload_sync(public_id: str, user: dict, token: str):
    """(upload_dict | None, error | None)。error: forbidden | not_found。"""
    upload = get_upload_by_public_id(public_id)
    if upload:
        if upload["user_id"] != user["id"]:
            return None, "forbidden"
        return upload, None
    # Tool flow: original in gokapi but not in DB
    if not public_id.isalnum() or len(public_id) < 8 or len(public_id) > 20:
        return None, "not_found"
    if not verify_source_token(token, user["id"], public_id):
        return None, "forbidden"
    gokapi_info = storage_info(public_id)
    if not gokapi_info:
        return None, "not_found"
    return {
        "public_id": public_id,
        "gokapi_id": public_id,
        "file_name": gokapi_info.get("Name", "upload"),
        "content_type": gokapi_info.get("ContentType", "application/octet-stream"),
        "size_bytes": int(gokapi_info.get("SizeBytes", 0)),
        "url_hotlink": gokapi_info.get("UrlHotlink", ""),
        "url_download": gokapi_info.get("UrlDownload", ""),
    }, None


async def resolve_upload(public_id: str, user: dict, token: str = ""):
    """async 包装: gokapi fallback 是阻塞 HTTP, 走线程。"""
    return await asyncio.to_thread(resolve_upload_sync, public_id, user, token)


import asyncio  # noqa: E402  (放在使用后避免顶部循环依赖窗口)
