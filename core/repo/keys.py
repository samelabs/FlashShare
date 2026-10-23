"""repo: api_keys — the Bearer credential store (agent/MCP channel).

key_hash authenticates (HMAC-SHA256, same authority as sessions);
key_sealed holds the encrypted copy (secrets_vault enc1 format) that
lets the owner view the full key again. Deleting a key is a physical
DELETE — there is no status column and no revoked state.
"""

from typing import Optional


def insert_key(con, user_id: int, name: str, key_hash: str,
               key_sealed: str, prefix: str, ts: int,
               scopes: str) -> dict:
    return con.execute(
        """
        INSERT INTO api_keys (user_id, name, key_hash, key_sealed, prefix,
                              created_at, scopes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id, user_id, name, key_sealed, prefix, last_used_at,
                  created_at, scopes
        """,
        (user_id, name, key_hash, key_sealed, prefix, ts, scopes)).fetchone()


def count_keys_for_update(con, user_id: int) -> int:
    """Count under the caller-held users row lock (serialize concurrent
    creates; the limit is enforced on this count)."""
    return int(con.execute(
        "SELECT COUNT(*) AS n FROM api_keys WHERE user_id = %s",
        (user_id,)).fetchone()["n"])


def delete_key(con, user_id: int, key_id: int) -> int:
    cur = con.execute(
        "DELETE FROM api_keys WHERE id = %s AND user_id = %s",
        (key_id, user_id))
    return cur.rowcount


def list_keys(con, user_id: int) -> list:
    return con.execute(
        """
        SELECT id, name, prefix, last_used_at, created_at
        FROM api_keys WHERE user_id = %s ORDER BY id DESC
        """,
        (user_id,)).fetchall()


def key_for_owner(con, user_id: int, key_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM api_keys WHERE id = %s AND user_id = %s",
        (key_id, user_id)).fetchone()


def user_by_api_key(con, key_hash: str) -> Optional[dict]:
    """The owner of a Bearer key. Suspended accounts never authenticate."""
    return con.execute(
        """
        SELECT k.id AS key_id, k.scopes AS key_scopes, u.*
               FROM api_keys k
        JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = %s AND u.status <> 'suspended'
        """,
        (key_hash,)).fetchone()


def touch_key(con, key_id: int, ts: int) -> None:
    from identity.capabilities import LAST_USED_TOUCH_INTERVAL_S
    con.execute(
        "UPDATE api_keys SET last_used_at = %s WHERE id = %s"
        " AND (last_used_at IS NULL OR last_used_at < %s)",
        (ts, key_id, ts - LAST_USED_TOUCH_INTERVAL_S))
