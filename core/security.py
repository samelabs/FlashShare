import base64
import hashlib
import hmac
import os

import bcrypt

from config import ALLOWED_EXT, BLOCKED_EXT, SESSION_SECRET


def token_hash(token: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


def sign_source_token(user_id: int, file_id: str) -> str:
    """Sign an intermediate (gokapi-only) file to its uploading user.

    Tool flow uploads the original to gokapi without a DB record; this token
    proves ownership at /api/tools/execute time without persisting state.
    """
    msg = f"src:{user_id}:{file_id}".encode()
    return hmac.new(SESSION_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def verify_source_token(token: str, user_id: int, file_id: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(sign_source_token(user_id, file_id), token)


def sniff_allowed(filename: str, content_type: str, sample: bytes) -> tuple:
    ext = os.path.splitext(filename.lower())[1]
    if ext in BLOCKED_EXT:
        return False, "err_file_type_blocked"
    if ext not in ALLOWED_EXT:
        return False, "err_format_not_supported"

    if ext in {".jpg", ".jpeg"} and not sample.startswith(b"\xff\xd8"):
        return False, "err_content_mismatch_image"
    if ext == ".png" and not sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return False, "err_content_mismatch_image"
    if ext == ".gif" and not (sample.startswith(b"GIF87a") or sample.startswith(b"GIF89a")):
        return False, "err_content_mismatch_image"
    if ext == ".webp" and not (len(sample) >= 12 and sample[:4] == b"RIFF" and sample[8:12] == b"WEBP"):
        return False, "err_content_mismatch_image"
    if ext == ".bmp" and not sample.startswith(b"BM"):
        return False, "err_content_mismatch_image"
    if ext == ".pdf" and not sample.startswith(b"%PDF"):
        return False, "err_content_mismatch_pdf"
    if ext in {".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"} and not (
        sample.startswith(b"PK\x03\x04") or sample.startswith(b"PK\x05\x06") or sample.startswith(b"PK\x07\x08")
    ):
        return False, "err_content_mismatch_archive"
    if ext in {".doc", ".xls", ".ppt"} and not sample.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return False, "err_content_mismatch_office"
    if ext == ".7z" and not sample.startswith(b"\x37\x7a\xbc\xaf\x27\x1c"):
        return False, "err_content_mismatch_archive"
    if ext == ".rar" and not (sample.startswith(b"Rar!\x1a\x07\x00") or sample.startswith(b"Rar!\x1a\x07\x01\x00")):
        return False, "err_content_mismatch_archive"
    if ext in {".txt", ".csv", ".md", ".json", ".xml", ".yaml", ".yml",
               ".py", ".js", ".ts", ".html", ".htm", ".css", ".go", ".rs",
               ".java", ".c", ".cpp", ".h", ".rb", ".php", ".sh", ".sql"} and b"\x00" in sample:
        return False, "err_content_mismatch_text"
    import mimetypes
    return True, content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    # bcrypt hashes start with $2
    if stored.startswith("$2"):
        try:
            return bcrypt.checkpw(password.encode(), stored.encode())
        except Exception:
            return False
    # Legacy SHA-256 format: "base64(salt):base64(hash)"
    try:
        salt_b64, hash_b64 = stored.split(":", 1)
        salt = base64.b64decode(salt_b64)
        h = hashlib.sha256(salt + password.encode()).digest()
        return base64.b64encode(h).decode() == hash_b64
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    """Check if a password hash needs upgrading from SHA-256 to bcrypt."""
    return bool(stored) and not stored.startswith("$2")


# ── One-time signed source-fetch tokens (remote tool execution, phase 2) ──

import time as _time


def now_ts() -> int:
    return int(_time.time())


def _fetch_sig(user_id: int, gokapi_id: str, expires_at: int) -> str:
    msg = f"fetch:{user_id}:{gokapi_id}:{expires_at}".encode()
    return hmac.new(SESSION_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:40]


def sign_fetch_token(user_id: int, gokapi_id: str, expires_in: int = 600) -> str:
    """HMAC token authorizing ONE fetch of a source file within expires_in.

    Pull model: the remote endpoint fetches the input itself — the
    platform never pushes user files out. Binds (user, file, expiry);
    single-use is enforced by verify_fetch_token's redemption set
    (single uvicorn worker per run.sh --workers 1)."""
    expires_at = now_ts() + int(expires_in)
    return f"{expires_at}.{_fetch_sig(user_id, gokapi_id, expires_at)}"


_REDEEMED_FETCH = set()


def verify_fetch_token(token: str, user_id: int, gokapi_id: str) -> bool:
    """True exactly once per token. Expired or replayed tokens fail."""
    try:
        expires_at_str, sig = token.split(".", 1)
        expires_at = int(expires_at_str)
    except (ValueError, AttributeError):
        return False
    if expires_at < now_ts():
        return False
    if not hmac.compare_digest(sig, _fetch_sig(user_id, gokapi_id, expires_at)):
        return False
    if token in _REDEEMED_FETCH:
        return False
    _REDEEMED_FETCH.add(token)
    # Bound the redemption set: 淘汰最旧一半 (环形), 而非全清——
    # 全清会让清空瞬间的已用 token 重新可用 (重放窗口)。
    if len(_REDEEMED_FETCH) > 4096:
        doomed = iter(list(_REDEEMED_FETCH)[:2048])
        for t in doomed:
            _REDEEMED_FETCH.discard(t)
    return True

