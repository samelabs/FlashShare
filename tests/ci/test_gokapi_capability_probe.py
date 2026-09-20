#!/usr/bin/env python3
"""Gokapi capability probe contract (task 7/10/12).

Against a running fake_gokapi (FLASHSHARE_GOKAPI_URL):
  1. key with DOWNLOAD permission → verify_download_permission() passes
  2. internal endpoint failing 401 → typed
     InternalDownloadUnavailable("gokapi_download_permission_missing")
     (startup must abort; here we assert the typed failure the startup
     hook raises on)
  3. storage.read() full read via internal primitive → count stays 0
  4. public /downloadFile → count moves to 1

Requires the fake_gokapi process (tests/support/fake_gokapi.py).
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.request

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "core"))

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name}")


def fail_internal(mode):
    req = urllib.request.Request(
        os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/") + "/ci/fail-internal",
        data=json.dumps({"mode": mode}).encode(),
        headers={"Content-Type": "application/json", "apikey": os.environ.get("FLASHSHARE_GOKAPI_KEY", "")},
        method="POST")
    urllib.request.urlopen(req).read()


def download_count(file_id):
    from gokapi import get_gokapi_file
    info = get_gokapi_file(file_id)
    return (info or {}).get("DownloadCount", 0)


def main():
    from gokapi import (InternalDownloadUnavailable, upload_to_gokapi,
                        verify_download_permission)
    from storage import read as storage_read

    payload = upload_to_gokapi(io.BytesIO(b"probe-seed-bytes"), "seed.txt",
                               "text/plain", 1, 5, len(b"probe-seed-bytes"))
    file_id = payload["FileInfo"]["Id"]

    # 1. permitted key → probe passes
    verify_download_permission()
    check("capability probe: permitted key → pass", True)

    # 2. missing DOWNLOAD permission → typed fail-closed marker
    fail_internal("401")
    try:
        verify_download_permission()
        check("capability probe: 401 → fail-closed", False)
    except InternalDownloadUnavailable as exc:
        check("capability probe: 401 → fail-closed (gokapi_download_permission_missing)",
              "gokapi_download_permission_missing" in str(exc))
    fail_internal(None)

    # 3. storage.read() is the internal non-counting primitive
    status, headers, stream = storage_read(file_id)
    body = stream.read()
    check("storage.read full read succeeds", status == 200 and body == b"probe-seed-bytes")
    check("storage.read keeps DownloadCount at 0", download_count(file_id) == 0)

    # 4. public /downloadFile is counted
    status, _h, stream = __import__("gokapi").open_gokapi_download(file_id)
    stream.read()
    check("public /downloadFile counts (0 → 1)", download_count(file_id) == 1)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
