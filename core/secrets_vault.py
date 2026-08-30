"""Tool endpoint credential vault (Fernet, single read/write path).

Protects users' tool auth_token at rest: DB dump alone can no longer
harvest third-party credentials. Key: TOOL_VAULT_KEY env (Fernet key).
Absent key → vault disabled: writes stay plaintext, reads pass through
(feature parity, no surprise breakage). Enable by setting the env +
running the one-shot migration.
"""
import os

from cryptography.fernet import Fernet, InvalidToken

_KEY = os.environ.get("TOOL_VAULT_KEY", "").strip()
_f = Fernet(_KEY.encode()) if _KEY else None

MARKER = "enc1:"


def vault_enabled() -> bool:
    return _f is not None


def seal(plaintext: str) -> str:
    """Encrypt for storage. Vault off → store as-is (legacy behaviour)."""
    if not plaintext or _f is None:
        return plaintext
    return MARKER + _f.encrypt(plaintext.encode()).decode()


def unseal(stored: str) -> str:
    """Decrypt for the single dispatch path. Understands BOTH sealed
    (enc1:…) and legacy plaintext rows — so pre-migration data keeps
    working and migration is not a flag day."""
    if not stored or _f is None:
        return stored
    if not stored.startswith(MARKER):
        return stored          # legacy plaintext, untouched
    try:
        return _f.decrypt(stored[len(MARKER):].encode()).decode()
    except InvalidToken:
        # wrong key (rotated without re-migration) — fail closed: return
        # nothing usable rather than leaking a malformed header value.
        return ""
