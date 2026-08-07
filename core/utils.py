import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import time
import urllib.parse

from config import BASE_URL, SESSION_SECRET


def now() -> int:
    return int(time.time())


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def json_bytes(data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return status, {"Content-Type": "application/json; charset=utf-8"}, body


def text_response(text: str, status=200, content_type="text/html; charset=utf-8"):
    return status, {"Content-Type": content_type}, text.encode("utf-8")


def binary_response(body: bytes, content_type: str, status=200):
    return status, {"Content-Type": content_type, "Cache-Control": "public, max-age=86400"}, body


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


def generate_public_id(con) -> str:
    for _ in range(20):
        public_id = b64url(secrets.token_bytes(9))
        exists = con.execute("SELECT 1 FROM uploads WHERE public_id = ?", (public_id,)).fetchone()
        if not exists:
            return public_id
    raise RuntimeError("public_id_generation_failed")


def public_file_url(public_id: str) -> str:
    return f"{BASE_URL}/f/{urllib.parse.quote(public_id)}"


def parse_choice(value: str, allowed: set, default: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed in allowed else default


def html_escape(value: str) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


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


def format_share_date(ts) -> str:
    if not ts:
        return "-"
    value = time.gmtime(int(ts))
    return f"{value.tm_mday}/{value.tm_mon}/{value.tm_year}"


def compact_filename(name: str, limit: int = 10) -> str:
    clean = os.path.basename(name or "file").strip() or "file"
    if len(clean) <= limit:
        return clean
    stem, ext = os.path.splitext(clean)
    if ext and len(ext) < limit:
        return f"{stem[:max(1, limit - len(ext))]}{ext}"
    return clean[:limit]


def file_ext_label(name: str) -> str:
    ext = os.path.splitext(name or "")[1].strip(".").upper()
    return ext[:4] or "FILE"


# ── SVG icon functions ──
# All delegate to the unified icon registry (icons.py / Heroicons v2).
# These wrappers exist for backward compatibility with existing call sites.

from icons import icon_svg as _icon_svg_wrapper


def qr_icon_svg() -> str:
    return _icon_svg_wrapper("qr")


def download_icon_svg() -> str:
    return _icon_svg_wrapper("download")


def upload_icon_svg() -> str:
    return _icon_svg_wrapper("upload")


def refresh_icon_svg() -> str:
    return _icon_svg_wrapper("refresh")


def close_icon_svg() -> str:
    return _icon_svg_wrapper("close")


def brand_icon_svg() -> str:
    return _icon_svg_wrapper("brand")


def file_size_icon_svg() -> str:
    return _icon_svg_wrapper("file_size")


def upload_count_icon_svg() -> str:
    return _icon_svg_wrapper("upload_count")


def bandwidth_icon_svg() -> str:
    return _icon_svg_wrapper("bandwidth")


def expiry_icon_svg() -> str:
    return _icon_svg_wrapper("expiry")


def download_count_icon_svg() -> str:
    return _icon_svg_wrapper("download_count")


def history_icon_svg() -> str:
    return _icon_svg_wrapper("history")


def link_icon_svg() -> str:
    return _icon_svg_wrapper("link")


def check_icon_svg() -> str:
    return _icon_svg_wrapper("check")


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
    ext = os.path.splitext(name or "")[1].lower()
    cat_key = EXT_TO_CATEGORY.get(ext, "")
    if cat_key and cat_key in FORMAT_ICONS:
        css_class, label, svg_paths = FORMAT_ICONS[cat_key]
        return ('<div class="file-mark ' + css_class + '" aria-hidden="true">'
                '<svg viewBox="0 0 24 24">' + svg_paths + '</svg>'
                '<span>' + label + '</span></div>')
    ext_upper = ext.strip(".").upper()[:4] or "FILE"
    return ('<div class="file-mark fmt-default" aria-hidden="true">'
            '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><path d="M14 2v6h6"></path></svg>'
            '<span>' + ext_upper + '</span></div>')


# ── QR code generation (pure Python, no dependencies) ──

def qr_svg(value: str) -> str:
    codewords = _qr_codewords(value.encode("utf-8"))
    size = 33
    modules = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_module(x, y, dark, reserve=True):
        if 0 <= x < size and 0 <= y < size:
            modules[y][x] = bool(dark)
            if reserve:
                reserved[y][x] = True

    def finder(x, y):
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                xx, yy = x + dx, y + dy
                if 0 <= xx < size and 0 <= yy < size:
                    dark = 0 <= dx <= 6 and 0 <= dy <= 6 and (
                        dx in {0, 6} or dy in {0, 6} or (2 <= dx <= 4 and 2 <= dy <= 4)
                    )
                    set_module(xx, yy, dark)

    finder(0, 0)
    finder(size - 7, 0)
    finder(0, size - 7)
    for i in range(8, size - 8):
        set_module(i, 6, i % 2 == 0)
        set_module(6, i, i % 2 == 0)
    for cy in [26]:
        for cx in [26]:
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_module(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    set_module(8, 25, True)
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    for i in range(8):
        reserved[8][size - 8 + i] = True
        reserved[size - 8 + i][8] = True

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
                    set_module(xx, y, dark, reserve=False)
                    bit_index += 1
            y += direction
        direction = -direction
        y += direction
        x -= 2

    format_bits = _qr_format_bits(1, 0)
    def bit(i):
        return ((format_bits >> i) & 1) == 1
    for i in range(6):
        set_module(8, i, bit(i))
    set_module(8, 7, bit(6))
    set_module(8, 8, bit(7))
    set_module(7, 8, bit(8))
    for i in range(9, 15):
        set_module(14 - i, 8, bit(i))
    for i in range(8):
        set_module(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        set_module(8, size - 15 + i, bit(i))
    set_module(8, size - 8, True)

    scale = 8
    pad = 4
    rects = []
    for y, row in enumerate(modules):
        for x, dark in enumerate(row):
            if dark:
                rects.append(f'<rect x="{(x + pad) * scale}" y="{(y + pad) * scale}" width="{scale}" height="{scale}"/>')
    total = (size + pad * 2) * scale
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" width="{total}" height="{total}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#ffffff"/><g fill="#0f1419">{"".join(rects)}</g></svg>'


def _qr_codewords(data: bytes) -> list:
    if len(data) > 78:
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


def _qr_gf_pow(power: int) -> int:
    value = 1
    for _ in range(power):
        value = _qr_gf_mul(value, 2)
    return value


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
