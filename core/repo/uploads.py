"""repo: uploads — 上传表数据访问 (批1b).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
"""

from typing import Optional


def insert_record(con, user_id: int, ip: str, public_id: str, gokapi_id: str,
                  filename: str, content_type: str, size: int,
                  expiry_days: int, allowed_downloads: int,
                  expire_at: Optional[int], url_download: str,
                  url_hotlink: str, created_at: int) -> int:
    """RETURNING id — 旧调用方忽略返回值不受影响; tool-result settlement
    用返回 id 绑定 tool_calls.upload_id。"""
    return int(con.execute(
        """
        INSERT INTO uploads(user_id, ip, public_id, gokapi_id, file_name, content_type, size_bytes,
           expiry_days, allowed_downloads, expire_at, url_download, url_hotlink, created_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """,
        (user_id, ip, public_id, gokapi_id, filename, content_type, size,
         expiry_days, allowed_downloads, expire_at, url_download,
         url_hotlink, created_at)).fetchone()["id"])


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


def by_id(con, upload_id: int) -> Optional[dict]:
    """Operator/internal by-id read (audit snapshot + takedown
    targeting); owner scoping is decided by the caller."""
    return con.execute(
        """
        SELECT id, user_id, public_id, gokapi_id, file_name,
               content_type, size_bytes, allowed_downloads, expire_at,
               created_at
        FROM uploads WHERE id = %s
        """,
        (upload_id,)).fetchone()


def by_owner(con, user_id: int, now_ts: int, limit: int, offset: int) -> list:
    return con.execute(
        """
        SELECT id, public_id, gokapi_id, file_name, content_type, size_bytes,
               expiry_days, allowed_downloads, expire_at,
               url_download, url_hotlink, created_at
        FROM uploads
        WHERE user_id = %s AND expire_at > %s
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """,
        (user_id, now_ts, limit, offset)).fetchall()


def owned_row(con, upload_id: int, user_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT id, gokapi_id FROM uploads WHERE id = %s AND user_id = %s",
        (upload_id, user_id)).fetchone()


def owned_row_for_update(con, upload_id: int, user_id: int) -> Optional[dict]:
    """Owner-scoped FOR UPDATE read for the single delete point: reading
    and deleting in ONE transaction, concurrent deleters converge (the
    loser's predicate re-evaluates after the winner commits → None)."""
    return con.execute(
        """
        SELECT id, user_id, public_id, gokapi_id, file_name,
               content_type, size_bytes
        FROM uploads WHERE id = %s AND user_id = %s FOR UPDATE
        """,
        (upload_id, user_id)).fetchone()


def active_storage_rows(con, now_ts: int) -> list:
    """Local live mappings used by storage reconciliation."""
    return con.execute(
        "SELECT id, gokapi_id FROM uploads WHERE expire_at >= %s",
        (now_ts,)).fetchall()


def delete_upload_row(con, upload_id: int, user_id: int) -> None:
    con.execute(
        "DELETE FROM uploads WHERE id = %s AND user_id = %s", (upload_id, user_id))


def delete_rows_by_ids(con, upload_ids: list[int]) -> int:
    if not upload_ids:
        return 0
    cur = con.execute("DELETE FROM uploads WHERE id = ANY(%s)", (upload_ids,))
    return int(cur.rowcount or 0)


def rename(con, upload_id: int, new_name: str) -> None:
    con.execute(
        "UPDATE uploads SET file_name = %s WHERE id = %s", (new_name, upload_id))


def purge_expired(con, now_ts: int) -> int:
    cur = con.execute("DELETE FROM uploads WHERE expire_at < %s", (now_ts,))
    return int(cur.rowcount or 0)
