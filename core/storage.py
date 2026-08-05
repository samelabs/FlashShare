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
    status, headers, raw = open_gokapi_download(file_id)
    data = raw.read() if hasattr(raw, "read") else b""
    if hasattr(raw, "close"):
        raw.close()
    return status, headers, io.BytesIO(data)


def read_bytes(file_id: str) -> bytes:
    """Read a file fully into memory. Use for small files only."""
    status, headers, stream = read(file_id)
    data = stream.read() if hasattr(stream, "read") else b""
    return data


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
