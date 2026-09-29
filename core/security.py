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


# ISO-BMFF container brands (bytes 8..12 of a `ftyp` box) → extension.
_BMFF_BRANDS = {
    b"heic": ".heic", b"heix": ".heic", b"hevc": ".heic",
    b"mif1": ".heic", b"msf1": ".heic",
    b"avif": ".avif",
    b"qt  ": ".mov",
    b"isom": ".mp4", b"mp41": ".mp4", b"mp42": ".mp4",
    b"iso2": ".mp4", b"M4V ": ".mp4",
}


def detect_ext(head: bytes) -> str | None:
    """Magic-number → extension, the single detection point for ingress
    diagnostics (content/extension mismatch reports) and placeholder
    result names. Covers common binary formats; unknown → None."""
    m = {
        b"\x89PNG\r\n\x1a\n": ".png",
        b"\xff\xd8": ".jpg",
        b"GIF87a": ".gif", b"GIF89a": ".gif",
        b"%PDF": ".pdf",
        b"BM": ".bmp",
        b"PK\x03\x04": ".zip",
        b"\x1f\x8b": ".gz",
        b"ID3": ".mp3", b"\xff\xfb": ".mp3", b"\xff\xf3": ".mp3",
        b"RIFF": ".webp",  # RIFF 容器细化需读 8-12 位 (WEBP/WAV), 保守归 webp
    }
    for magic, ext in m.items():
        if head.startswith(magic):
            if magic == b"RIFF" and len(head) >= 12 and head[8:12] == b"WAVE":
                return ".wav"
            if magic == b"RIFF" and len(head) >= 12 and head[8:12] != b"WEBP":
                continue
            return ext
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return _BMFF_BRANDS.get(head[8:12])
    return None


def mismatch_detail(filename: str, sample: bytes) -> dict:
    """Structured sniff-rejection detail shared by every ingress surface
    (web /api/uploads, MCP ticket PUT, MCP inline/url sources):
    the sanitized filename, the detected real format (or None) and a
    rename suggestion built from the original stem (or None). The error
    code itself stays verbatim at each entry point."""
    detected = detect_ext(sample)
    stem = os.path.splitext(filename)[0]
    return {
        "filename": filename,
        "detected": detected,
        "suggested_name": stem + detected if detected else None,
    }


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
               ".svg", ".step", ".stp", ".iges", ".igs", ".obj",
               ".log", ".diff", ".patch", ".toml", ".ini",
               ".sh", ".tsx", ".jsx", ".go", ".rs", ".java", ".ipynb"} and b"\x00" in sample:
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
