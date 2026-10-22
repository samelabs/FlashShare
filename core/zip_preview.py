"""ZIP preview — list archive contents without downloading the whole file.

Two data paths, one parser:
  1. Remote (result-end): fetch only the tail (EOCD + central directory)
     from gokapi via HTTP Range requests (206), parse, return JSON list.
  2. Local bytes: same parser over a fully-buffered archive (used by tests).

Central directory file header = 46-byte fixed part (incl. 4-byte signature
0x02014b50) + name + extra + comment. Layout matches cpython zipfile:
    <I  sig><H vmade><H vneed><H flags><H method><H mtime><H mdate>
    <I crc><I csize><I usize><H nlen><H elen><H clen><H disk>
    <H iattr><I eattr><I lho>
EOCD = 22 bytes: <I sig><H disk><H cddisk><H disk_entries><H total_entries>
                  <I cd_size><I cd_offset><H clen>
"""

import struct

EOCD_SIG = b"PK\x05\x06"
CD_SIG = b"PK\x01\x02"
CD_FIXED = struct.Struct("<IHHHHHHIIIHHHHHII")   # 46 bytes, 17 fields
EOCD_FIXED = struct.Struct("<IHHHHIIH")           # 22 bytes, 8 fields
MAX_CD_BYTES = 512 * 1024          # cap central-directory fetch at 512KB
MAX_ENTRIES = 500                  # cap rendered entries
ENCRYPTED_FLAG = 0x1
UTF8_FLAG = 0x800


def parse_zip_central_directory(data: bytes, cd_size: int, cd_offset: int) -> list:
    """Parse CD entries from a buffer where the CD starts at cd_offset.

    Returns list of dicts: name, size, compressed, is_dir, encrypted.
    Raises ValueError("bad_cd_signature" | "cd_truncated") on malformed data.
    """
    entries = []
    pos = cd_offset
    end = cd_offset + cd_size
    while pos + 46 <= end and len(entries) < MAX_ENTRIES:
        if data[pos:pos + 4] != CD_SIG:
            raise ValueError("bad_cd_signature")
        try:
            fields = CD_FIXED.unpack_from(data, pos)
        except struct.error as exc:
            raise ValueError("cd_truncated") from exc
        flags = fields[3]
        csize, usize = fields[8], fields[9]
        nlen, elen, clen = fields[10], fields[11], fields[12]
        raw = data[pos + 46: pos + 46 + nlen]
        if len(raw) != nlen:
            raise ValueError("cd_truncated")
        if nlen and (flags & UTF8_FLAG):
            name = raw.decode("utf-8", "replace")
        elif nlen:
            name = raw.decode("cp437", "replace")
        else:
            name = ""
        entries.append({
            "name": name,
            "size": usize,
            "compressed": csize,
            "is_dir": name.endswith("/") or name.endswith("\\"),
            "encrypted": bool(flags & ENCRYPTED_FLAG),
        })
        pos += 46 + nlen + elen + clen
    return entries


def find_eocd(tail: bytes, tail_start: int) -> tuple:
    """Locate EOCD in a tail buffer; return (cd_size, cd_offset, eocd_pos).

    cd_offset is the CENTRAL DIRECTORY's archive-absolute offset straight
    from the EOCD field — the caller fetches exactly
    [cd_offset, cd_offset + cd_size). eocd_pos stays tail-relative.
    Raises ValueError on absence, truncation, or ZIP64 markers
    (unsupported).
    """
    idx = tail.rfind(EOCD_SIG)
    if idx < 0:
        raise ValueError("no_eocd")
    if len(tail) - idx < 22:
        raise ValueError("eocd_truncated")
    (_sig, _disk, _cddisk, _disk_entries,
     total_entries, cd_size, cd_offset, _clen) = EOCD_FIXED.unpack_from(tail, idx)
    if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF or total_entries == 0xFFFF:
        raise ValueError("zip64_unsupported")
    if cd_size == 0 or total_entries == 0:
        raise ValueError("empty_archive")  #  合法空档, 调用方(proxy)转空列表
    return cd_size, cd_offset, idx


def list_zip_remote(fetch_range, total_size: int) -> list:
    """List a remote zip via Range fetches.

    fetch_range(start, length) -> bytes (must honor exact ranges).
    Fetch the last MAX_CD_BYTES (or the whole file if smaller) and locate
    the EOCD; when the central directory isn't fully inside that tail
    window, pull EXACTLY [cd_offset, cd_offset + cd_size) — no heuristic
    slack window (a big archive comment or a far-out CD used to force
    cd_before_tail / an oversized refetch). cd_offset + cd_size must not
    overrun the EOCD's absolute position — anything else is corruption.
    """
    if total_size <= 0:
        raise ValueError("empty_file")
    tail_len = min(total_size, MAX_CD_BYTES)
    tail_start = total_size - tail_len
    tail = fetch_range(tail_start, tail_len)
    if len(tail) != tail_len:
        raise ValueError("range_short_read")
    cd_size, cd_offset, eocd_in_tail = find_eocd(tail, tail_start)
    if cd_offset + cd_size > tail_start + eocd_in_tail:
        raise ValueError("cd_overruns_eocd")
    if cd_offset >= tail_start:
        return parse_zip_central_directory(tail, cd_size, cd_offset - tail_start)
    if cd_size > MAX_CD_BYTES:
        raise ValueError("cd_too_large")
    cd = fetch_range(cd_offset, cd_size)
    if len(cd) != cd_size:
        raise ValueError("range_short_read")
    return parse_zip_central_directory(cd, cd_size, 0)
