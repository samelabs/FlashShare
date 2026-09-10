import http.client
import json
import secrets
import urllib.parse

from config import GOKAPI_URL, GOKAPI_KEY, GOKAPI_DATA_DIR


def _gokapi_connection(timeout=30):
    parsed = urllib.parse.urlparse(GOKAPI_URL)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    return conn_cls(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), timeout=timeout), parsed


def _gokapi_path(path: str) -> str:
    parsed = urllib.parse.urlparse(GOKAPI_URL)
    base = parsed.path.rstrip("/")
    return f"{base}{path if path.startswith('/') else '/' + path}"


def upload_to_gokapi(file_obj, filename: str, content_type: str, expiry_days: int, allowed_downloads: int, file_size: int) -> dict:
    """Upload a file to Gokapi. Streams from file_obj, never loads full file into memory.

    Args:
        file_obj: seekable file-like object (SpooledTemporaryFile)
        filename: sanitized filename
        content_type: MIME type
        expiry_days: expiry in days
        allowed_downloads: max downloads
        file_size: size in bytes (for Content-Length header)
    """
    if not GOKAPI_KEY:
        raise RuntimeError("Gokapi API key not configured")
    boundary = "----FlashShare" + secrets.token_hex(16)

    # Calculate total body size for Content-Length
    fields = {
        "allowedDownloads": str(allowed_downloads),
        "expiryDays": str(expiry_days),
    }
    header_parts = bytearray()
    for key, value in fields.items():
        header_parts.extend(f"--{boundary}\r\n".encode())
        header_parts.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    header_parts.extend(f"--{boundary}\r\n".encode())
    safe_name = filename.replace('"', "_")
    header_parts.extend(f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode())
    header_parts.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
    footer = f"\r\n--{boundary}--\r\n".encode()
    total_size = len(header_parts) + file_size + len(footer)

    conn, _parsed = _gokapi_connection(timeout=300)
    conn.connect()
    conn.putrequest("POST", _gokapi_path("/api/files/add"))
    conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
    conn.putheader("Content-Length", str(total_size))
    conn.putheader("apikey", GOKAPI_KEY)
    conn.endheaders()

    # Stream: headers → file chunks → footer
    sock = conn.sock
    if sock is None:
        raise RuntimeError("gokapi connection not established")
    sock.sendall(bytes(header_parts))
    file_obj.seek(0)
    while True:
        chunk = file_obj.read(64 * 1024)
        if not chunk:
            break
        sock.sendall(chunk)
    sock.sendall(footer)

    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        payload = {"raw": data.decode("utf-8", "replace")}
    if resp.status != 200 or payload.get("Result") != "OK":
        raise RuntimeError(payload.get("ErrorMessage") or f"Gokapi upload failed HTTP {resp.status}")
    return payload


def get_gokapi_file(file_id: str):
    if not GOKAPI_KEY or not file_id:
        return None
    conn, _parsed = _gokapi_connection(timeout=5)
    conn.request("GET", _gokapi_path(f"/api/files/list/{urllib.parse.quote(file_id)}"), headers={"apikey": GOKAPI_KEY})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if resp.status != 200:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def list_gokapi_files() -> tuple[bool, list]:
    """Return an authoritative snapshot of live storage files.

    `(True, files)` means Gokapi answered successfully; a valid JSON `null`
    response is normalized to an empty list. `(False, [])` means the snapshot
    is unavailable and MUST NOT be used to infer that local mappings are stale.
    """
    if not GOKAPI_KEY:
        return False, []
    conn = None
    try:
        conn, _parsed = _gokapi_connection(timeout=5)
        conn.request("GET", _gokapi_path("/api/files/list"), headers={"apikey": GOKAPI_KEY})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            return False, []
        payload = json.loads(data.decode("utf-8"))
        if payload is None:
            return True, []
        if not isinstance(payload, list):
            return False, []
        return True, payload
    except Exception:
        return False, []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def delete_gokapi_file(file_id: str) -> None:
    if not GOKAPI_KEY or not file_id:
        raise RuntimeError("delete_not_configured")
    conn, _parsed = _gokapi_connection(timeout=30)
    conn.request("POST", _gokapi_path("/api/files/delete"), headers={"apikey": GOKAPI_KEY, "id": file_id})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if resp.status == 200:
        return
    try:
        payload = json.loads(data.decode("utf-8"))
        message = payload.get("ErrorMessage") or payload.get("error") or f"delete_failed_{resp.status}"
    except Exception:
        message = data.decode("utf-8", "replace") or f"delete_failed_{resp.status}"
    raise RuntimeError(message)


