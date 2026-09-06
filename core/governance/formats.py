"""Scenario format families — the platform's user-facing format vocabulary.

范式 (0930 拍板): 场景定族、族定格式、声明即契约、执行即强制。
- 族 (FormatFamily) 按"用户处理文件的场景"划分, 与具体工具无关;
  工具上线下线, 族不动。
- 族从 ALLOWED_EXT (config) 派生: 平台全域边界与工具声明边界同源,
  扩展名集是平台准入边界的子集视图, 永不越界。
- 单一事实源: 后端执行闸门 / C2C 发布选择器 / 前端 chips 三方共用。
"""

from __future__ import annotations

from config import ALLOWED_EXT, BLOCKED_EXT

assert ALLOWED_EXT.isdisjoint(BLOCKED_EXT), "ALLOWED_EXT ∩ BLOCKED_EXT must be ∅"


class FormatFamily:
    """A scenario format family: user-readable name + ext/MIME sets + chip."""

    __slots__ = ("key", "exts", "mimes", "chip")

    def __init__(self, key: str, exts: set[str], mimes: set[str], chip: str):
        assert key and chip
        # 平台准入边界是硬天花板: 族不能声明平台拒绝的类型
        unknown = exts - ALLOWED_EXT
        assert not unknown, f"family '{key}' escapes ALLOWED_EXT: {sorted(unknown)}"
        self.key = key
        self.exts = frozenset(exts)
        self.mimes = frozenset(mimes)
        self.chip = chip


# ── 场景格式族 (用户格式语言, 平台词表) ──
# ext 集为 ALLOWED_EXT 的严格子集 (import 时断言)。
FAMILIES: dict[str, FormatFamily] = {
    fam.key: fam
    for fam in (
        FormatFamily(
            "image",
            {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
             ".tiff", ".tif", ".avif"},
            {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
             "image/heic", "image/heif", "image/tiff", "image/avif"},
            "Images",
        ),
        FormatFamily(
            "ico",
            {".ico"},
            {"image/x-icon", "image/vnd.microsoft.icon"},
            "ICO",
        ),
        FormatFamily(
            "document",
            {".pdf", ".txt", ".csv", ".md", ".rtf",
             ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
             ".odt", ".ods", ".odp"},
            {"application/pdf", "text/plain", "text/csv", "text/markdown",
             "application/rtf", "application/msword",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "application/vnd.ms-excel",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "application/vnd.ms-powerpoint",
             "application/vnd.openxmlformats-officedocument.presentationml.presentation",
             "application/vnd.oasis.opendocument.text",
             "application/vnd.oasis.opendocument.spreadsheet",
             "application/vnd.oasis.opendocument.presentation"},
            "Documents",
        ),
        FormatFamily(
            "archive",
            {".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz", ".tgz"},
            {"application/zip", "application/vnd.rar", "application/x-7z-compressed",
             "application/gzip", "application/x-tar", "application/x-bzip2",
             "application/x-xz"},
            "Archives",
        ),
        FormatFamily(
            "data",
            {".json", ".xml", ".yaml", ".yml"},
            {"application/json", "application/xml", "text/xml",
             "application/yaml", "text/yaml"},
            "Data",
        ),
        FormatFamily(
            "code",
            {".svg", ".py", ".js", ".mjs", ".ts", ".html", ".htm", ".css",
             ".go", ".rs", ".java", ".c", ".cpp", ".h", ".rb", ".php", ".sh", ".sql"},
            {"image/svg+xml", "text/x-python", "text/javascript",
             "application/javascript", "text/typescript", "text/html",
             "text/css", "text/x-go", "text/rust", "text/x-java",
             "text/x-c", "text/x-c++", "text/x-ruby", "application/x-httpd-php",
             "text/x-sh", "application/x-sql"},
            "Code",
        ),
    )
}

FAMILY_ORDER = ["image", "ico", "document", "archive", "data", "code"]

# chip 标签 (族内逐格式展示用): ext → 短名, 从派生族生成
CHIP_BY_EXT: dict[str, str] = {}
for _fam in FAMILIES.values():
    for _ext in sorted(_fam.exts):
        CHIP_BY_EXT[_ext] = _ext.lstrip(".").upper()

# MIME → 声明它的族集合 (执行闸门反向查找用)
MIME_TO_FAMILIES: dict[str, set[str]] = {}
for _key, _fam in FAMILIES.items():
    for _mime in _fam.mimes:
        MIME_TO_FAMILIES.setdefault(_mime, set()).add(_key)


def families_for_mimes(mimes: list[str] | None) -> set[str] | None:
    """MIME 声明 → 族集合 (作者存量真 MIME 自动映射)。未知 MIME → KeyError 上抛。

    mimes=None → None (accept=all 语义, 不参与闸门)。"""
    if not mimes:
        return None
    fams: set[str] = set()
    for m in mimes:
        f = MIME_TO_FAMILIES.get(m)
        if not f:
            raise KeyError(m)
        fams |= f
    return fams


def validate_family_keys(keys: list[str] | None) -> tuple[set[str] | None, str | None]:
    """发布提交校验: 族键列表 → (family_keys|None, err)。

    None/空 → (None, None) = 接受全部。非法族键 → (None, 'bad_format_family')。"""
    if not keys:
        return None, None
    if not isinstance(keys, list) or any(k not in FAMILIES for k in keys):
        return None, "bad_format_family"
    return set(keys), None


def sniff_ext_to_families(ext: str) -> set[str]:
    """扩展名 → 族集合 (执行闸门: 上传 sniff 后的 ext 反查)。未知 ext → 空集。"""
    return {k for k, f in FAMILIES.items() if ext in f.exts}


def mime_in_families(mime: str | None, ext: str, fam_keys: set[str]) -> bool:
    """执行闸门判定: sniff 产物 (final_type 或 ext) 是否落入声明族。

    族词表以 ext 为准 (MIME 服务端不可信, ext 已过魔数嗅探)。"""
    return bool(sniff_ext_to_families(ext) & fam_keys)
