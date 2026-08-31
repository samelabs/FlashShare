import json
import time

from config import DAILY_UPLOADS, DAILY_BYTES
from database import db
from repo import uploads as uploads_repo
from gokapi import delete_gokapi_file, _gokapi_connection, _gokapi_path, GOKAPI_KEY
from utils import now, public_file_url


def check_quota(user_id: int, ip: str, incoming_bytes: int) -> tuple:
    start = now() - 24 * 3600
    with db() as con:
        row = uploads_repo.quota_usage(con, user_id, start)
        ip_row = {"c": uploads_repo.ip_usage_count(con, ip, start)}
    if row["c"] >= DAILY_UPLOADS:
        return False, "err_quota_count"
    if row["b"] + incoming_bytes > DAILY_BYTES:
        return False, "err_quota_size"
    if ip_row["c"] >= DAILY_UPLOADS * 2:
        return False, "err_quota_ip"
    return True, ""


def get_upload_by_public_id(public_id: str):
    with db() as con:
        return uploads_repo.by_public_id(con, public_id)


_gokapi_cache = {}  # {"data": {...}, "ts": epoch}
_GOKAPI_CACHE_TTL = 60  # seconds
_GOKAPI_STALE_TTL = 300  # serve stale data up to 5min while refreshing

def _get_all_gokapi_status():
    """Fetch all file statuses from gokapi, cached for 60 seconds.
    Serves stale data (up to 5min) to avoid blocking on gokapi HTTP.
    Returns empty dict on failure."""
    import time
    now_ts = time.time()
    age = now_ts - _gokapi_cache.get("ts", 0)
    if age < _GOKAPI_CACHE_TTL:
        return _gokapi_cache.get("data", {})
    if not GOKAPI_KEY:
        return {}
    # Cache expired — try refresh, but serve stale if available
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
    # Single batch request — no N+1
    gokapi_map = _get_all_gokapi_status()
    uploads = []
    for row in rows:
        item = dict(row)
        live = gokapi_map.get(item["gokapi_id"])
        if live:
            item["downloads_remaining"] = live.get("DownloadsRemaining")
            item["download_count"] = live.get("DownloadCount")
            item["expire_at"] = live.get("ExpireAt") or item["expire_at"]
            item["url_hotlink"] = live.get("UrlHotlink") or item["url_hotlink"]
        item["url_download"] = public_file_url(item["public_id"])
        uploads.append(item)
    return uploads


def delete_upload(upload_id: int, user_id: int) -> bool:
    with db() as con:
        row = uploads_repo.owned_row(con, upload_id, user_id)
    if not row:
        return False
    # DB first, then storage: a storage failure after the DB delete leaves an
    # orphaned gokapi file that expires on its own (invisible, no record).
    # The reverse order could leave a live DB record pointing at a dead file.
    with db() as con:
        uploads_repo.delete_row(con, upload_id, user_id)
    try:
        delete_gokapi_file(row["gokapi_id"])
    except Exception:
        # Non-fatal: gokapi expiry is the backstop.
        import logging
        logging.getLogger("flashshare").warning(
            "gokapi delete failed for %s (will expire naturally)", row["gokapi_id"])
    return True
