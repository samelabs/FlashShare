"""repo: OAuth 2.1 authorization codes and tokens for /mcp.

CIMD clients have no table (the client_id URL is the identifier);
these rows are the OAuth channel only — they do not replace browser
sessions, X OAuth state, or the fsk_ API-key channel.
"""

def insert_code(con, code_hash: str, user_id: int, client_id: str,
                redirect_uri: str, code_challenge: str, scope: str,
                resource: str, expires_at: int, created_at: int) -> None:
    con.execute(
        """
        INSERT INTO oauth_mcp_codes
          (code_hash, user_id, client_id, redirect_uri, code_challenge,
           scope, resource, expires_at, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (code_hash, user_id, client_id, redirect_uri, code_challenge,
         scope, resource, expires_at, created_at),
    )


def code_for_update(con, code_hash: str, now_ts: int):
    return con.execute(
        """
        SELECT * FROM oauth_mcp_codes
        WHERE code_hash = %s AND expires_at > %s
        FOR UPDATE
        """,
        (code_hash, now_ts),
    ).fetchone()


def delete_code(con, code_hash: str) -> None:
    con.execute("DELETE FROM oauth_mcp_codes WHERE code_hash = %s",
                (code_hash,))


def insert_token(con, access_hash: str, refresh_hash: str, user_id: int,
                 client_id: str, scope: str, resource: str,
                 access_expires_at: int, refresh_expires_at: int,
                 created_at: int) -> None:
    con.execute(
        """
        INSERT INTO oauth_mcp_tokens
          (access_token_hash, refresh_token_hash, user_id, client_id, scope,
           resource, access_expires_at, refresh_expires_at, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (access_hash, refresh_hash, user_id, client_id, scope, resource,
         access_expires_at, refresh_expires_at, created_at),
    )


def user_by_access_token(con, access_hash: str, resource: str, now_ts: int):
    return con.execute(
        """
        SELECT u.*, t.scope AS oauth_scope, t.client_id AS oauth_client_id
        FROM oauth_mcp_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.access_token_hash = %s
          AND t.resource = %s
          AND t.revoked_at IS NULL
          AND t.access_expires_at > %s
          AND u.status <> 'suspended'
        """,
        (access_hash, resource, now_ts),
    ).fetchone()


def touch_access_token(con, access_hash: str, now_ts: int) -> None:
    con.execute(
        "UPDATE oauth_mcp_tokens SET last_used_at = %s "
        "WHERE access_token_hash = %s",
        (now_ts, access_hash),
    )


def refresh_for_update(con, refresh_hash: str, client_id: str,
                       resource: str, now_ts: int):
    return con.execute(
        """
        SELECT t.*, u.status AS user_status
        FROM oauth_mcp_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.refresh_token_hash = %s
          AND t.client_id = %s
          AND t.resource = %s
          AND t.revoked_at IS NULL
          AND t.refresh_expires_at > %s
        FOR UPDATE
        """,
        (refresh_hash, client_id, resource, now_ts),
    ).fetchone()


def rotate_token(con, token_id: int, access_hash: str, refresh_hash: str,
                 access_expires_at: int, refresh_expires_at: int,
                 now_ts: int) -> None:
    con.execute(
        """
        UPDATE oauth_mcp_tokens
        SET access_token_hash = %s,
            refresh_token_hash = %s,
            access_expires_at = %s,
            refresh_expires_at = %s,
            last_used_at = NULL,
            created_at = %s
        WHERE id = %s
        """,
        (access_hash, refresh_hash, access_expires_at, refresh_expires_at,
         now_ts, token_id),
    )


def delete_expired(con, now_ts: int) -> None:
    con.execute("DELETE FROM oauth_mcp_codes WHERE expires_at <= %s",
                (now_ts,))
    con.execute(
        "DELETE FROM oauth_mcp_tokens "
        "WHERE refresh_expires_at <= %s OR revoked_at IS NOT NULL",
        (now_ts,),
    )
