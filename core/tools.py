"""Tool registry for FlashShare.

Each tool is a self-contained object. It declares what formats it accepts,
what parameters it declares, and a handler function that transforms input bytes
to output bytes. The handler never touches storage, database, or HTTP.

Tool lifecycle:
    CP1 Register  — tool declares its contract at startup
    CP2 List      — GET /api/tools returns declarations to frontend
    CP3 Select    — frontend binds accept to <input>, updates UI
    CP4 Upload    — existing security pipeline (whitelist + sniff + quota)
    CP5 Gate      — handle_execute_tool checks existence + auth + CSRF
    CP6 Execute   — orchestrator reads source from storage → calls handler
                    → writes result through share pipeline → returns link
    CP7 Output    — frontend renders share_link / image / text result
"""

from pathlib import Path


def out_filename(orig_name, ext=None, default_stem="file"):
    """The ONE output-name convention for tool-produced files: original
    stem + a short random suffix + extension. No tool word in the name
    (the page already says which tool ran) and no long strings — the
    filename is the user's display space. Composite outputs (zip/pdf)
    pass their semantic stem via default_stem."""
    import secrets as _secrets
    name = str(orig_name or "").strip()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = (stem or default_stem)[:64].strip() or default_stem
    if ext is None:
        ext = (name.rsplit(".", 1)[1].lower() if "." in name else "bin")
    return f"{stem}_{_secrets.token_hex(2)}.{ext}"


class ToolError(Exception):
    """Raised by a handler when processing fails with a user-facing error key.

    The error key should be an i18n string key (e.g. "err_format_not_supported").
    The orchestrator catches this and returns it to the frontend.
    """
    pass


# THE single pixel ceiling for image tools. Also set as Pillow's own
# DecompressionBomb limit so no code path can bypass it; the explicit
# width×height check below rejects oversized images BEFORE load() with a
# typed error instead of relying on Pillow's warning-to-error behavior.
MAX_IMAGE_PIXELS = 64_000_000


def open_image(stream):
    """Decode an image source with the platform pixel ceiling. seek(0) →
    Image.open → explicit width×height check → load(). Oversized images
    raise ToolError("err_image_too_large") — checked before load() so the
    typed rejection never depends on Pillow's bomb warnings; anything
    else Pillow cannot decode maps to err_format_not_supported. Returns
    the decoded Image."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    stream.seek(0)
    try:
        img = Image.open(stream)
    except Image.DecompressionBombError as exc:
        raise ToolError("err_image_too_large") from exc
    except Exception as exc:  # noqa: BLE001 — decode errors are typed, not 500s
        raise ToolError("err_format_not_supported") from exc
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise ToolError("err_image_too_large")
    try:
        img.load()
    except Image.DecompressionBombError as exc:
        raise ToolError("err_image_too_large") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError("err_format_not_supported") from exc
    return img


_registry = []


def register(tool_id, label, handler,
             accept=None, desc=None, files_min=1, files_max=1,
             outputs=("file",), params=None, tag=None,
             body_format=None, body_placeholder=None):
    """Register a tool.

    Args:
        tool_id: unique string identifier (lowercase + underscores).
        label: dict of {lang_code: display_name}. Must contain "en".
        handler: callable(tool_input: dict, params: dict) -> dict.
                 Receives source file stream + metadata, returns result.
                 Must NOT touch gokapi, database, or HTTP.
        accept: set/list of lowercase extension tokens or None, e.g.
                {".jpg", ".jpeg", ".png"}. Tokens must be canonical
                (governance.formats.VOCAB_EXTS). None means accept all
                whitelisted formats. The register() authoring adapter
                normalizes to a sorted canonical list before the
                ToolDeclaration validator (which is list-only).
        files_min / files_max: input file count bounds ([min, max];
               min may be 0 for text-only tools). max > 1 → the handler
               receives a "sources" array; exactly 1 → a single
               "stream"; 0 → "sources" is an empty list.
        outputs: item kinds this tool may produce ("text"/"json"/"file").
                 Handlers return {"items": [...]} (IO-1 result model).
        params: canonical parameter declaration (tool modes spec v3) — same
                shape as user tools' params_json. Validated with the SAME
                validator as user tools (governance.tool_params). This is the
                ONLY parameter declaration channel for first-party tools.
        tag: 工具类型声明 (仅第一方需要): 作者声明的应用场景 tag
                (九键之一, 见 governance.formats)。正文工具
                (files_max=0) 不声明。图标 = tag 查表, 无派生。
        body_format / body_placeholder: 正文工具输入方式 ('text'|
                'json') 与可选占位 (LocalizedText)。文件工具
                (files_max>0) 两者恒 None; 正文工具 body_format 必填,
                handler 收到 {"body": <取值>} 形态的 tool_input。
    """
    # local runtime binding 专属检查 (duplicate registration)
    if any(t["id"] == tool_id for t in _registry):
        raise ValueError(f"Tool already registered: {tool_id}")

    # 阶段 B-close: declaration 经 canonical validator 单点 (tool_supply),
    # register() 不再独立校验 params/accept/label contract — 只保留
    # runtime duplicate registration + handler binding。accept contract =
    # canonical lowercase extension tokens。
    from governance.tool_supply import validate_tool_declaration, DeclarationError
    decl = {
        "tool_key": tool_id,
        "tool_id": tool_id,
        "owner_id": None,
        "execution": "local",
        "label": label,
        "desc": desc,
        "inputs": {"files": {"min": files_min, "max": files_max,
                             "accept": sorted(accept) if accept else None}},
        "format_family": tag,
        "body_format": body_format,
        "body_placeholder": body_placeholder,
        "outputs": list(outputs),
        "params": params,
        "visibility": "public",
        "enabled": True,
    }
    try:
        decl = validate_tool_declaration(decl)
    except DeclarationError as exc:
        raise ValueError(f"invalid tool declaration: {exc}") from exc
    _registry.append({
        "id": tool_id,
        "label": decl["label"],
        "desc": decl["desc"],
        "tag": decl["format_family"],
        "body_format": decl["body_format"],
        "body_placeholder": decl["body_placeholder"],
        "handler": handler,
        "accept": set(decl["inputs"]["files"]["accept"])
        if decl["inputs"]["files"]["accept"] else None,
        "files_min": decl["inputs"]["files"]["min"],
        "files_max": decl["inputs"]["files"]["max"],
        "outputs": decl["outputs"],
        "params": decl["params"],
    })


def get_tool(tool_id: str):
    """Get a registered tool by id (full dict including handler), or None."""
    for tool in _registry:
        if tool["id"] == tool_id:
            return tool
    return None



_loaded = False


def load_all():
    """Import every tools_*.py module in this directory (sorted for determinism).

    Each module registers itself via register() at import time. Idempotent —
    calling twice is a no-op. This replaces the hand-maintained import lists
    that used to live in main.py / worker.py / pages.py (adding a tool = adding
    one file, nothing else to update).
    """
    global _loaded
    if _loaded:
        return
    import importlib
    import pkgutil

    for mod in sorted((m.name for m in pkgutil.iter_modules([str(Path(__file__).parent)])
                       if m.name.startswith("tools_"))):
        importlib.import_module(mod)
    _loaded = True
