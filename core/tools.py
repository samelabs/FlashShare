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

# Tool type constants
TYPE_VIEW = "view"
TYPE_PEEK = "peek"
TYPE_PRODUCE = "produce"

# Icon identifiers (matched in frontend SVG sprite)
VALID_ICONS = {
    "shield", "image", "archive", "edit", "crop",
    "compress", "convert", "favicon", "merge", "info", "hash", "upload", "wand",
}


class ToolError(Exception):
    """Raised by a handler when processing fails with a user-facing error key.

    The error key should be an i18n string key (e.g. "err_format_not_supported").
    The orchestrator catches this and returns it to the frontend.
    """
    pass


_registry = []


def register(tool_id, tool_type, label, icon, match, handler,
             accept=None, presets=None, multi=False):
    """Register a tool.

    Args:
        tool_id: unique string identifier (lowercase + underscores).
        tool_type: TYPE_VIEW, TYPE_PEEK, or TYPE_PRODUCE.
        label: dict of {lang_code: display_name}. Must contain "en".
        icon: icon identifier from VALID_ICONS.
        match: callable(upload: dict) -> bool. Used by detail page to
               filter tools for a given file.
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

    _registry.append({
        "id": tool_id,
        "type": tool_type,
        "label": label,
        "icon": icon,
        "match": match,
        "handler": handler,
        "accept": accept,
        "presets": presets,
        "multi": multi,
    })


def list_all_tools():
    """Return all registered tools' declaration info for the homepage.

    Strips handler and match (not needed by frontend).
    Strips 'default' from presets (orchestrator handles defaults).
    """
    result = []
    for tool in _registry:
        item = {
            "id": tool["id"],
            "type": tool["type"],
            "label": tool["label"],
            "icon": tool["icon"],
            "accept": sorted(tool["accept"]) if tool["accept"] else None,
            "presets": None,
            "multi": tool.get("multi", False),
        }
        if tool["presets"]:
            item["presets"] = [
                {"id": p["id"], "label": p["label"], "default": p.get("default", False)}
                for p in tool["presets"]
            ]
        result.append(item)
    return result


def get_tools_for_upload(upload: dict) -> list:
    """Return tools whose match() returns True for this upload.

    Used by the detail page (GET /api/tools/{public_id}).
    Strips handler and match from the output.
    """
    result = []
    for tool in _registry:
        try:
            if tool["match"](upload):
                item = {
                    "id": tool["id"],
                    "type": tool["type"],
                    "label": tool["label"],
                    "icon": tool["icon"],
                    "accept": sorted(tool["accept"]) if tool["accept"] else None,
                    "presets": None,
                }
                if tool["presets"]:
                    item["presets"] = [
                        {"id": p["id"], "label": p["label"]}
                        for p in tool["presets"]
                    ]
                result.append(item)
        except Exception:
            pass
    return result


def get_tool(tool_id: str):
    """Get a registered tool by id (full dict including handler), or None."""
    for tool in _registry:
        if tool["id"] == tool_id:
            return tool
    return None


def get_default_preset(tool_id: str):
    """Return the default preset id for a tool, or None."""
    tool = get_tool(tool_id)
    if not tool or not tool["presets"]:
        return None
    for p in tool["presets"]:
        if p.get("default"):
            return p["id"]
    return None
