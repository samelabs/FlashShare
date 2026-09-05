"""Tool catalog — the data-state runtime for the tool registry (Phase 1).

register() in tools.py remains the CREATION format for first-party tools.
This module compiles those declarations into `platform_tools` rows and makes
the DATABASE the runtime source of truth for the catalog:

    startup:  load_all() → seed_from_registry() → rows exist
    catalog:  list_catalog() reads rows (pricing overlay here, one place)
    resolve:  resolve(tool_key, viewer) — the single routing entry for all
              three consumption surfaces (web grid, /api/v1, MCP)

Why: the platform thesis — new capabilities enter as DATA rows (user tools,
remote execution classes), never as code changes. First-party tools are just
seed rows with owner_id NULL and execution='local'.

Handler dispatch stays in tools.get_tool() for local tools (worker needs the
callable); catalog rows carry the DECLARATION, not the callable. resolve()
joins the two: DB row = contract, _registry = implementation for local class.
"""

import json

from database import db
from repo import catalog as catalog_repo
from utils import now

# Visibility ladder (data field, owner-settable — no human gate).
# Validation of stored values happens at write time (samelabs/remote and
# the future user-submission endpoint), not read time.
VIS_PRIVATE = "private"    # owner only
VIS_UNLISTED = "unlisted"  # callable when the key is known, never listed
VIS_PUBLIC = "public"      # grid + every caller's tools/list


# ── Seeding: register() declarations → platform_tools rows ──

def seed_from_registry() -> dict:
    """Sync first-party register() declarations into platform_tools.

    Idempotent upsert ON tool_key. First-party keys are bare ids ('beautify').
    Preserves row identity (id, created_at) and operator-managed fields that
    first-party seeding does not own (visibility stays 'public' for seeds,
    enabled reflects declaration on conflict — pricing.enabled remains the
    commerce switch, platform_tools.enabled is the registry switch).

    A tool removed from code keeps its row (enabled=false) so metering
    history stays interpretable — same convention as the pricing table.
    """
    from tools import _registry
    ts = now()
    added = 0
    updated = 0
    with db() as con:
        for t in _registry:
            row = catalog_repo.upsert_first_party(
                con, t["id"], t["type"],
                json.dumps(t["label"], ensure_ascii=False),
                t["icon"],
                json.dumps(sorted(t["accept"]), ensure_ascii=False) if t["accept"] else None,
                json.dumps(t["presets"], ensure_ascii=False) if t["presets"] else None,
                bool(t.get("multi", False)), ts,
                json.dumps(t["desc"], ensure_ascii=False) if t.get("desc") else None)
            if row["inserted"]:
                added += 1
            else:
                updated += 1
        # Retired-in-code first-party tools: flip enabled off, keep the row
        catalog_repo.retire_absent_first_party(
            con, [t["id"] for t in _registry], ts)
    return {"added": added, "updated": updated}


def _row_to_item(row: dict, price: dict) -> dict:
    """platform_tools row + pricing row → the catalog item shape.

    Byte-compatible with the legacy tools.list_all_tools() item (same keys,
    same types) — the three consumption surfaces keep one contract.
    """
    item = {
        "id": row["tool_key"],  # namespaced key is THE identity from now on
        "type": row["tool_type"],
        "label": json.loads(row["label_json"]),
        "icon": row["icon"],
        "accept": json.loads(row["accept_json"]) if row["accept_json"] else None,
        "presets": None,
        "multi": bool(row["multi"]),
        "cost": int(price.get("cost") or 0),
    }
    if row["presets_json"]:
        item["presets"] = [
            {"id": p["id"], "label": p["label"], "default": p.get("default", False)}
            for p in json.loads(row["presets_json"])
        ]
    # params 声明 (tool modes v3): 三面同构 — web 浮层/MCP inputSchema/详情同源
    item["params"] = json.loads(row["params_json"]) if row["params_json"] else None
    # 简介 (名称/简介/模式/使用): str|dict, 前端 resolveLabel 解析; 系统工具简介
    # 在 i18n {id}_desc — 由前端 t() 双通道兼容
    item["desc"] = json.loads(row["desc_json"]) if row.get("desc_json") else None
    return item


# ── Catalog (single source for web home, /api/v1/tools, MCP tools/list) ──

