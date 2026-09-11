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

# Tool type constants
TYPE_VIEW = "view"
TYPE_PEEK = "peek"
TYPE_PRODUCE = "produce"

# Icon identifiers (matched in frontend SVG sprite)
VALID_ICONS = {
    "shield", "image", "archive", "edit", "crop",
    "compress", "convert", "print", "favicon", "merge", "info", "hash", "upload", "wand",
}


class ToolError(Exception):
    """Raised by a handler when processing fails with a user-facing error key.

    The error key should be an i18n string key (e.g. "err_format_not_supported").
    The orchestrator catches this and returns it to the frontend.
    """
    pass


_registry = []


def register(tool_id, tool_type, label, icon, handler,
             accept=None, multi=False, desc=None,
             pass_through=False, params=None):
    """Register a tool.

    Args:
        tool_id: unique string identifier (lowercase + underscores).
        tool_type: TYPE_VIEW, TYPE_PEEK, or TYPE_PRODUCE.
        label: dict of {lang_code: display_name}. Must contain "en".
        icon: icon identifier from VALID_ICONS.
        handler: callable(tool_input: dict, params: dict) -> dict.
                 Receives source file stream + metadata, returns result.
                 Must NOT touch gokapi, database, or HTTP.
        accept: set/list of lowercase extension tokens or None, e.g.
                {".jpg", ".jpeg", ".png"}. Tokens must be canonical
                (governance.formats.VOCAB_EXTS). None means accept all
                whitelisted formats.
        multi: bool. If True, this tool accepts multiple input files
               (e.g. PDF merge). Handler receives "sources" array.
               If False, handler receives a single "stream" (default).
        pass_through: bool. If True, selecting this tool uploads directly
               with no execute step (identity handler, e.g. share).
        params: canonical parameter declaration (tool modes spec v3) — same
               shape as user tools' params_json. Validated with the SAME
               validator as user tools (governance.tool_params). This is the
               ONLY parameter declaration channel for first-party tools.
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
        "tool_type": tool_type,
        "label": label,
        "desc": desc,
        "icon": icon,
        "accept": sorted(accept) if accept else None,
        "multi": multi,
        "params": params,
        "pass_through": pass_through,
        "visibility": "public",
        "enabled": True,
    }
    try:
        decl = validate_tool_declaration(decl)
    except DeclarationError as exc:
        raise ValueError(f"invalid tool declaration: {exc}") from exc
    _registry.append({
        "id": tool_id,
        "type": tool_type,
        "label": decl["label"],
        "desc": decl["desc"],
        "icon": icon,
        "handler": handler,
        "accept": set(decl["accept"]) if decl["accept"] else None,
        "multi": multi,
        "pass_through": bool(pass_through),
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
