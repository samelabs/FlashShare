#!/usr/bin/env python3
"""Remote ZIP listing (R10 rework): the central directory is fetched by
its EXACT absolute offset — no heuristic slack window, no cd_before_tail.

  Z1  a legal archive whose CD sits outside the tail window (large
      archive comment) lists correctly via the precise [cd_offset,
      cd_offset+cd_size) fetch
  Z2  a CD beyond MAX_CD_BYTES answers cd_too_large
  Z3  a CD window overrunning the EOCD's absolute position answers
      cd_overruns_eocd (corrupt)

Fail-closed: standard canonical env guard (the listing itself is pure
in-memory; no DB/app needed).
"""
import os
import struct
import sys

_TEST_PORT = os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); sys.exit(2)
try:
    if int(_TEST_PORT, 10) == 8000:
        sys.stderr.write("ERROR: production port not allowed.\n"); sys.exit(2)
except ValueError:
    sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); sys.exit(2)
TEST_DB = os.environ.get("TEST_DB_URL")
if not TEST_DB:
    sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); sys.exit(2)

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(_REPO, "core"))
from zip_preview import (CD_FIXED, CD_SIG, EOCD_FIXED, EOCD_SIG,
                         MAX_CD_BYTES, find_eocd, list_zip_remote)

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


def _cd_entry(name: str) -> bytes:
    raw = name.encode()
    return CD_FIXED.pack(0x02014b50, 20, 4, 0, 0, 0, 0, 0, 0, 0,
                         len(raw), 0, 0, 0, 0, 0, 0) + raw


def _assemble(entries_cd: bytes, comment: bytes, *, cd_offset=None,
              cd_size=None, count=0) -> bytes:
    """[local-data stub] + [CD] + [comment] + [EOCD]. cd_offset/cd_size
    default to the truthful layout; override to forge corruption."""
    body = b"PK\x03\x04" + b"\x00" * 64          # local-header stub
    cd = entries_cd
    off = cd_offset if cd_offset is not None else len(body)
    size = cd_size if cd_size is not None else len(cd)
    eocd = EOCD_FIXED.pack(0x06054b50, 0, 0, 0, count, size, off,
                           len(comment))
    return body + cd + comment + eocd


def _fake_fetch(blob: bytes):
    calls = []
    def fetch_range(start: int, length: int) -> bytes:
        calls.append((start, length))
        return blob[start:start + length]
    fetch_range.calls = calls
    return fetch_range


# ── Z1: CD outside the tail window (476,790-byte CD + 60,000 comment),
#    precise fetch lists every entry ──
_names = ["folder/" + "a" * 1130 + f"-{i}.png" for i in range(400)]
_pad = 476790 - (400 * 46 + sum(len(n) for n in _names))
_names[0] = "folder/" + "a" * (1130 + _pad) + "-0.png"
_cd = b"".join(_cd_entry(n) for n in _names)
check("Z1 fixture CD size is the required 476,790 bytes", len(_cd) == 476790,
      len(_cd))
blob = _assemble(_cd, b"\x00" * 60000, count=len(_names))
fetch = _fake_fetch(blob)
entries = list_zip_remote(fetch, len(blob))
_exact = fetch.calls
check("Z1 legal big-comment archive lists all entries",
      [e["name"] for e in entries] == _names, f"{len(entries)} entries")
check("Z1 the CD was fetched at its exact absolute offset, once",
      _exact[1:] == [(68, 476790)], str(fetch.calls))

# ── Z2: CD beyond the 512KiB cap → cd_too_large ──
_big_cd = b"".join(_cd_entry(n) for n in
                   ["folder/" + "b" * 1130 + f"-{i}.png" for i in range(800)])
blob2 = _assemble(_big_cd, b"", count=800)
try:
    list_zip_remote(_fake_fetch(blob2), len(blob2))
    check("Z2 oversized CD → cd_too_large", False, "no error raised")
except ValueError as exc:
    check("Z2 oversized CD → cd_too_large", str(exc) == "cd_too_large", str(exc))

# ── Z3: EOCD lying about the CD (window overruns the EOCD) → corrupt ──
blob3 = _assemble(_cd, b"", cd_offset=64, cd_size=len(_cd) + 500,
                  count=len(_names))
try:
    list_zip_remote(_fake_fetch(blob3), len(blob3))
    check("Z3 CD window overrunning the EOCD → cd_overruns_eocd",
          False, "no error raised")
except ValueError as exc:
    check("Z3 CD window overrunning the EOCD → cd_overruns_eocd",
          str(exc) == "cd_overruns_eocd", str(exc))

# ── Z3b: find_eocd returns the archive-absolute CD offset ──
tail_len = min(len(blob), MAX_CD_BYTES)
cs, co, _ = find_eocd(blob[len(blob) - tail_len:], len(blob) - tail_len)
check("Z3b find_eocd returns the absolute CD offset and true size",
      (cs, co) == (len(_cd), 68), f"size={cs} offset={co}")

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
sys.exit(1 if FAIL else 0)
