"""Operator tool commands (the
/samelabs routes): pricing, presentation, operator HTTP tool
create/delete. All mutations go through the existing single points —
governance.pricing / governance.tool_supply / repo.catalog — inside ONE
transaction with operator_audit.
"""

import json

from database import db
from repo import operator_audit
from utils import now


def _snap_pricing(r) -> dict | None:
    if r is None:
        return None  # first-time pricing: before = NULL (create semantics)
    return {"tool_id": r["tool_id"], "cost": int(r["cost"]),
            "enabled": bool(r["enabled"])}


def _snap_presentation(r) -> dict:
    if r is None:
        return {}
    return {"tool_key": r["tool_key"], "featured": bool(r["featured"]),
            "sort_order": int(r["sort_order"])}


def _safe_remote_projection(con, tool_key: str) -> dict | None:
    """tool + pricing safe-field projection (allowlist; URL/endpoint_json/
    auth/Vault never enter). Reads via repo.catalog primitives."""
    from repo import catalog as _catalog_repo
    row = _catalog_repo.by_tool_key(con, tool_key)
    if row is None:
        return None
    d = {k: row[k] for k in ("tool_key", "tool_id", "execution", "tool_type",
                             "visibility", "enabled", "featured",
                             "sort_order", "files_min", "files_max")}
    accept_json = row["accept_json"]
    if accept_json:
        try:
            d["accept"] = json.loads(accept_json)
        except Exception:
            d["accept"] = None
    try:
        d["outputs"] = json.loads(row["outputs_json"])
    except Exception:
        d["outputs"] = ["file"]
    pr = _catalog_repo.pricing_row(con, tool_key)
    # pricing missing → cost None (LEFT JOIN semantics; never guess free)
    d["cost"] = pr["cost"] if pr is not None else None
    return d


def _snap_remote(r) -> dict:
    # remote allowlist ONLY — URL/endpoint_json/auth/Vault never enter audit
    if r is None:
        return {}
    return {k: r[k] for k in ("tool_key", "tool_id", "execution",
                              "tool_type", "visibility", "enabled",
                              "featured", "sort_order", "files_min", "files_max",
                              "outputs", "accept",
                              "cost") if k in r.keys()}


def set_tool_pricing(operator_id: int, tool_id: str, cost: int,
                     enabled: bool) -> dict:
    """One transaction: serialize on the platform_tools row, read the
    before snapshot AFTER the lock, mutate, audit. Retired tools fall
    back to a pricing-row lock."""
    from governance.pricing import set_pricing
    from repo.catalog import (tool_row_for_update, pricing_row,
                              pricing_row_for_update)
    with db() as con:
        tool = tool_row_for_update(con, tool_id)
        if tool is not None:
            before = pricing_row(con, tool_id)
        else:
            before = pricing_row_for_update(con, tool_id)
            if before is None:
                raise KeyError("not_found")
        row = set_pricing(tool_id, cost, enabled,
                          admin_user_id=operator_id, con=con)
        operator_audit.record(con, operator_id, "pricing.update", "pricing",
                              str(tool_id), _snap_pricing(before),
                              _snap_pricing(row), now())
    return {k: row[k] for k in ("tool_id", "cost", "enabled")}


def set_tool_presentation(operator_id: int, tool_key: str, featured: bool,
                          sort_order: int) -> dict:
    """Curation authority: featured flag + sort order."""
    from repo.catalog import set_tool_presentation, tool_row_for_update
    with db() as con:
        cur = tool_row_for_update(con, tool_key)
        if cur is None:
            raise KeyError("not_found")
        before = _snap_presentation(cur)
        row = set_tool_presentation(con, tool_key, featured, sort_order, now())
        operator_audit.record(con, operator_id, "tool.presentation.update",
                              "tool", str(tool_key), before,
                              _snap_presentation(row), now())
    return dict(row)


def _snap_enabled(r) -> dict:
    if r is None:
        return {}
    return {"tool_key": r["tool_key"], "enabled": bool(r["enabled"])}


def set_tool_enabled(operator_id: int, tool_key: str,
                     enabled: bool) -> dict:
    """Registry switch (platform_tools.enabled) for any row (Official
    local / Operator HTTP / Provider Tool): row lock → before snapshot →
    tool_supply single-point write → audit, ONE transaction."""
    from governance.tool_supply import update_tool
    from repo.catalog import tool_row_for_update, by_tool_key
    with db() as con:
        cur = tool_row_for_update(con, tool_key)
        if cur is None:
            raise KeyError("not_found")
        before = _snap_enabled(cur)
        update_tool(tool_key, {"enabled": bool(enabled)}, con=con)
        after = _snap_enabled(by_tool_key(con, tool_key))
        operator_audit.record(con, operator_id, "tool.enabled.update",
                              "tool", str(tool_key), before, after, now())
    return after


def create_http_tool(operator_id: int, payload: dict) -> str:
    """Operator trusted HTTP tool create (owner_id NULL, bare-id
    namespace). Validation lives with the route; this command composes
    the canonical declaration and drives tool_supply + pricing + audit
    in ONE transaction. Raises DeclarationError/ValueError from the
    single points."""
    from governance.tool_supply import create_tool, build_http_binding
    from repo.catalog import tool_key_exists
    from governance.pricing import create_tool_with_pricing

    tool_id = payload["tool_id"]
    accept = payload.get("accept")
    auth_header = payload.get("auth_header", "")
    auth_token = payload.get("auth_token", "")
    with db() as con:
        if tool_key_exists(con, tool_id):
            raise ValueError("duplicate_tool")
    declaration = {
        "tool_key": tool_id,
        "tool_id": tool_id,
        "owner_id": None,
        "execution": "http",
        "tool_type": "produce",
        "label": {"en": payload["label"], "zh": payload["label"]},
        "desc": None,
        "icon": "wand",
        "inputs": {"files": {
            "min": int(payload.get("files_min", 1)),
            "max": int(payload.get("files_max", 1)),
            "accept": accept}},
        "outputs": payload.get("outputs") or ["file"],
        # operator params declaration rides THE single supply point —
        # validate_params_decl inside tool_supply._normalize_params is
        # the one validator; this layer adds none of its own.
        "params": payload.get("params"),
        "visibility": "public",
        "enabled": True,
        "binding": build_http_binding({
            "url": payload["url"], "timeout_s": payload["timeout_s"],
            **({"auth_header": auth_header} if auth_header else {}),
            **({"auth_token": auth_token} if auth_token else {}),
        }),
    }
    with db() as con:
        tool_key = create_tool_with_pricing(declaration, payload["cost"],
                                            admin_user_id=operator_id,
                                            con=con)
        after = _snap_remote(_safe_remote_projection(con, tool_key))
        operator_audit.record(con, operator_id, "remote.create",
                              "remote_tool", str(tool_key), None,
                              after, now())
    return tool_key


def delete_http_tool(operator_id: int, tool_key: str) -> None:
    """Platform-owned HTTP tool delete: tool delete + pricing disable +
    audit in ONE transaction (all three commit or roll back together)."""
    from repo.catalog import platform_row_for_update, delete_tool_row
    from governance.pricing import set_pricing
    with db() as con:
        row = platform_row_for_update(con, tool_key, owner_id_is_null=True)
        if not row:
            raise KeyError("not_found")
        before = _snap_remote(_safe_remote_projection(con, tool_key))
        delete_tool_row(con, row["id"])
        set_pricing(tool_key, 0, False, admin_user_id=operator_id, con=con)
        operator_audit.record(con, operator_id, "remote.delete",
                              "remote_tool", str(tool_key), before, None,
                              now())
