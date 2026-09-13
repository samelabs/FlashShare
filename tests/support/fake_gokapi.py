#!/usr/bin/env python3
"""Deterministic HTTP test double for the Gokapi subset used by FlashShare CI.

Only the external HTTP contract consumed by core/gokapi.py is modeled here.
FlashShare ownership, quota, tool, economy, and lifecycle logic remain in the
real application under test.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._files: dict[str, dict] = {}

    def add(self, name: str, content_type: str, data: bytes,
            expiry_days: int, allowed_downloads: int, base_url: str) -> dict:
        with self._lock:
            self._seq += 1
            file_id = f"ci{self._seq:010d}"
            row = {
                "Id": file_id,
                "Name": name,
                "ContentType": content_type or "application/octet-stream",
                "SizeBytes": len(data),
                "ExpireAt": int(time.time()) + max(1, expiry_days) * 86400,
                "AllowedDownloads": allowed_downloads,
                "DownloadCount": 0,
                "DownloadsRemaining": allowed_downloads,
                "UrlDownload": f"{base_url}/downloadFile?id={quote(file_id)}",
                "UrlHotlink": f"{base_url}/downloadFile?id={quote(file_id)}",
                "_data": data,
            }
            self._files[file_id] = row
            return self._public(row)

    def get(self, file_id: str) -> dict | None:
        with self._lock:
            row = self._files.get(file_id)
            return self._public(row) if row else None

    def list(self) -> list[dict]:
        with self._lock:
            return [self._public(row) for row in self._files.values()]

    def delete(self, file_id: str) -> None:
        with self._lock:
            self._files.pop(file_id, None)

    def download(self, file_id: str) -> tuple[dict | None, bytes | None]:
        """Public delivery read: consume one download slot."""
        with self._lock:
            row = self._files.get(file_id)
            if not row:
                return None, None
            allowed = int(row["AllowedDownloads"])
            if allowed > 0 and int(row["DownloadCount"]) >= allowed:
                return self._public(row), None
            row["DownloadCount"] = int(row["DownloadCount"]) + 1
            row["DownloadsRemaining"] = (
                max(0, allowed - int(row["DownloadCount"])) if allowed > 0 else 0
            )
            return self._public(row), bytes(row["_data"])

    def read(self, file_id: str) -> tuple[dict | None, bytes | None]:
        """Privileged API read: return bytes without changing counters."""
        with self._lock:
            row = self._files.get(file_id)
            if not row:
                return None, None
            return self._public(row), bytes(row["_data"])

    @staticmethod
    def _public(row: dict) -> dict:
        return {k: v for k, v in row.items() if not k.startswith("_")}


STORE = Store()
API_KEY = ""


def _multipart(body: bytes, content_type: str) -> tuple[dict[str, str], tuple[str, str, bytes]]:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not match:
        raise ValueError("missing_boundary")
    boundary = (match.group(1) or match.group(2)).strip().encode()
    fields: dict[str, str] = {}
    file_part: tuple[str, str, bytes] | None = None
    marker = b"--" + boundary
    for raw in body.split(marker):
        part = raw.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        head, sep, payload = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        headers = head.decode("utf-8", "replace")
        disp = re.search(
            r'Content-Disposition:\s*form-data;\s*name="([^"]+)"(?:;\s*filename="([^"]*)")?',
            headers, re.I)
        if not disp:
            continue
        name, filename = disp.group(1), disp.group(2)
        if filename is not None:
            ctype = "application/octet-stream"
            ctype_match = re.search(r"^Content-Type:\s*([^\r\n]+)", headers, re.I | re.M)
            if ctype_match:
                ctype = ctype_match.group(1).strip()
            file_part = (filename, ctype, payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    if file_part is None:
        raise ValueError("missing_file")
    return fields, file_part


class Handler(BaseHTTPRequestHandler):
    server_version = "FlashShareFakeGokapi/1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.log_date_time_string()} {fmt % args}", flush=True)

    def _json(self, status: int, payload) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return bool(API_KEY) and self.headers.get("apikey", "") == API_KEY

    def _base_url(self) -> str:
        return f"http://{self.headers.get('Host', '127.0.0.1')}"

    def _file_response(self, meta: dict | None, data: bytes | None) -> None:
        if meta is None:
            self.send_error(404)
            return
        if data is None:
            self.send_error(410)
            return

        status = 200
        start, end = 0, len(data) - 1
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not match:
                self.send_error(416)
                return
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else end
            if start < 0 or start >= len(data) or end < start:
                self.send_error(416)
                return
            end = min(end, len(data) - 1)
            data = data[start:end + 1]
            status = 206

        self.send_response(status)
        self.send_header("Content-Type", meta["ContentType"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{meta['SizeBytes']}")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._json(200, {"ok": True})
            return

        if path == "/api/files/list":
            if not self._authorized():
                self._json(401, {"ErrorMessage": "unauthorized"})
                return
            self._json(200, STORE.list())
            return

        if path.startswith("/api/files/list/"):
            if not self._authorized():
                self._json(401, {"ErrorMessage": "unauthorized"})
                return
            file_id = path.rsplit("/", 1)[-1]
            row = STORE.get(file_id)
            if not row:
                self._json(404, {"ErrorMessage": "not_found"})
                return
            self._json(200, row)
            return

        if path.startswith("/api/files/download/"):
            if not self._authorized():
                self._json(401, {"ErrorMessage": "unauthorized"})
                return
            file_id = path.rsplit("/", 1)[-1]
            meta, data = STORE.read(file_id)
            self._file_response(meta, data)
            return

        if path == "/downloadFile":
            file_id = (parse_qs(parsed.query).get("id") or [""])[0]
            meta, data = STORE.download(file_id)
            self._file_response(meta, data)
            return

        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/files/add":
            if not self._authorized():
                self._json(401, {"Result": "ERROR", "ErrorMessage": "unauthorized"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size)
                fields, (filename, content_type, data) = _multipart(
                    body, self.headers.get("Content-Type", ""))
                meta = STORE.add(
                    filename,
                    content_type,
                    data,
                    int(fields.get("expiryDays", "1")),
                    int(fields.get("allowedDownloads", "50")),
                    self._base_url(),
                )
            except Exception as exc:
                self._json(400, {"Result": "ERROR", "ErrorMessage": str(exc)})
                return
            self._json(200, {"Result": "OK", "FileInfo": meta})
            return

        if path == "/api/files/delete":
            if not self._authorized():
                self._json(401, {"ErrorMessage": "unauthorized"})
                return
            STORE.delete(self.headers.get("id", ""))
            self._json(200, {"Result": "OK"})
            return

        self.send_error(404)


def main() -> None:
    global API_KEY
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=53898)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    API_KEY = args.key
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake-gokapi listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
