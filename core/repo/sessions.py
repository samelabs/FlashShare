"""repo: sessions — browser sessions + API token persistence.

Functions are transaction-agnostic repo primitives; authentication lookups
apply the account suspension gate so every session/token consumer sees the same
identity state.
"""

from typing import Optional


# ── sessions (browser cookie) ──

def insert(con, token_hash: str, user_id: int, expires_at: int, created_at: int) -> None:
    con.execute(
        "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)",
        (token_hash, user_id, expires_at, created_at))


def user_by_session_token(con, token_hash: str, now_ts: int) -> Optional[dict]:
    return con.execute(
        """
        SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > %s
          AND u.status <> 'suspended'
        """,
        (token_hash, now_ts)).fetchone()


def delete_session(con, token_hash: str) -> None:
    con.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))


def delete_user_sessions(con, user_id: int) -> None:
    con.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


def delete_expired(con, now_ts: int) -> None:
    con.execute("DELETE FROM sessions WHERE expires_at < %s", (now_ts,))


# ── oauth_states (X OAuth intermediate state) ──

def insert_oauth_state(con, state: str, verifier: str, expires_at: int,
                       created_at: int, bind_user_id: Optional[int]) -> None:
    con.execute(
        "INSERT INTO oauth_states(state, verifier, expires_at, created_at, bind_user_id) VALUES(%s,%s,%s,%s,%s)",
        (state, verifier, expires_at, created_at, bind_user_id))


def pop_oauth_state(con, state: str, now_ts: int) -> Optional[dict]:
    row = con.execute(
        "SELECT * FROM oauth_states WHERE state = %s AND expires_at > %s",
        (state, now_ts)).fetchone()
    con.execute("DELETE FROM oauth_states WHERE state = %s", (state,))
    return row


def delete_expired_oauth(con, now_ts: int) -> None:
    con.execute("DELETE FROM oauth_states WHERE expires_at < %s", (now_ts,))


# ── api_tokens (Bearer, agent/MCP channel) ──

def insert_token(con, user_id: int, name: str, token_hash: str,
                 prefix: str, ts: int) -> dict:
    return con.execute(
        """
        INSERT INTO api_tokens (user_id, name, token_hash, prefix, status, created_at)
        VALUES (%s, %s, %s, %s, 'active', %s)
        RETURNING id, user_id, name, prefix, status, last_used_at, expires_at, created_at
        """,
        (user_id, name, token_hash, prefix, ts)).fetchone()


def revoke_token(con, user_id: int, token_id: int) -> int:
    cur = con.execute(
        "UPDATE api_tokens SET status = 'revoked' WHERE id = %s AND user_id = %s AND status = 'active'",
        (token_id, user_id))
    return cur.rowcount


def revoke_all_tokens(con, user_id: int) -> int:
    cur = con.execute(
        "UPDATE api_tokens SET status='revoked' WHERE user_id=%s AND status='active'",
        (user_id,))
    return cur.rowcount


def list_tokens(con, user_id: int) -> list:
    return con.execute(
        """
        SELECT id, name, prefix, status, last_used_at, expires_at, created_at
        FROM api_tokens WHERE user_id = %s ORDER BY id DESC
        """,
        (user_id,)).fetchall()


def user_by_api_token(con, token_hash: str, now_ts: int) -> Optional[dict]:
    return con.execute(
        """
        SELECT t.id AS token_id, u.* FROM api_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = %s AND t.status = 'active'
          AND u.status <> 'suspended'
          AND (t.expires_at IS NULL OR t.expires_at > %s)
        """,
        (token_hash, now_ts)).fetchone()


def touch_token(con, token_id: int, ts: int) -> None:
    con.execute(
        "UPDATE api_tokens SET last_used_at = %s WHERE id = %s", (ts, token_id))
