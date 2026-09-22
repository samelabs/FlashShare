import hashlib
import hmac
import json
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
               ".py", ".js", ".ts", ".html", ".htm", ".css", ".sql",
               ".svg", ".step", ".stp", ".iges", ".igs", ".obj"} and b"\x00" in sample:
        return False, "err_content_mismatch_text"
    import mimetypes
    return True, content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, stored: str) -> bool:
    """bcrypt is the only password format. Anything else fails closed."""
    if not stored or not stored.startswith("$2"):
        return False
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except Exception:
        return False


# ── Hosted-execution capabilities (/tool-endpoints/* authority) ──
# The hosted southbound surface is callable ONLY with this short-lived
# HMAC capability, minted by the canonical remote dispatch for trusted
# same-origin hosted endpoints. It binds the exact endpoint path and a
# digest over the outbound job payload, so a capability cannot be
# replayed against another endpoint or spliced onto a different set of
# signed source URLs. Ordinary API Bearer tokens are NOT authority for
# /tool-endpoints/*.

def _hosted_exec_sig(path: str, payload_digest: str, expires_at: int) -> str:
    msg = f"hostedexec:{path}:{payload_digest}:{expires_at}".encode()
    return hmac.new(SESSION_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:40]


def hosted_payload_digest(payload: dict) -> str:
    """Stable digest over the canonical outbound payload (job_inputs +
    params). Deterministic key order so both sides compute the same
    digest without depending on transport serialization order."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def sign_hosted_exec_token(path: str, payload: dict,
                           expires_in: int = 120) -> str:
    expires_at = now_ts() + int(expires_in)
    digest = hosted_payload_digest(payload)
    return f"{expires_at}.{_hosted_exec_sig(path, digest, expires_at)}"


def verify_hosted_exec_token(token: str, path: str, payload: dict) -> bool:
    """True iff the capability was minted for this exact path + payload
    and is unexpired. No redemption state: the short TTL plus payload
    binding make replay against the dispatch that requested it harmless
    (the source capability inside is independently one-time)."""
    try:
        expires_at_str, sig = token.split(".", 1)
        expires_at = int(expires_at_str)
    except (ValueError, AttributeError):
        return False
    if expires_at < now_ts():
        return False
    digest = hosted_payload_digest(payload)
    return hmac.compare_digest(sig, _hosted_exec_sig(path, digest, expires_at))


# ── One-time signed source-fetch tokens (remote tool execution) ──

import time as _time


def now_ts() -> int:
    return int(_time.time())


def _fetch_sig(user_id: int, gokapi_id: str, expires_at: int) -> str:
    msg = f"fetch:{user_id}:{gokapi_id}:{expires_at}".encode()
    return hmac.new(SESSION_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:40]


def sign_fetch_token(user_id: int, gokapi_id: str, expires_in: int = 600) -> str:
    expires_at = now_ts() + int(expires_in)
    return f"{expires_at}.{_fetch_sig(user_id, gokapi_id, expires_at)}"


# token -> expires_at. Single-worker state is sufficient for this short-lived
# internal fetch credential; valid redeemed markers are never evicted early.
_REDEEMED_FETCH: dict[str, int] = {}


def verify_fetch_token(token: str, user_id: int, gokapi_id: str) -> bool:
    """True exactly once while this process is alive and the token is valid."""
    try:
        expires_at_str, sig = token.split(".", 1)
        expires_at = int(expires_at_str)
    except (ValueError, AttributeError):
        return False
    ts = now_ts()
    if expires_at < ts:
        return False
    if not hmac.compare_digest(sig, _fetch_sig(user_id, gokapi_id, expires_at)):
        return False

    # Purge only markers whose token validity has ended. Removing a still-live
    # marker would make a previously redeemed token usable again.
    if len(_REDEEMED_FETCH) > 4096:
        for redeemed, exp in list(_REDEEMED_FETCH.items()):
            if exp < ts:
                _REDEEMED_FETCH.pop(redeemed, None)

    if token in _REDEEMED_FETCH:
        return False
    _REDEEMED_FETCH[token] = expires_at
    return True
