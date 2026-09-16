"""Tool endpoint credential vault (Fernet, single read/write path).

Protects users' tool auth_token at rest: DB dump alone can no longer
harvest third-party credentials. Key: TOOL_VAULT_KEY env (Fernet key).

Contract:
    seal("")            → ""            (no secret, nothing to protect)
    seal(secret) + key  → enc1:<ct>
    seal(secret) no key → VaultUnavailable  (fail closed — plaintext
                          at rest is NOT an acceptable new write)
    unseal(legacy plaintext)   → passthrough (temporary read
                          compatibility; re-seal migration is separate)
    unseal(enc1:) + key        → plaintext
    unseal(enc1:) no key       → VaultUnavailable
    unseal(enc1:) wrong key    → VaultDecryptError

vault_enabled() is true only when a valid Fernet key is loaded. A
malformed key is unavailable, never an unclassified 500.
"""
import os

from cryptography.fernet import Fernet, InvalidToken

MARKER = "enc1:"


class VaultUnavailable(RuntimeError):
    """Vault key missing/invalid — new secret writes and sealed reads
    are refused (typed), instead of silently degrading to plaintext."""


class VaultDecryptError(RuntimeError):
    """Sealed row cannot be decrypted (wrong/rotated key or bad
    ciphertext) — never fall back to returning ciphertext or ''."""


def _load_fernet() -> Fernet | None:
    key = os.environ.get("TOOL_VAULT_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        # malformed key (bad length/base64) — unavailable, not a crash
        return None


_f = _load_fernet()


def _fernet() -> Fernet:
    if _f is None:
        raise VaultUnavailable("TOOL_VAULT_KEY not configured")
    return _f


def vault_enabled() -> bool:
    return _f is not None


def seal(plaintext: str) -> str:
    """Encrypt for storage. Empty secret → empty string; a non-empty
    secret without a working vault key is REFUSED (VaultUnavailable)."""
    if not plaintext:
        return ""
    return MARKER + _fernet().encrypt(plaintext.encode()).decode()


def unseal(stored: str) -> str:
    """Decrypt for the single dispatch path. Understands BOTH sealed
    (enc1:…) and legacy plaintext rows — pre-migration data keeps
    working; re-sealing is a separate migration, not a flag day."""
    if not stored:
        return stored
    if not stored.startswith(MARKER):
        return stored          # legacy plaintext, untouched
    try:
        return _fernet().decrypt(stored[len(MARKER):].encode()).decode()
    except InvalidToken as exc:
        raise VaultDecryptError("sealed credential cannot be decrypted") from exc