def search_catalog(q: str, viewer_id: int | None = None, limit: int = 30) -> list:
    """518-native tool search: prefix/substring match across name (all label
    langs) and tool key, self-operated + community in ONE result list.

    NOT a directory page (that's the MCP-marketplace pattern) — the home
    search box IS the tool selector: typing filters, clicking selects the
    active tool, the upload flow is unchanged. Community rows carry a
    provider badge so provenance is visible at a glance.
    """
    q = (q or "").strip().lower()
    if not q:
        return []
    like = f"%{q}%"
    with db() as con:
        rows = catalog_repo.search(con, VIS_PUBLIC, viewer_id, like, limit)
    out = []
    for row in rows:
        p = _price_of(row["tool_key"])
        if p is None:
            continue
        out.append(_row_to_item(row, p))
    return out


def _price_of(tool_key: str) -> dict | None:
    """pricing overlay for one row: None = disabled/hidden."""
    from governance.pricing import get_pricing
    p = get_pricing(tool_key)
    if not p or not p["enabled"]:
        return None
    return p


def catalog_version() -> str:
    """Cheap change-detection for MCP clients: any insert/update/delete of
    an enabled row bumps this digest (count + max updated_at + key checksum).
    Agents compare versions to know when to re-list."""
    with db() as con:
        row = catalog_repo.version_digest(con)
    return f"v{row['n']}-{row['mx']}-{row['ck'] % 100000}"


def list_catalog(viewer_id: int | None = None, include_remote: bool = True) -> list:
    """Pricing-overlaid catalog, identical contract to the legacy
    tools.list_all_tools(): disabled tools hidden, cost included.

    viewer_id: when given, the result additionally includes the viewer's
    own private/unlisted tools (owner surface). Public listing (web home
    for anonymous) passes None → public rows only.
    include_remote: False strips execution=http|mcp rows — the web home
    grid is local-only until the remote-tool UX ships; /api/v1 and MCP
    keep them (programmatic surfaces are the remote-first consumers).
    """
    from governance.pricing import list_pricing
    prices = {r["tool_id"]: r for r in list_pricing()}

    with db() as con:
        rows = catalog_repo.visible_to(con, VIS_PUBLIC, viewer_id)

    result = []
    for row in rows:
        if not include_remote and row["execution"] != "local":
            continue
        p = prices.get(row["tool_key"]) or {"cost": 0, "enabled": True}
        if not p["enabled"]:
            continue
        result.append(_row_to_item(row, p))
    return result


def get_default_preset(tool_key: str) -> str | None:
    """Default preset id for a tool key, or None."""
    with db() as con:
        row = catalog_repo.by_tool_key(con, tool_key)
    if not row or not row["presets_json"]:
        return None
    for p in json.loads(row["presets_json"]):
        if p.get("default"):
            return p["id"]
    return None


# ── resolve: the single routing entry for all surfaces ──

def resolve(tool_key: str, viewer_id: int | None = None) -> tuple:
    """Resolve a tool key to its full runtime declaration.

    Returns (tool, error) — exactly one is non-None.
      tool: dict with row fields + `handler` attached when execution='local'
            (the callable from tools._registry; worker dispatch needs it).
      error: 'not_found' | 'disabled' | 'forbidden'

    Key grammar: bare id ('beautify') = first-party; 'owner/tool' = user
    namespace (user tools land as data rows in Phase 3 — resolve() already
    speaks the grammar so no surface changes then).

    Visibility is enforced HERE so the three surfaces cannot diverge:
      public    → everyone
      unlisted  → everyone (callable, never listed)
      private   → owner only
    """
    with db() as con:
        row = catalog_repo.by_tool_key(con, tool_key)
    if not row:
        return None, "not_found"
    if not row["enabled"]:
        return None, "disabled"
    if row["visibility"] == VIS_PRIVATE:
        if viewer_id is None or row["owner_id"] != viewer_id:
            return None, "forbidden"

    tool = dict(row)
    if tool["execution"] == "local":
        from tools import get_tool as _get_local
        local = _get_local(tool["tool_id"])
        if local:
            tool["handler"] = local["handler"]
            tool["accept"] = local["accept"]
    return tool, None


def mcp_name(tool_key: str) -> str:
    """MCP tool-name projection: 'sun/super-resolve' → 'sun__super-resolve'
    (MCP name charset forbids '/'; bare first-party ids pass through)."""
    return tool_key.replace("/", "__")