class InternalDownloadUnavailable(RuntimeError):
    """The internal non-counting read primitive could not serve this file.

    Raised instead of silently falling back to the counted public download
    path — a preview must never consume a user's download allowance.
    """


def open_gokapi_internal_download(file_id: str, range_header: str | None = None):
    """Open a file for internal platform use WITHOUT consuming the download
    counter (preview, zip central-directory inspection, print embedding).

    Resolution order:
    1. Official authenticated API GET /api/files/download/{id} with
       apikey + increaseCounter:false (available on newer Gokapi builds).
    2. Same-host data directory read (id -> SHA1 from the files API), which
       bypasses HTTP entirely. This is NOT the counted /downloadFile path.
    3. Neither works -> InternalDownloadUnavailable (explicit preview
       unavailability; never a silent counted fallback).

    Returns (status, headers, stream) like open_gokapi_download.
    """
    import urllib.parse
    if not file_id:
        raise InternalDownloadUnavailable("missing_file_id")
    headers = {"apikey": GOKAPI_KEY, "increaseCounter": "false"}
    if range_header:
        headers["Range"] = range_header
    conn, _parsed = _gokapi_connection(timeout=300)
    conn.request(
        "GET",
        _gokapi_path(f"/api/files/download/{urllib.parse.quote(file_id)}"),
        headers=headers)
    resp = conn.getresponse()
    if resp.status == 200:
        return resp.status, resp.getheaders(), resp
    resp.read()
    # API route not usable on this build (e.g. admin-session-only scope) —
    # fall back to the same-host data directory, never to the counted path.
    try:
        return _local_data_read(file_id, range_header)
    except Exception as exc:
        raise InternalDownloadUnavailable(str(exc)) from exc


def _local_data_read(file_id: str, range_header: str | None = None):
    import io as _io
    import os as _os
    import re as _re
    import sqlite3 as _sqlite3
    info = get_gokapi_file(file_id)
    if not info:
        raise InternalDownloadUnavailable("file_not_in_storage")
    # Content address (SHA1) is not part of the list API response — read it
    # from the same-host Gokapi metadata database (read-only query).
    sha1 = ""
    import glob as _glob
    candidates = [
        _os.path.join(GOKAPI_DATA_DIR, "gokapi.sqlite"),
        _os.path.join(_os.path.dirname(GOKAPI_DATA_DIR.rstrip("/")), "data", "gokapi.sqlite"),
        "/opt/gokapi/data/gokapi.sqlite",
    ]
    candidates += _glob.glob("/opt/gokapi/*/gokapi.sqlite")
    db_path = next((p for p in candidates if _os.path.isfile(p)), "")
    if db_path:
        try:
            con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = con.execute(
                "SELECT SHA1 FROM FileMetaData WHERE Id = ?",
                (file_id,)).fetchone()
            con.close()
            sha1 = str(row[0]) if row and row[0] else ""
        except Exception:
            sha1 = ""
    if not _re.fullmatch(r"[0-9a-f]{40}", sha1):
        raise InternalDownloadUnavailable("no_content_address")
    path = _os.path.join(GOKAPI_DATA_DIR, sha1)
    if not _os.path.isfile(path):
        raise InternalDownloadUnavailable("content_object_missing")
    with open(path, "rb") as fh:
        if range_header:
            m = _re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not m:
                raise InternalDownloadUnavailable("bad_range")
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else _os.path.getsize(path) - 1
            fh.seek(start)
            data = fh.read(end - start + 1)
            headers = [
                ("Content-Range", f"bytes {start}-{end}/{_os.path.getsize(path)}"),
                ("Content-Length", str(len(data))),
                ("Accept-Ranges", "bytes"),
            ]
            return 206, headers, _io.BytesIO(data)
        data = fh.read()
    headers = [
        ("Content-Length", str(len(data))),
        ("Content-Type", info.get("ContentType") or "application/octet-stream"),
    ]
    return 200, headers, _io.BytesIO(data)


def open_gokapi_download(file_id: str):
    if not file_id:
        raise RuntimeError("missing_file_id")
    conn, _parsed = _gokapi_connection(timeout=300)
    conn.request("GET", _gokapi_path(f"/downloadFile?id={urllib.parse.quote(file_id)}"))
    resp = conn.getresponse()
    return resp.status, resp.getheaders(), resp
