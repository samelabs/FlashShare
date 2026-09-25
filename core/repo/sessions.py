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
