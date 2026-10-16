"""Capabilities — the ONE permission vocabulary of the /mcp surface.

Every bearer credential speaks this language: an fsk_ API key carries a
scope string on its row (full set by default), an OAuth access token
carries the scopes granted at consent. The /mcp dispatch chokepoint is
the single place they are compared against what an operation needs —
there is no second authorization logic anywhere.

Scope classes map to the three sensitivity tiers of the real operation
set: executing tools (paid tools spend credits), enumerating the
account's live files (names are private), deleting files (irreversible).
"""

SCOPES = ("tools:call", "files:read", "files:delete")
FULL_SCOPES = " ".join(SCOPES)
_SCOPE_SET = frozenset(SCOPES)


def normalize_scope(raw) -> str:
    """Space-separated scope string → canonical form. Raises ValueError
    on any token outside the vocabulary (strict — a typo'd scope must
    never silently narrow or widen a grant)."""
    parts = [p for p in str(raw or "").split() if p]
    if not parts:
        raise ValueError("invalid_scope")
    if any(p not in _SCOPE_SET for p in parts):
        raise ValueError("invalid_scope")
    uniq = dict.fromkeys(parts)
    return " ".join(s for s in SCOPES if s in uniq)


def scope_set(raw) -> frozenset:
    """A stored scope string → set (storage is trusted; written via
    normalize_scope)."""
    return frozenset(p for p in str(raw or "").split() if p)


# Operation → required capability (the dispatch chokepoint reads this).
# Discovery and favorites ride tools:call: they are part of the
# tool-usage workflow, not a separate trust tier.
OP_SCOPES = {
    "tool": "tools:call",        # any platform tool execution
    "create_upload": "tools:call",
    "catalog_search": "tools:call",
    "pin_tool": "tools:call",
    "unpin_tool": "tools:call",
    "list_favorites": "tools:call",
    "list_uploads": "files:read",
    "delete_upload": "files:delete",
}


def op_allowed(op: str, granted) -> bool:
    need = OP_SCOPES.get(op)
    return need is None or need in granted
