"""API tokens — the agent/MCP channel (batch B2).

api_tokens rows parallel sessions: a session authenticates a browser
(cookie), a token authenticates a program (Bearer). Tokens are stored
hashed (sha256); the plaintext is shown exactly once at creation.

Prefix convention: fsk_<8 chars>... — the prefix lives in the DB so the
profile page can list tokens without the secret.
"""

import secrets

from database import db
from repo import sessions as sessions_repo
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
        row = sessions_repo.insert_token(
            con, user_id, name.strip()[:64] or "token",
            token_hash(plaintext), prefix, ts)
    out = dict(row)
    out["token"] = plaintext  # one-time secret
    return out


def revoke_token(user_id: int, token_id: int) -> bool:
    """Soft-revoke (status=revoked). Revoked tokens never authenticate."""
    with db() as con:
        return sessions_repo.revoke_token(con, user_id, token_id) > 0


def list_tokens(user_id: int) -> list:
    """All tokens for the profile page. No secrets — prefix only."""
    with db() as con:
        rows = sessions_repo.list_tokens(con, user_id)
    return [dict(r) for r in rows]


def authenticate_token(plaintext: str) -> dict | None:
    """Validate a Bearer token. Returns the owner user dict or None.

    Updates last_used_at opportunistically (non-fatal on failure —
    a throttled update must never block authentication).
    """
    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return None
    with db() as con:
        tok = sessions_repo.user_by_api_token(con, token_hash(plaintext), now())
        if not tok:
            return None
        try:
            sessions_repo.touch_token(con, tok["token_id"], now())
        except Exception:
            pass  # last_used is telemetry, not auth state
    d = dict(tok)
    d.pop("token_id", None)
    return d
