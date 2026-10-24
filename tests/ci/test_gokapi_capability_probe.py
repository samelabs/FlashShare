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


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name}" + (f" — {detail}" if detail else ""))


def fail_internal(mode):
    req = urllib.request.Request(
        os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/") + "/ci/fail-internal",
        data=json.dumps({"mode": mode}).encode(),
        headers={"Content-Type": "application/json", "apikey": os.environ.get("FLASHSHARE_GOKAPI_KEY", "")},
        method="POST")
    urllib.request.urlopen(req).read()


def download_count(file_id):
    from gokapi import get_gokapi_file
    _ok, info = get_gokapi_file(file_id)
    return (info or {}).get("DownloadCount", 0)


def main():
    from gokapi import (InternalDownloadUnavailable, upload_to_gokapi,
                        verify_download_permission)
    from storage import read as storage_read

    payload = upload_to_gokapi(io.BytesIO(b"probe-seed-bytes"), "seed.txt",
                               "text/plain", 1, 5, len(b"probe-seed-bytes"))
    file_id = payload["FileInfo"]["Id"]

    # ── real-file probe matrix (storage non-empty) ──
    # 1. permitted key + real file + 200 → PASS
    verify_download_permission()
    check("real file + 200 → PASS", True)

    # 2-6. failure matrix: every non-200/206 status fails closed, with the
    # permission marker reserved for 401/403
    for mode, expect in (("401", "gokapi_download_permission_missing"),
                         ("403", "gokapi_download_permission_missing"),
                         ("404", "gokapi_download_capability_unknown_404"),
                         ("500", "gokapi_download_capability_unknown_500"),
                         ("405", "gokapi_download_capability_unknown_405")):
        fail_internal(mode)
        try:
            verify_download_permission()
            check(f"real file + {mode} → FAIL (fail-closed)", False,
                  "probe unexpectedly passed")
        except InternalDownloadUnavailable as exc:
            check(f"real file + {mode} → FAIL ({expect.split('_', 2)[-1]})",
                  expect in str(exc), str(exc))
        finally:
            fail_internal(None)

    # ── synthetic-id probe matrix (storage emptied) ──
    from gokapi import delete_gokapi_file, list_gokapi_files
    delete_gokapi_file(file_id)
    _ok, remaining = list_gokapi_files()
    if remaining:
        for f in list(remaining):
            delete_gokapi_file(f.get("id") or f.get("Id"))

    # contract-proven missing-file 404 → auth gate passed
    fail_internal("404")
    try:
        verify_download_permission()
        check("synthetic id + contract 404 → auth passed", True)
    except InternalDownloadUnavailable as exc:
        check("synthetic id + contract 404 → auth passed", False, str(exc))
    finally:
        fail_internal(None)

    # anything else on a synthetic probe fails closed (route-missing vs
    # permission-pass indistinguishable)
    for mode in ("500", "405"):
        fail_internal(mode)
        try:
            verify_download_permission()
            check(f"synthetic id + {mode} → FAIL (fail-closed)", False,
                  "probe unexpectedly passed")
        except InternalDownloadUnavailable as exc:
            check(f"synthetic id + {mode} → FAIL (capability_unknown)",
                  "gokapi_download_capability_unknown" in str(exc), str(exc))
        finally:
            fail_internal(None)

    # ── internal non-counting + counted recipient (needs a live file) ──
    payload = upload_to_gokapi(io.BytesIO(b"probe-seed-bytes"), "seed.txt",
                               "text/plain", 1, 5, len(b"probe-seed-bytes"))
    file_id = payload["FileInfo"]["Id"]

    # storage.read() is the internal non-counting primitive
    status, headers, stream = storage_read(file_id)
    body = stream.read()
    check("storage.read full read succeeds", status == 200 and body == b"probe-seed-bytes")
    check("storage.read keeps DownloadCount at 0", download_count(file_id) == 0)

    # public /downloadFile is counted
    status, _h, stream = __import__("gokapi").open_gokapi_download(file_id)
    stream.read()
    check("public /downloadFile counts (0 → 1)", download_count(file_id) == 1)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
