"""repo: upload_usage — append-only ingress quota facts.

One row represents one source file accepted from a user. Durable share/result
rows live in uploads; generated tool outputs intentionally never land here.
"""


def lock_user(con, user_id: int) -> None:
    con.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))


def user_usage(con, user_id: int, since_ts: int) -> dict:
    return con.execute(
        """
        SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS b
        FROM upload_usage WHERE user_id=%s AND created_at>%s
        """,
        (user_id, since_ts)).fetchone()


def ip_usage_count(con, ip: str, since_ts: int) -> int:
    return int(con.execute(
        "SELECT COUNT(*) AS c FROM upload_usage WHERE ip=%s AND created_at>%s",
        (ip, since_ts)).fetchone()["c"])


def insert_many(con, user_id: int, ip: str, sizes: list[int],
                created_at: int) -> list[int]:
    ids = []
    for size in sizes:
        row = con.execute(
            """
            INSERT INTO upload_usage(user_id, ip, size_bytes, created_at)
            VALUES(%s,%s,%s,%s) RETURNING id
            """,
            (user_id, ip, int(size), created_at)).fetchone()
        ids.append(int(row["id"]))
    return ids


def delete_ids(con, user_id: int, ids: list[int]) -> None:
    if not ids:
        return
    con.execute(
        "DELETE FROM upload_usage WHERE user_id=%s AND id = ANY(%s)",
        (user_id, ids))
