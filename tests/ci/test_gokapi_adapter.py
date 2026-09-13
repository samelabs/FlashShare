#!/usr/bin/env python3
"""Contract smoke for real core/gokapi.py against the CI boundary service."""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))
os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "ci-test-secret")

from gokapi import (  # noqa: E402
    delete_gokapi_file,
    get_gokapi_file,
    list_gokapi_files,
    open_gokapi_download,
    open_gokapi_internal_read,
    upload_to_gokapi,
)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"✓ {name}")


def main() -> None:
    data = b"flashshare-ci-gokapi"
    payload = upload_to_gokapi(
        io.BytesIO(data), "ci.txt", "text/plain", 1, 3, len(data)
    )
    info = payload.get("FileInfo") or {}
    file_id = info.get("Id") or ""
    check("upload returns file id", bool(file_id))

    row = get_gokapi_file(file_id)
    check("metadata round-trip", bool(row) and row.get("Name") == "ci.txt")
    check("initial download count is zero", int(row.get("DownloadCount", -1)) == 0)

    ok, rows = list_gokapi_files()
    check("list is authoritative", ok and any(r.get("Id") == file_id for r in rows))

    status, _headers, stream = open_gokapi_internal_read(file_id)
    body = stream.read()
    stream.close()
    check("non-counting internal read succeeds", status == 200 and body == data)

    row = get_gokapi_file(file_id)
    check("internal read preserves counter", int(row.get("DownloadCount", -1)) == 0)

    status, _headers, stream = open_gokapi_internal_read(file_id, "bytes=0-4")
    body = stream.read()
    stream.close()
    check("non-counting range read succeeds", status == 206 and body == data[:5])

    row = get_gokapi_file(file_id)
    check("range preview preserves counter", int(row.get("DownloadCount", -1)) == 0)

    status, _headers, stream = open_gokapi_download(file_id)
    body = stream.read()
    stream.close()
    check("counted download succeeds", status == 200 and body == data)

    row = get_gokapi_file(file_id)
    check("counted download increments counter", int(row.get("DownloadCount", -1)) == 1)

    delete_gokapi_file(file_id)
    check("delete removes metadata", get_gokapi_file(file_id) is None)
    print("PASS: gokapi adapter contract")


if __name__ == "__main__":
    main()
