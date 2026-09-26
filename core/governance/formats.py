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
    "application/gzip": {".gz", ".tgz"},
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
    "application/yaml": {".yaml", ".yml"},
    "text/yaml": {".yaml", ".yml"},
    # code
    "text/x-python": {".py"},
    "text/javascript": {".js"},
    "application/javascript": {".js"},
    "text/typescript": {".ts"},
    "text/html": {".html", ".htm"},
    "text/css": {".css"},
    "application/x-sql": {".sql"},
}

# 4C-1a: 不再有 FAMILY_EXTRA_EXTS — MIME_EXTS 完整表达当前 family 可发布/
# 可执行的扩展范围 (application/gzip 含 .tgz, application/yaml/text/yaml 含
# .yml 等合理别名), family 发布存 mimes → dispatch 精确执行 extensions_
# for_mimes(mimes) 必须放行族内全部格式, 不允许第二份隐藏扩展来源。


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
        set().union(*(MIME_EXTS[m] for m in mimes)),
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

# 平台 format vocabulary 全部具体扩展名并集 (canonical AcceptToken 边界)
VOCAB_EXTS: frozenset[str] = frozenset().union(
    *(_fam.exts for _fam in FAMILIES.values()))


def extensions_for_accept(tokens) -> set[str] | None:
    """Stored-accept → canonical extension set (Tool Input Contract 单点).

    Stored contract has exactly two shapes:
      - None → any platform-whitelisted format (explicit "any" semantics;
        used by current pass-through tools, e.g. share / zip_pack)
      - non-empty list of canonical lowercase extension tokens → set(ext)

    Everything else is INVALID — no second name for "any":
      - [] (empty list) → ValueError
      - bare string (not a list) → TypeError
      - MIME token / unknown token / empty token → KeyError(token)
    """
    if tokens is None:
        return None
    if not isinstance(tokens, list):
        raise TypeError(
            f"accept tokens must be a list, got {type(tokens).__name__}")
    token_list = tokens
    if not token_list:
        raise ValueError("accept=[] is invalid — use null for any-format")
    exts: set[str] = set()
    for tok in token_list:
        t = str(tok or "").strip().lower()
        if not t:
            raise ValueError("accept list contains an empty token")
        if t.startswith("."):
            if t not in VOCAB_EXTS:
                raise KeyError(t)
            exts.add(t)
        else:
            raise KeyError(t)
    if not exts:
        raise ValueError("accept list has no canonical tokens")
    return exts


def parse_stored_accept(raw: str | None) -> set[str] | None:
    """THE single stored-accept parser (platform_tools.accept_json column).

    Exactly two physical shapes are valid:
      SQL NULL           → None (explicit "any platform-whitelisted format")
      JSON text decoding to a NON-EMPTY list of canonical lowercase ext
                         → set(ext)

    Everything else is an invariant violation (typed raise):
      '' (empty string), 'null', '[]', a JSON object, a JSON scalar,
      MIME tokens, unknown ext tokens, empty tokens.
    """
    import json as _json
    if raw is None:
        return None
    try:
        decoded = _json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"stored accept is not valid JSON: {exc}") from None
    if not isinstance(decoded, list):
        raise ValueError(
            f"stored accept must decode to a list, got "
            f"{type(decoded).__name__}")
    return extensions_for_accept(decoded)


def validate_family_key(key) -> tuple[str | None, str | None]:
    """发布提交校验: 单个族键 (单选范式) → (family_key|None, err)。

    non-string / unknown family → (None, 'bad_format_family') —
    单选必填, 无空语义。"""
    if not key or not isinstance(key, str) or key not in FAMILIES:
        return None, "bad_format_family"
    return key, None


def format_manifest() -> dict:
    """客户端 format manifest (4C-2a): 发布页唯一格式事实源。

    直接从 FAMILY_ORDER / FAMILIES 派生 (排序稳定), 客户端禁止再维护
    第二份 ext/MIME 镜像。只含公开格式声明数据, 不含任何平台内部字段。
    """
    return {
        "order": list(FAMILY_ORDER),
        "families": {
            key: {"exts": sorted(FAMILIES[key].exts)}
            for key in FAMILY_ORDER
        },
    }


def sniff_ext_to_families(ext: str) -> set[str]:
    """扩展名 → 族集合 (执行闸门: 上传 sniff 后的 ext 反查)。未知 ext → 空集。"""
    return {k for k, f in FAMILIES.items() if ext in f.exts}


# 4C-1a 决定性 invariant: family 声明的 MIME 集合按 MIME_EXTS 展开, 必须
# 恰等于该族 exts — 七个 family 全部成立。未来增改 family/MIME 词表违反此
# invariant 即加载失败, 不允许 family 声称支持但精确执行闸门拒绝的格式。
for _key, _fam in FAMILIES.items():
    _mime_union: set[str] = set()
    for _m in _fam.mimes:
        _mime_union |= set(MIME_EXTS[_m])
    assert _mime_union == set(_fam.exts), (
        f"family '{_key}' MIME→ext 分叉: "
        f"MIME_EXTS 展开={sorted(_mime_union)} "
        f"!= exts={sorted(_fam.exts)}"
    )
