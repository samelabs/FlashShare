import http.client
import json
import secrets
import urllib.parse

from config import GOKAPI_URL, GOKAPI_KEY


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


def open_gokapi_download(file_id: str):
    if not file_id:
        raise RuntimeError("missing_file_id")
    conn, _parsed = _gokapi_connection(timeout=300)
    conn.request("GET", _gokapi_path(f"/downloadFile?id={urllib.parse.quote(file_id)}"))
    resp = conn.getresponse()
    return resp.status, resp.getheaders(), resp


class InternalDownloadUnavailable(RuntimeError):
    """The authenticated non-counting Gokapi channel is unavailable.

    Raised by open_gokapi_internal_download when the official API
    cannot serve a non-counting read. Callers must surface a typed
    preview_unavailable (503) — a preview must NEVER fall back to the
    counted public download path.
    """


def open_gokapi_internal_download(file_id: str, range_header: str | None = None):
    """THE internal read primitive: non-counting authenticated stream.

    Uses ONLY the official authenticated API:
        GET /api/files/download/{id}
        apikey: GOKAPI_KEY
        increaseCounter: false
        optional Range header

    No /downloadFile fallback, no sqlite, no data-dir access. Returns
    (status, headers, response) like open_gokapi_download. Any non-2xx
    raises InternalDownloadUnavailable instead of leaking a counted
    fallback path.
    """
    if not file_id:
        raise InternalDownloadUnavailable("missing_file_id")
    if not GOKAPI_KEY:
        raise InternalDownloadUnavailable("api_key_not_configured")
    conn, _parsed = _gokapi_connection(timeout=300)
    headers = {"apikey": GOKAPI_KEY, "increaseCounter": "false"}
    if range_header:
        headers["Range"] = range_header
    conn.request("GET",
                 _gokapi_path(f"/api/files/download/{urllib.parse.quote(file_id)}"),
                 headers=headers)
    resp = conn.getresponse()
    if resp.status not in (200, 206):
        try:
            resp.read()
        finally:
            conn.close()
        raise InternalDownloadUnavailable(f"gokapi_api_{resp.status}")
    return resp.status, resp.getheaders(), resp


def verify_download_permission() -> None:
    """Startup capability probe (fail-closed, task 7).

    Proves the configured service key passes Gokapi's DOWNLOAD permission
    gate BEFORE the app accepts traffic. Read-only: no file is written
    and no counter moves (increaseCounter: false).

    Strategy: list files via the same key (proves the key authenticates),
    then issue an internal download probe against a known file id —
    either from the listing or a synthetic probe id. A 401/403 on the
    download endpoint means the key lacks DOWNLOAD permission:
        raise InternalDownloadUnavailable("gokapi_download_permission_missing")
    so main.py startup fails-closed with a clear log line, instead of
    every tool call and preview failing at user time.
    """
    if not GOKAPI_KEY:
        raise InternalDownloadUnavailable("api_key_not_configured")
    ok, files = list_gokapi_files()
    if not ok:
        raise InternalDownloadUnavailable("gokapi_unreachable")
    probe_id = (files[0].get("id") or files[0].get("Id")) if files else "__capability_probe__"
    conn, _parsed = _gokapi_connection(timeout=10)
    try:
        conn.request(
            "GET",
            _gokapi_path(f"/api/files/download/{urllib.parse.quote(probe_id)}"),
            headers={"apikey": GOKAPI_KEY, "increaseCounter": "false"},
        )
        resp = conn.getresponse()
        if resp.status in (401, 403):
            # fail-closed: DOWNLOAD permission missing on this key
            raise InternalDownloadUnavailable("gokapi_download_permission_missing")
        if resp.status == 200:
            resp.read()  # drain (only when a real file was probed)
        # 404 / 206 for the synthetic id both prove the permission gate
        # was passed (auth accepted) — the id simply does not exist.
    finally:
        try:
            conn.close()
        except Exception:
            pass
