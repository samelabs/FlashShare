"""Storage adapter — abstracts file storage behind a uniform interface.

Gokapi is the current implementation. Replacing it with S3/R2/local
only requires implementing the same interface; no caller changes.
"""

import io

from gokapi import (
    upload_to_gokapi,
    get_gokapi_file,
    delete_gokapi_file,
    open_gokapi_internal_download,
)


def read(file_id: str):
    """Read a file from storage.

    Returns (status, headers, stream) where stream is a seekable
    BytesIO. Consuming the full response into memory is intentional
    — files are already size-limited by the upload pipeline, and
    tool handlers need seekable input (Image.open, identity passthrough, etc).

    Read authority: platform internal reads (local tool source reads,
    remote /api/source fetches) are execution/presentation custody —
    they ride the authenticated non-counting primitive ONLY and never
    consume a recipient's download allowance. Real recipient downloads
    stay on the counted /downloadFile path.
    """
    from config import MAX_UPLOAD_BYTES
    status, headers, raw = open_gokapi_internal_download(file_id)
    try:
        # bounded chunked read: 不信任上游返回体大小, 也不依赖单次
        # read(n) 的返回语义 (short-read 合法) — 最多累计 cap+1 即停,
        # 超过平台单文件上限立即失败, 绝不整读进内存后再检查。
        chunks = []
        total = 0
        chunk_size = 1024 * 1024
        while total <= MAX_UPLOAD_BYTES:
            want = min(chunk_size, MAX_UPLOAD_BYTES + 1 - total)
            chunk = raw.read(want) if hasattr(raw, "read") else b""
            if not chunk:
                break
            total += len(chunk)
            chunks.append(chunk)
    finally:
        if hasattr(raw, "close"):
            raw.close()
    if total > MAX_UPLOAD_BYTES:
        raise ValueError("storage body exceeds platform file cap")
    return status, headers, io.BytesIO(b"".join(chunks))


def write(stream, filename: str, content_type: str,
          expiry_days: int, allowed_downloads: int, file_size: int) -> dict:
    """Write a file to storage.

    Args:
        stream: seekable file-like object positioned at 0.
        filename, content_type, expiry_days, allowed_downloads, file_size.

    Returns:
        Storage-specific metadata dict. For Gokapi:
        {"FileInfo": {"Id": "...", "ExpireAt": ..., "UrlDownload": "...", ...}}

    Raises:
        RuntimeError if upload fails.
    """
    return upload_to_gokapi(stream, filename, content_type,
                            expiry_days, allowed_downloads, file_size)


def delete(file_id: str) -> None:
    """Delete a file from storage."""
    delete_gokapi_file(file_id)


def info(file_id: str):
    """Get file metadata from storage (live status).

    Returns dict or None if not found / not configured.
    """
    return get_gokapi_file(file_id)
