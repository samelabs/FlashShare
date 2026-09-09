"""Tool registry for FlashShare.

Each tool is a self-contained object. It declares what formats it accepts,
what presets it offers, and a handler function that transforms input bytes
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
             accept=None, presets=None, multi=False, desc=None,
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
        accept: set of MIME types or None. Used by homepage to constrain
                <input accept>. None means accept all formats.
        presets: list of preset dicts or None. Each preset:
                 {"id": str, "label": {lang: name}, "default": bool}
                 Exactly one preset must have default=True if presets given.
        multi: bool. If True, this tool accepts multiple input files
               (e.g. PDF merge). Handler receives "sources" array.
               If False, handler receives a single "stream" (default).
        pass_through: bool. If True, selecting this tool uploads directly
               with no execute step (identity handler, e.g. share).
        params: canonical parameter declaration (tool modes spec v3) — same
               shape as user tools' params_json. Validated with the SAME
               validator as user tools (governance.tool_params). This is the
               ONLY parameter declaration channel for first-party tools;
               presets is legacy storage-compat only.
    """
    if tool_type not in (TYPE_VIEW, TYPE_PEEK, TYPE_PRODUCE):
        raise ValueError(f"Invalid tool type: {tool_type}")
    if icon not in VALID_ICONS:
        raise ValueError(f"Invalid icon: {icon}")
    if "en" not in label:
        raise ValueError("Label must have 'en' key")
    if any(t["id"] == tool_id for t in _registry):
        raise ValueError(f"Tool already registered: {tool_id}")
    if presets is not None:
        defaults = [p for p in presets if p.get("default")]
        if len(defaults) != 1:
            raise ValueError(f"presets must have exactly one default, got {len(defaults)}")

    if desc is not None and "en" not in desc:
        raise ValueError("desc must have 'en' key")
    # Canonical params: 与用户工具完全相同的声明 validator (批 4A 单一轨道)
    params_decl = []
    if params not in (None, [], ""):
        from governance.tool_params import validate_params_decl, ParamsDeclError
        try:
            params_decl = validate_params_decl(params)
        except ParamsDeclError as exc:
            raise ValueError(f"invalid params declaration: {exc}") from exc
    _registry.append({
        "id": tool_id,
        "type": tool_type,
        "label": label,
        "desc": desc,
        "icon": icon,
        "handler": handler,
        "accept": accept,
        "presets": presets,
        "multi": multi,
        "pass_through": bool(pass_through),
        "params": params_decl,
    })


def list_all_tools() -> list:
    """Public tool catalog — single source for web home, /api/v1, MCP.

    Pricing overlay happens HERE once: every channel gets the same
    catalog (cost included, disabled tools hidden).
    Strips handler/match and preset defaults (orchestrator's job).
    """
    from governance.pricing import list_pricing
    prices = {r["tool_id"]: r for r in list_pricing()}

    result = []
    for tool in _registry:
        p = prices.get(tool["id"]) or {"cost": 0, "enabled": True}
        if not p["enabled"]:
            continue
        item = {
            "id": tool["id"],
            "type": tool["type"],
            "label": tool["label"],
            "icon": tool["icon"],
            "accept": sorted(tool["accept"]) if tool["accept"] else None,
            "presets": None,
            "multi": tool.get("multi", False),
            "cost": int(p["cost"]),
        }
        if tool["presets"]:
            item["presets"] = [
                {"id": p["id"], "label": p["label"], "default": p.get("default", False)}
                for p in tool["presets"]
            ]
        result.append(item)
    return result


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
