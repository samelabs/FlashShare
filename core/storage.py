"""Storage adapter — abstracts file storage behind a uniform interface.

Gokapi is the current implementation. Replacing it with S3/R2/local
only requires implementing the same interface; no caller changes.
"""

import io

from gokapi import (
    upload_to_gokapi,
    get_gokapi_file,
    delete_gokapi_file,
    open_gokapi_download,
)


def read(file_id: str):
    """Read a file from storage.

    Returns (status, headers, stream) where stream is a seekable
    BytesIO. Consuming the full response into memory is intentional
    — files are already size-limited by the upload pipeline, and
    tool handlers need seekable input (Image.open, identity passthrough, etc).
    """
    from config import MAX_UPLOAD_BYTES
    status, headers, raw = open_gokapi_download(file_id)
    try:
        # bounded: 不信任上游返回体大小 — 超过平台单文件上限立即失败,
        # 绝不整读进内存后再检查。
        data = raw.read(MAX_UPLOAD_BYTES + 1) if hasattr(raw, "read") else b""
    finally:
        if hasattr(raw, "close"):
            raw.close()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("storage body exceeds platform file cap")
    return status, headers, io.BytesIO(data)


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
