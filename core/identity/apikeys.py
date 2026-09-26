"""API keys — the agent/MCP channel credential.

api_keys rows parallel sessions: a session authenticates a browser
(cookie), an API key authenticates a program (Bearer). Authentication
checks key_hash only (HMAC-SHA256); a second, encrypted copy
(key_sealed, secrets_vault with TOOL_VAULT_KEY) lets the owner view the
full key again from the keys page. Creating a key without a working
vault fails with a typed error — there is no show-once fallback.

Prefix convention: fsk_<8 chars>… — the prefix lives in the DB so the
keys page can list keys without decrypting anything. The prefix is the
credential format, not product vocabulary.
"""

import secrets

from database import db
from repo import keys as keys_repo
from repo import ledger as ledger_repo
from security import token_hash
from utils import now

KEY_PREFIX = "fsk_"

# Per-user cap, enforced under the users row lock.
MAX_KEYS_PER_USER = 10


def _generate_key() -> tuple:
    """Return (plaintext, prefix). Plaintext never touches the DB raw."""
    body = secrets.token_urlsafe(32)
    return f"{KEY_PREFIX}{body}", body[:8]


def create_key(user_id: int, name: str) -> dict:
    """Create an API key. Returns the row with the plaintext included
    (the only moment the caller sees it outside reveal).

    Raises ValueError('err_key_limit') when the user already holds the
    maximum number of keys (counted under the users row lock, so
    concurrent creates cannot exceed it) and ValueError('err_vault')
    when the encryption vault is unavailable (a key is never created
    without its encrypted copy)."""
    from secrets_vault import VaultUnavailable, seal
    plaintext, prefix = _generate_key()
    ts = now()
    try:
        sealed = seal(plaintext)
    except VaultUnavailable as exc:
        raise ValueError("err_vault") from exc
    with db() as con:
        ledger_repo.lock_user(con, user_id)
        n = keys_repo.count_keys_for_update(con, user_id)
        if n >= MAX_KEYS_PER_USER:
            raise ValueError("err_key_limit")
        row = keys_repo.insert_key(
            con, user_id, name.strip()[:64] or "key",
            token_hash(plaintext), sealed, prefix, ts)
    out = dict(row)
    out["key"] = plaintext
    return out


def delete_key(user_id: int, key_id: int) -> bool:
    """Physically delete one key. Programs using it lose access
    immediately."""
    with db() as con:
        return keys_repo.delete_key(con, user_id, key_id) > 0


def list_keys(user_id: int) -> list:
    """All keys for the keys page. Prefixes only — no secrets; every key
    carries an encrypted copy, so the full value is revealable any
    time from the owner's browser session."""
    with db() as con:
        rows = keys_repo.list_keys(con, user_id)
    return [dict(r) for r in rows]


def reveal_key(user_id: int, key_id: int) -> str | None:
    """The full key plaintext for the owner's own browser session.
    None when the key does not exist; ValueError('err_vault') when the
    stored copy cannot be decrypted."""
    from secrets_vault import unseal
    with db() as con:
        row = keys_repo.key_for_owner(con, user_id, key_id)
    if row is None:
        return None
    return unseal(row["key_sealed"])


def authenticate_key(plaintext: str) -> dict | None:
    """Validate a Bearer key. Returns the owner user dict or None.

    Updates last_used_at opportunistically (non-fatal on failure —
    a throttled update must never block authentication).
    """
    if not plaintext or not plaintext.startswith(KEY_PREFIX):
        return None
    with db() as con:
        key = keys_repo.user_by_api_key(con, token_hash(plaintext))
        if not key:
            return None
        try:
            keys_repo.touch_key(con, key["key_id"], now())
        except Exception:
            pass  # last_used is telemetry, not auth state
    d = dict(key)
    d.pop("key_id", None)
    return d
