import json
import time

from config import DAILY_UPLOADS, DAILY_BYTES
from database import db
from gokapi import get_gokapi_file, delete_gokapi_file, _gokapi_connection, _gokapi_path, GOKAPI_KEY
from utils import now, public_file_url


def check_quota(user_id: int, ip: str, incoming_bytes: int) -> tuple:
    start = now() - 24 * 3600
    with db() as con:
        row = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(size_bytes), 0) b FROM uploads WHERE user_id = ? AND created_at > ?",
            (user_id, start),
        ).fetchone()
        ip_row = con.execute(
            "SELECT COUNT(*) c FROM uploads WHERE ip = ? AND created_at > ?",
            (ip, start),
        ).fetchone()
    if row["c"] >= DAILY_UPLOADS:
        return False, "err_quota_count"
    if row["b"] + incoming_bytes > DAILY_BYTES:
        return False, "err_quota_size"
    if ip_row["c"] >= DAILY_UPLOADS * 2:
        return False, "err_quota_ip"
    return True, ""


def get_upload_by_public_id(public_id: str):
    with db() as con:
        return con.execute(
            """
            SELECT id, public_id, gokapi_id, file_name, content_type, size_bytes, expiry_days,
                   allowed_downloads, expire_at, url_download, url_hotlink, created_at, user_id
            FROM uploads
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()


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


def list_uploads(user_id: int) -> list:
    with db() as con:
        rows = con.execute(
            """
            SELECT id, public_id, gokapi_id, file_name, size_bytes, expiry_days, allowed_downloads, expire_at,
                   url_download, url_hotlink, created_at
            FROM uploads
            WHERE user_id = ? AND expire_at > ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (user_id, int(time.time())),
        ).fetchall()
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
        row = con.execute(
            "SELECT id, gokapi_id FROM uploads WHERE id = ? AND user_id = ?",
            (upload_id, user_id),
        ).fetchone()
    if not row:
        return False
    delete_gokapi_file(row["gokapi_id"])
    with db() as con:
        con.execute("DELETE FROM uploads WHERE id = ? AND user_id = ?", (upload_id, user_id))
    return True
