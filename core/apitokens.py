"""API tokens — the agent/MCP channel (batch B2).

api_tokens rows parallel sessions: a session authenticates a browser
(cookie), a token authenticates a program (Bearer). Tokens are stored
hashed (sha256); the plaintext is shown exactly once at creation.

Prefix convention: fsk_<8 chars>... — the prefix lives in the DB so the
profile page can list tokens without the secret.
"""

import secrets

from database import db
from security import token_hash
from utils import now

TOKEN_PREFIX = "fsk_"


def _generate_token() -> tuple:
    """Return (plaintext, prefix). Plaintext never touches the DB."""
    body = secrets.token_urlsafe(32)
    return f"{TOKEN_PREFIX}{body}", body[:8]


def create_token(user_id: int, name: str) -> dict:
    """Create a token. Returns dict with plaintext included (only time)."""
    plaintext, prefix = _generate_token()
    ts = now()
    with db() as con:
        row = con.execute(
            """
            INSERT INTO api_tokens (user_id, name, token_hash, prefix, status, created_at)
            VALUES (%s, %s, %s, %s, 'active', %s)
            RETURNING id, user_id, name, prefix, status, last_used_at, expires_at, created_at
            """,
            (user_id, name.strip()[:64] or "token", token_hash(plaintext), prefix, ts),
        ).fetchone()
    out = dict(row)
    out["token"] = plaintext  # one-time secret
    return out


def revoke_token(user_id: int, token_id: int) -> bool:
    """Soft-revoke (status=revoked). Revoked tokens never authenticate."""
    with db() as con:
        cur = con.execute(
            "UPDATE api_tokens SET status = 'revoked' WHERE id = %s AND user_id = %s AND status = 'active'",
            (token_id, user_id),
        )
        return cur.rowcount > 0


def list_tokens(user_id: int) -> list:
    """All tokens for the profile page. No secrets — prefix only."""
    with db() as con:
        rows = con.execute(
            """
            SELECT id, name, prefix, status, last_used_at, expires_at, created_at
            FROM api_tokens WHERE user_id = %s ORDER BY id DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def authenticate_token(plaintext: str) -> dict | None:
    """Validate a Bearer token. Returns the owner user dict or None.

    Updates last_used_at opportunistically (non-fatal on failure —
    a throttled update must never block authentication).
    """
    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return None
    with db() as con:
        tok = con.execute(
            """
            SELECT t.id AS token_id, u.* FROM api_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = %s AND t.status = 'active'
              AND (t.expires_at IS NULL OR t.expires_at > %s)
            """,
            (token_hash(plaintext), now()),
        ).fetchone()
        if not tok:
            return None
        try:
            con.execute(
                "UPDATE api_tokens SET last_used_at = %s WHERE id = %s",
                (now(), tok["token_id"]),
            )
        except Exception:
            pass  # last_used is telemetry, not auth state
    d = dict(tok)
    d.pop("token_id", None)
    return d
