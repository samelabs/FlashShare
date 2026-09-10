import json
import time

from config import DAILY_UPLOADS, DAILY_BYTES
from database import db
from repo import uploads as uploads_repo
from repo import upload_usage as usage_repo
from gokapi import delete_gokapi_file, _gokapi_connection, _gokapi_path, GOKAPI_KEY
from utils import now, public_file_url


def _quota_reason(row: dict, ip_count: int, incoming_bytes: int,
                  incoming_count: int) -> str:
    if int(row["c"]) + incoming_count > DAILY_UPLOADS:
        return "err_quota_count"
    if int(row["b"]) + incoming_bytes > DAILY_BYTES:
        return "err_quota_size"
    if int(ip_count) + incoming_count > DAILY_UPLOADS * 2:
        return "err_quota_ip"
    return ""


def check_quota(user_id: int, ip: str, incoming_bytes: int,
                incoming_count: int = 1) -> tuple:
    """Read-only quota preview against ingress facts."""
    start = now() - 24 * 3600
    with db() as con:
        row = usage_repo.user_usage(con, user_id, start)
        ip_count = usage_repo.ip_usage_count(con, ip, start)
    reason = _quota_reason(row, ip_count, int(incoming_bytes), int(incoming_count))
    return (not reason, reason)


def reserve_ingress(user_id: int, ip: str, sizes: list[int]) -> tuple:
    """Atomically reserve the daily source-file ingress budget."""
    clean = [int(s) for s in sizes]
    if not clean or any(s <= 0 for s in clean):
        return False, "err_empty_file", []
    start = now() - 24 * 3600
    ts = now()
    with db() as con:
        usage_repo.lock_user(con, user_id)
        row = usage_repo.user_usage(con, user_id, start)
        ip_count = usage_repo.ip_usage_count(con, ip, start)
        reason = _quota_reason(row, ip_count, sum(clean), len(clean))
        if reason:
            return False, reason, []
        ids = usage_repo.insert_many(con, user_id, ip, clean, ts)
    return True, "", ids


def release_ingress(user_id: int, usage_ids: list[int]) -> None:
    if not usage_ids:
        return
    with db() as con:
        usage_repo.delete_ids(con, user_id, usage_ids)


def get_upload_by_public_id(public_id: str):
    """Raw owner/internal lookup; callers decide whether expiry is relevant."""
    with db() as con:
        return uploads_repo.by_public_id(con, public_id)


def get_live_upload_by_public_id(public_id: str):
    """Canonical read gate for serving a share/result to a consumer."""
    row = get_upload_by_public_id(public_id)
    if not row:
        return None
    if int(row["expire_at"] or 0) < now():
        return None
    return row


_gokapi_cache = {}
_GOKAPI_CACHE_TTL = 60
_GOKAPI_STALE_TTL = 300

def _get_all_gokapi_status():
    """Fetch all file statuses, with a short stale-on-error cache."""
    import time
    now_ts = time.time()
    age = now_ts - _gokapi_cache.get("ts", 0)
    if age < _GOKAPI_CACHE_TTL:
        return _gokapi_cache.get("data", {})
    if not GOKAPI_KEY:
        return {}
    has_stale = age < _GOKAPI_STALE_TTL and _gokapi_cache.get("data")
    try:
        conn, _ = _gokapi_connection(timeout=2)
        conn.request("GET", _gokapi_path("/api/files/list"), headers={"apikey": GOKAPI_KEY})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status != 200:
            return _gokapi_cache.get("data", {})
        files = json.loads(data.decode("utf-8"))
        result = {f["Id"]: f for f in files} if isinstance(files, list) else {}
        _gokapi_cache["data"] = result
        _gokapi_cache["ts"] = now_ts
        return result
    except Exception:
        return _gokapi_cache.get("data", {}) if has_stale else {}


def list_uploads(user_id: int, offset: int = 0, limit: int = 20) -> list:
    with db() as con:
        rows = uploads_repo.by_owner(con, user_id, int(time.time()), limit, offset)
    gokapi_map = _get_all_gokapi_status()
    uploads = []
    for row in rows:
        item = dict(row)
        live = gokapi_map.get(item["gokapi_id"])
        if live:
            item["downloads_remaining"] = live.get("DownloadsRemaining")
            item["download_count"] = live.get("DownloadCount")
            exp = live.get("ExpireAt")
            try:
                item["expire_at"] = int(exp) if exp else item["expire_at"]
            except (TypeError, ValueError):
                pass
        item["url_download"] = public_file_url(item["public_id"])
        # Storage binding is server-side implementation detail, not an account
        # API contract. Keeping it out also prevents bypassing /f lifecycle.
        item.pop("gokapi_id", None)
        item.pop("url_hotlink", None)
        uploads.append(item)
    return uploads


def delete_upload(upload_id: int, user_id: int) -> bool:
    with db() as con:
        row = uploads_repo.owned_row(con, upload_id, user_id)
    if not row:
        return False
    with db() as con:
        uploads_repo.delete_upload_row(con, upload_id, user_id)
    try:
        delete_gokapi_file(row["gokapi_id"])
    except Exception:
        import logging
        logging.getLogger("flashshare").warning(
            "gokapi delete failed for %s (will expire naturally)", row["gokapi_id"])
    return True
