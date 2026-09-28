"""repo: tool_favorites — a user's pinned tools (我的收藏).

Plain set semantics per user: add is idempotent, remove of an absent
row is a no-op. Visibility is not checked here; callers validate the
tool against the catalog before adding, and every read projection
filters through the catalog visibility predicate.
"""


def list_keys(con, user_id: int) -> list:
    rows = con.execute(
        "SELECT tool_key FROM tool_favorites WHERE user_id = %s"
        " ORDER BY created_at, tool_key", (user_id,)).fetchall()
    return [r["tool_key"] for r in rows]


def add(con, user_id: int, tool_key: str, ts: int) -> None:
    con.execute(
        "INSERT INTO tool_favorites (user_id, tool_key, created_at)"
        " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (user_id, tool_key, ts))


def remove(con, user_id: int, tool_key: str) -> None:
    con.execute(
        "DELETE FROM tool_favorites WHERE user_id = %s AND tool_key = %s",
        (user_id, tool_key))
