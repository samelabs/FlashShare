from __future__ import annotations

import base64
import os
import secrets
import time
import urllib.parse

from config import BASE_URL


def now() -> int:
    return int(time.time())


# List paging bounds: past the last page a list is simply empty, so a
# page/offset beyond these never needs to reach SQL (keeps OFFSET inside
# bigint and the scan finite).
MAX_LIST_PAGE = 10_000
MAX_LIST_OFFSET = 1_000_000


def page_number(raw) -> int:
    """1-based page from a request value: non-integers → 1, clamped to
    [1, MAX_LIST_PAGE]."""
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return 1
    return min(max(page, 1), MAX_LIST_PAGE)


def list_offset(raw) -> int:
    """Row offset from a request value: non-integers and negatives → 0,
    clamped to MAX_LIST_OFFSET."""
    try:
        offset = int(raw)
    except (TypeError, ValueError):
        return 0
    return min(max(offset, 0), MAX_LIST_OFFSET)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name or "upload.bin").strip().replace("\x00", "")
    keep = []
    for ch in name:
        if ch.isalnum() or ch in "._- ()[]":
            keep.append(ch)
        else:
            keep.append("_")
    clean = "".join(keep).strip(" .")
    return clean[:120] or "upload.bin"


def content_disposition(disposition: str, name: str, fallback: str) -> str:
    """Content-Disposition header value carrying a Unicode-safe filename:
    backslash and quote neutralized, ASCII fallback (or `fallback` when the
    name has no ASCII left) plus the RFC 5987 filename* form — one formula
    for every serving surface (proxy download/preview, /v1 file items)."""
    safe = str(name).replace("\\", "_").replace('"', "_")
    ascii_name = safe.encode("ascii", "ignore").decode("ascii") or fallback
    return (f'{disposition}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{urllib.parse.quote(safe)}")


def generate_public_id(con) -> str:
    from repo.uploads import public_id_exists
    for _ in range(20):
        public_id = b64url(secrets.token_bytes(9))
        if not public_id_exists(con, public_id):
            return public_id
    raise RuntimeError("public_id_generation_failed")


def public_file_url(public_id: str) -> str:
    return f"{BASE_URL}/f/{urllib.parse.quote(public_id)}"


def public_result_url(result_id: str) -> str:
    """Result page link (/r/{id}) — the single builder for stored
    result records, the /r page share surface and its QR."""
    return f"{BASE_URL}/r/{urllib.parse.quote(result_id)}"


def html_escape(value: str) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


def file_urls(public_id: str) -> tuple[str, str]:
    """THE single file-address builder: returns (page_url, download_url)
    for one /f/{id} resource. Every surface (result record, /r page, MCP
    structuredContent, web card) derives both from here."""
    page = f"{BASE_URL}/f/{urllib.parse.quote(public_id)}"
    return page, f"{page}?download=1"


def remaining_downloads(total: int, live: dict | None) -> int | None:
    """THE single remaining-download calculator over a live storage-info
    dict: prefers the provider's DownloadsRemaining, falls back to
    total - DownloadCount, None when neither is known."""
    if not isinstance(live, dict):
        return None
    rem = live.get("DownloadsRemaining")
    if isinstance(rem, int):
        return max(0, rem)
    cnt = live.get("DownloadCount")
    if isinstance(cnt, int):
        return max(0, int(total) - cnt)
    return None


def mime_base(content_type: str) -> str:
    """Lowercase base media type with parameters stripped.

    Stored MIME strings are client-declared and may carry '; param'
    suffixes; full-string equality against a bare type then misses and
    the same upload is classified differently at upload vs preview.
    Every admission/classification comparison uses this base form."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def file_glyph(content_type: str, filename: str) -> str:
    """File-row glyph icon name by type family (one mapping; the result
    page and the file page share it)."""
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    if ct.startswith("image/"):
        return "image"
    if ct == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if ct in ("application/zip", "application/x-zip-compressed") \
            or name.endswith(".zip"):
        return "archive"
    if "sheet" in ct or "excel" in ct or "csv" in ct:
        return "sheet"
    if "presentation" in ct or "powerpoint" in ct:
        return "slides"
    if "word" in ct or "document" in ct:
        return "doc"
    if "text" in ct:
        return "text"
    return "doc"


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size or 0)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {units[index]}" if index else f"{int(value)} {units[index]}"


def format_ts(ts) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ts)))


# ── Format category icons ──
# SVG paths sourced from icons.py (single source of truth)
# Format: category → (css_class, label, svg_paths)
from icons import _ICONS as _FMT_IC

FORMAT_ICONS = {
    "image":  ("fmt-image",  "IMG", _FMT_IC.get("image", "")),
    "pdf":    ("fmt-pdf",    "PDF", _FMT_IC.get("pdf", "")),
    "doc":    ("fmt-doc",    "DOC", _FMT_IC.get("doc", "")),
    "sheet":  ("fmt-sheet",  "XLS", _FMT_IC.get("sheet", "")),
    "slides": ("fmt-slides", "PPT", _FMT_IC.get("slides", "")),
    "text":   ("fmt-text",   "TXT", _FMT_IC.get("text", "")),
    "archive":("fmt-arch",   "ZIP", _FMT_IC.get("archive", "")),
}

EXT_TO_CATEGORY = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".webp": "image",
    ".bmp": "image", ".ico": "image", ".heic": "image", ".heif": "image",
    ".tiff": "image", ".tif": "image", ".avif": "image", ".svg": "image",
    ".pdf": "pdf",
    ".doc": "doc", ".docx": "doc", ".rtf": "doc", ".odt": "doc",
    ".xls": "sheet", ".xlsx": "sheet", ".ods": "sheet",
    ".ppt": "slides", ".pptx": "slides", ".odp": "slides",
    ".txt": "text", ".csv": "text", ".md": "text",
    ".json": "text", ".xml": "text", ".yaml": "text", ".yml": "text",
    ".py": "text", ".js": "text", ".ts": "text", ".html": "text", ".htm": "text", ".css": "text",
    ".go": "text", ".rs": "text", ".java": "text", ".c": "text", ".cpp": "text", ".h": "text",
    ".rb": "text", ".php": "text", ".sh": "text", ".sql": "text",
    ".zip": "archive", ".rar": "archive", ".7z": "archive", ".gz": "archive",
    ".tar": "archive", ".bz2": "archive", ".xz": "archive", ".tgz": "archive",
}


def file_format_icon(name: str) -> str:
    """Platform-owned SVG+label markup (Markup on return): crosses the
    autoescape boundary as the single file-mark producer."""
    from markupsafe import Markup
    ext = os.path.splitext(name or "")[1].lower()
    cat_key = EXT_TO_CATEGORY.get(ext, "")
    if cat_key and cat_key in FORMAT_ICONS:
        css_class, label, svg_paths = FORMAT_ICONS[cat_key]
        return Markup('<div class="file-mark ' + css_class + '" aria-hidden="true">'
                      '<svg viewBox="0 0 24 24">' + svg_paths + '</svg>'
                      '<span>' + label + '</span></div>')
    ext_upper = ext.strip(".").upper()[:4] or "FILE"
    return Markup('<div class="file-mark fmt-default" aria-hidden="true">'
            '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><path d="M14 2v6h6"></path></svg>'
            '<span>' + ext_upper + '</span></div>')


# ── QR code generation (pure Python, no dependencies) ──


def _qr_function_modules(size: int) -> tuple:
    """Phase-1 pass of qr_svg: draws every function pattern (three
    finders + separators, timing lines, alignment pattern, dark module)
    and returns (modules, reserved). `reserved` marks each module a data
    bit must never occupy — for V4 (33×33) the complement is exactly the
    807-module data region (100 codewords × 8 bits + the 7 trailing
    remainder bits). The second copy of the format strips spans 8 modules
    each (row 8 cols size-8..size-1, col 8 rows size-8..size-1); the 9th
    cell next to the top-left finder belongs to copy 1 only — reserving
    it for copy 2 (the historical range(9)) shrank the data region to 805
    and desynchronized the whole data placement."""
    modules = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_module(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            modules[y][x] = bool(dark)
            reserved[y][x] = True

    def finder(x, y):
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                dark = 0 <= dx <= 6 and 0 <= dy <= 6 and (
                    dx in {0, 6} or dy in {0, 6} or (2 <= dx <= 4 and 2 <= dy <= 4)
                )
                set_module(x + dx, y + dy, dark)

    finder(0, 0)
    finder(size - 7, 0)
    finder(0, size - 7)
    for i in range(8, size - 8):
        set_module(i, 6, i % 2 == 0)
        set_module(6, i, i % 2 == 0)
    # V4 single alignment pattern, centre (26, 26)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            set_module(26 + dx, 26 + dy, max(abs(dx), abs(dy)) != 1)
    set_module(8, size - 8, True)  # dark module
    for i in range(9):  # format copy 1: row 8 / col 8 beside the top-left finder
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):  # format copy 2: 8 modules beside each far finder
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    return modules, reserved


def qr_svg(value: str) -> str:
    codewords = _qr_codewords(value.encode("utf-8"))
    size = 33
    modules, reserved = _qr_function_modules(size)

    def place(x, y, dark):
        modules[y][x] = bool(dark)

    bits = []
    for b in codewords:
        bits.extend(((b >> i) & 1) == 1 for i in range(7, -1, -1))
    bits.extend([False] * 7)
    bit_index = 0
    direction = -1
    x = size - 1
    y = size - 1
    while x > 0:
        if x == 6:
            x -= 1
        while 0 <= y < size:
            for xx in [x, x - 1]:
                if not reserved[y][xx]:
                    dark = bits[bit_index] if bit_index < len(bits) else False
                    if (xx + y) % 2 == 0:
                        dark = not dark
                    place(xx, y, dark)
                    bit_index += 1
            y += direction
        direction = -direction
        y += direction
        x -= 2

    format_bits = _qr_format_bits(1, 0)
    def bit(i):
        return ((format_bits >> i) & 1) == 1
    for i in range(6):
        place(8, i, bit(i))
    place(8, 7, bit(6))
    place(8, 8, bit(7))
    place(7, 8, bit(8))
    for i in range(9, 15):
        place(14 - i, 8, bit(i))
    for i in range(8):
        place(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        place(8, size - 15 + i, bit(i))
    place(8, size - 8, True)

    scale = 8
    pad = 4
    rects = []
    for y, row in enumerate(modules):
        for x, dark in enumerate(row):
            if dark:
                rects.append(f'<rect x="{(x + pad) * scale}" y="{(y + pad) * scale}" width="{scale}" height="{scale}"/>')
    total = (size + pad * 2) * scale
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" width="{total}" height="{total}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#ffffff"/><g fill="#0f1419">{"".join(rects)}</g></svg>'


# Fixed V4-L encoder capacity in payload bytes. Endpoints minting QRs
# must reject longer payloads BEFORE calling qr_svg (qr_svg raises).
QR_MAX_PAYLOAD = 78


def _qr_codewords(data: bytes) -> list:
    if len(data) > QR_MAX_PAYLOAD:
        raise ValueError("QR payload too long")
    bits = [0, 1, 0, 0]
    bits.extend(((len(data) >> i) & 1) for i in range(7, -1, -1))
    for b in data:
        bits.extend(((b >> i) & 1) for i in range(7, -1, -1))
    bits.extend([0] * min(4, 640 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = []
    for i in range(0, len(bits), 8):
        value = 0
        for bit in bits[i:i + 8]:
            value = (value << 1) | bit
        codewords.append(value)
    for pad in [0xEC, 0x11] * 40:
        if len(codewords) >= 80:
            break
        codewords.append(pad)
    return codewords + _qr_rs_remainder(codewords, 20)


def _qr_gf_mul(x: int, y: int) -> int:
    result = 0
    while y:
        if y & 1:
            result ^= x
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
        y >>= 1
    return result


def _qr_rs_remainder(data: list, degree: int) -> list:
    generator = [0] * degree
    generator[-1] = 1
    root = 1
    for i in range(degree):
        for j in range(degree):
            generator[j] = _qr_gf_mul(generator[j], root)
            if j + 1 < degree:
                generator[j] ^= generator[j + 1]
        root = _qr_gf_mul(root, 2)
    result = [0] * degree
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(generator):
            result[i] ^= _qr_gf_mul(coef, factor)
    return result


def _qr_format_bits(ecc_level: int, mask: int) -> int:
    data = (ecc_level << 3) | mask
    bits = data << 10
    generator = 0x537
    for i in range(14, 9, -1):
        if (bits >> i) & 1:
            bits ^= generator << (i - 10)
    return ((data << 10) | bits) ^ 0x5412
