"""repo: uploads — 上传表数据访问 (批1b).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
"""

from typing import Optional


def insert_record(con, user_id: int, ip: str, public_id: str, gokapi_id: str,
                  filename: str, content_type: str, size: int,
                  expiry_days: int, allowed_downloads: int,
                  expire_at: Optional[int], url_download: str,
                  url_hotlink: str, created_at: int) -> None:
    con.execute(
        """
        INSERT INTO uploads(user_id, ip, public_id, gokapi_id, file_name, content_type, size_bytes,
           expiry_days, allowed_downloads, expire_at, url_download, url_hotlink, created_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (user_id, ip, public_id, gokapi_id, filename, content_type, size,
         expiry_days, allowed_downloads, expire_at, url_download,
         url_hotlink, created_at))


def public_id_exists(con, public_id: str) -> bool:
    return con.execute(
        "SELECT 1 FROM uploads WHERE public_id = %s", (public_id,)).fetchone() is not None


def by_public_id(con, public_id: str) -> Optional[dict]:
    return con.execute(
        """
        SELECT id, public_id, gokapi_id, file_name, content_type, size_bytes, expiry_days,
               allowed_downloads, expire_at, url_download, url_hotlink, created_at, user_id
        FROM uploads
        WHERE public_id = %s
        """,
        (public_id,)).fetchone()


def by_owner(con, user_id: int, now_ts: int, limit: int, offset: int) -> list:
    return con.execute(
        """
        SELECT id, public_id, gokapi_id, file_name, size_bytes, expiry_days, allowed_downloads, expire_at,
               url_download, url_hotlink, created_at
        FROM uploads
        WHERE user_id = %s AND expire_at > %s
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """,
        (user_id, now_ts, limit, offset)).fetchall()


def quota_usage(con, user_id: int, since_ts: int) -> dict:
    return con.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(size_bytes), 0) b FROM uploads WHERE user_id = %s AND created_at > %s",
        (user_id, since_ts)).fetchone()


def ip_usage_count(con, ip: str, since_ts: int) -> int:
    return con.execute(
        "SELECT COUNT(*) c FROM uploads WHERE ip = %s AND created_at > %s",
        (ip, since_ts)).fetchone()["c"]


def owned_row(con, upload_id: int, user_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT id, gokapi_id FROM uploads WHERE id = %s AND user_id = %s",
        (upload_id, user_id)).fetchone()


def delete_upload_row(con, upload_id: int, user_id: int) -> None:
    con.execute(
        "DELETE FROM uploads WHERE id = %s AND user_id = %s", (upload_id, user_id))


def rename(con, upload_id: int, new_name: str) -> None:
    con.execute(
        "UPDATE uploads SET file_name = %s WHERE id = %s", (new_name, upload_id))


def purge_expired(con, now_ts: int) -> None:
    con.execute("DELETE FROM uploads WHERE expire_at < %s", (now_ts,))
