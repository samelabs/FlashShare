"""Scenario format families — the platform's user-facing format vocabulary.

范式: 场景定族、族定格式、声明即契约、执行即强制。
- 族 (FormatFamily) 按"用户处理文件的场景"划分, 与具体工具无关;
  工具上线下线, 族不动。
- 族从 ALLOWED_EXT (config) 派生: 平台全域边界与工具声明边界同源,
  扩展名集是平台准入边界的子集视图, 永不越界。
- 单一事实源: 后端执行闸门 / C2C 发布选择器 / 前端 chips 三方共用。
- 输入声明为**单选** (每工具一个族, 必选不可空);
  输出由端点决定, 平台只保证产物落在 ALLOWED_EXT 内, 不做声明。
"""

from __future__ import annotations

from config import ALLOWED_EXT, BLOCKED_EXT

assert ALLOWED_EXT.isdisjoint(BLOCKED_EXT), "ALLOWED_EXT ∩ BLOCKED_EXT must be ∅"


# ── 具体格式 ↔ MIME 词表 (4C-1: 精确执行契约的单一事实源) ──
# family = 分类/发现维度; accept = 精确执行契约。MIME → 具体扩展名集合,
# dispatch 格式闸门只认这里, 禁止把精确声明扩大成 family。
MIME_EXTS: dict[str, set[str]] = {
    # image
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
    "image/bmp": {".bmp"},
    "image/heic": {".heic"},
    "image/heif": {".heif"},
    "image/tiff": {".tiff", ".tif"},
    "image/avif": {".avif"},
    "image/x-icon": {".ico"},
    "image/vnd.microsoft.icon": {".ico"},
    "image/svg+xml": {".svg"},
    # document
    "application/pdf": {".pdf"},
    "text/plain": {".txt"},
    "text/csv": {".csv"},
    "text/markdown": {".md"},
    "application/rtf": {".rtf"},
    "application/msword": {".doc"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.ms-excel": {".xls"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
    "application/vnd.ms-powerpoint": {".ppt"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {".pptx"},
    "application/vnd.oasis.opendocument.text": {".odt"},
    "application/vnd.oasis.opendocument.spreadsheet": {".ods"},
    "application/vnd.oasis.opendocument.presentation": {".odp"},
    # archive
    "application/zip": {".zip"},
    "application/vnd.rar": {".rar"},
    "application/x-7z-compressed": {".7z"},
    "application/gzip": {".gz"},
    "application/x-tar": {".tar"},
    "application/x-bzip2": {".bz2"},
    "application/x-xz": {".xz"},
    # cad3d (application/octet-stream 是这些 ext 的泛型回退 MIME)
    "application/acad": {".dwg"},
    "image/vnd.dwg": {".dwg"},
    "image/x-dwg": {".dwg"},
    "model/step": {".step", ".stp"},
    "model/iges": {".iges", ".igs"},
    "model/stl": {".stl"},
    "model/obj": {".obj"},
    "model/gltf-binary": {".glb"},
    "model/gltf+json": {".gltf"},
    "application/octet-stream": {
        ".dxf", ".fbx", ".3ds", ".skp", ".blend", ".sldprt", ".iam", ".catpart"},
    # media
    "audio/mpeg": {".mp3"},
    "audio/wav": {".wav"},
    "audio/x-wav": {".wav"},
    "audio/flac": {".flac"},
    "audio/aac": {".aac"},
    "audio/ogg": {".ogg"},
    "audio/mp4": {".m4a"},
    "video/mp4": {".mp4"},
    "video/quicktime": {".mov"},
    "video/webm": {".webm"},
    "video/x-msvideo": {".avi"},
    "video/x-matroska": {".mkv"},
    # data
    "application/json": {".json"},
    "application/xml": {".xml"},
    "text/xml": {".xml"},
    "application/yaml": {".yaml"},
    "text/yaml": {".yaml"},
    # code
    "text/x-python": {".py"},
    "text/javascript": {".js"},
    "application/javascript": {".js"},
    "text/typescript": {".ts"},
    "text/html": {".html", ".htm"},
    "text/css": {".css"},
    "application/x-sql": {".sql"},
}

# 族特有附加扩展: 无专属 MIME (复合/历史别名), 仅存在于分类维度,
# 不属于任何 MIME 的精确映射 — family 派生时并入 exts, 不进入 MIME_EXTS。
FAMILY_EXTRA_EXTS: dict[str, set[str]] = {
    "archive": {".tgz"},
    "data": {".yml"},
}


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
# 4C-1: family 的 exts 从 MIME_EXTS 派生 (∪ 各 MIME 具体扩展) + FAMILY_EXTRA_EXTS,
# mimes 保持原声明集合 — 单一事实源在 MIME_EXTS, 族与精确契约不再各自漂移。
_FAMILY_DEFS: dict[str, tuple[set[str], str]] = {
    "image": (
        {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
         "image/heic", "image/heif", "image/tiff", "image/avif",
         "image/x-icon", "image/vnd.microsoft.icon", "image/svg+xml"},
        "Images",
    ),
    "document": (
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
    "archive": (
        {"application/zip", "application/vnd.rar", "application/x-7z-compressed",
         "application/gzip", "application/x-tar", "application/x-bzip2",
         "application/x-xz"},
        "Archives",
    ),
    "cad3d": (
        {"application/acad", "image/vnd.dwg", "image/x-dwg", "model/step",
         "model/iges", "model/stl", "model/obj", "model/gltf-binary",
         "model/gltf+json", "application/octet-stream"},
        "CAD & 3D",
    ),
    "media": (
        {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/flac",
         "audio/aac", "audio/ogg", "audio/mp4",
         "video/mp4", "video/quicktime", "video/webm", "video/x-msvideo",
         "video/x-matroska"},
        "Audio & Video",
    ),
    "data": (
        {"application/json", "application/xml", "text/xml",
         "application/yaml", "text/yaml"},
        "Data",
    ),
    "code": (
        {"text/x-python", "text/javascript", "application/javascript",
         "text/typescript", "text/html", "text/css", "application/x-sql"},
        "Code",
    ),
}

FAMILIES: dict[str, FormatFamily] = {
    key: FormatFamily(
        key,
        set().union(*(MIME_EXTS[m] for m in mimes)) | FAMILY_EXTRA_EXTS.get(key, set()),
        mimes,
        chip,
    )
    for key, (mimes, chip) in _FAMILY_DEFS.items()
}

FAMILY_ORDER = ["image", "document", "archive", "cad3d", "media", "data", "code"]

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
    """MIME 声明 → 族集合 (存量声明映射, 分类/发现维度)。未知 MIME → KeyError 上抛。

    mimes=None → None (accept=all 语义, 历史数据; 新发布为单选必填)。

    4C-1: 执行闸门不再使用本函数 — 精确契约用 extensions_for_mimes。
    本函数保留供分类/发现消费方使用 (family 是分类维度)。"""
    if not mimes:
        return None
    fams: set[str] = set()
    for m in mimes:
        f = MIME_TO_FAMILIES.get(m)
        if not f:
            raise KeyError(m)
        fams |= f
    return fams


def extensions_for_mimes(mimes: list[str] | None) -> set[str] | None:
    """MIME 声明 → 具体扩展名集合 (4C-1 精确执行契约)。

    - mimes=None/[] → None (不限制语义, 与 accept=None/[] 现有行为一致)
    - 已知 MIME → 对应具体扩展名 union (精确词表 MIME_EXTS)
    - 未知 MIME → KeyError 上抛 (存量脏数据; 调用方决定兼容策略:
      当前 dispatch 捕获后整体不拦截, 不产生 500)

    禁止把精确声明扩大成 family — application/pdf 只允许 .pdf,
    不会放行整个 document 族。
    """
    if not mimes:
        return None
    exts: set[str] = set()
    for m in mimes:
        e = MIME_EXTS.get(m)
        if not e:
            raise KeyError(m)
        exts |= e
    return exts


def validate_family_key(key) -> tuple[str | None, str | None]:
    """发布提交校验: 单个族键 (单选范式) → (family_key|None, err)。

    缺失/非法 (含旧客户端数组形态) → (None, 'bad_format_family') —
    单选必填, 无空语义。"""
    if not key or not isinstance(key, str) or key not in FAMILIES:
        return None, "bad_format_family"
    return key, None


def validate_family_keys(keys) -> tuple[set[str] | None, str | None]:
    """兼容入口 (旧多选调用方): 校验族键列表。新代码用 validate_family_key。"""
    if not keys:
        return None, "bad_format_family"  # 0930: 空声明已不可提交
    if not isinstance(keys, list) or len(keys) != 1 or keys[0] not in FAMILIES:
        return None, "bad_format_family"
    return {keys[0]}, None


def family_exts(key: str) -> frozenset[str]:
    return FAMILIES[key].exts


def sniff_ext_to_families(ext: str) -> set[str]:
    """扩展名 → 族集合 (执行闸门: 上传 sniff 后的 ext 反查)。未知 ext → 空集。"""
    return {k for k, f in FAMILIES.items() if ext in f.exts}


def mime_in_families(mime: str | None, ext: str, fam_keys: set[str]) -> bool:
    """执行闸门判定: sniff 产物 (final_type 或 ext) 是否落入声明族。

    族词表以 ext 为准 (MIME 服务端不可信, ext 已过魔数嗅探)。"""
    return bool(sniff_ext_to_families(ext) & fam_keys)
