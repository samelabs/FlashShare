"""Pricing + tool-call metering service (batch C).

Single source of truth for per-tool cost lives in the pricing table
(owned by /samelabs). The pricing row is part of the tool's commercial
contract: creation is atomic with the declaration
(create_tool_with_pricing), and every registered tool gets a pricing
row at seed time (explicit cost=0/enabled=true — free is a stated
fact, never an absence of data). Removing a tool leaves its row
(enabled=false) so history stays interpretable.

A MISSING pricing row fails closed: the tool is not commercially
created and reads as disabled — never free-by-default.

The metering point is ONE place: the tool execution gate. Every
billable execution path (web UI, /api/v1, MCP) funnels through
execute with client_ref idempotency + cost snapshot, so credits are
charged exactly once per logical call.
"""

from database import db
from repo import catalog as catalog_repo
from utils import now


def seed_pricing(tool_ids: list) -> dict:
    """Idempotent seeding: INSERT ... ON CONFLICT DO NOTHING.

    Called at startup (after load_all). First-party registry tools get
    an explicit free row (cost=0, enabled=true). User/operator tool
    creations write their pricing row in the same transaction as the
    declaration (create_tool_with_pricing), never here.
    """
    ts = now()
    added = 0
    with db() as con:
        for tid in tool_ids:
            added += catalog_repo.seed_pricing_row(con, tid, ts)
    return {"added": added}


def get_pricing(tool_id: str) -> dict:
    """Return the pricing row. Missing row fails CLOSED: a tool without
    a pricing row is not commercially created — reads as disabled,
    never free-by-default."""
    with db() as con:
        row = catalog_repo.pricing_row(con, tool_id)
    if not row:
        return {"tool_id": tool_id, "cost": 0, "enabled": False}
    return dict(row)


def get_cost(tool_id: str) -> int:
    """Cost in credits for one execution. 0 = free."""
    return int(get_pricing(tool_id)["cost"])


def is_enabled(tool_id: str) -> bool:
    return bool(get_pricing(tool_id)["enabled"])


def set_pricing(tool_id: str, cost: int, enabled: bool, admin_user_id: int | None = None) -> dict:
    """/samelabs updates a tool's price/switch (single writer)."""
    if cost < 0:
        raise ValueError("cost must be >= 0")
    ts = now()
    with db() as con:
        row = catalog_repo.upsert_pricing(con, tool_id, int(cost),
                                          bool(enabled), ts, admin_user_id)
    return dict(row)


def create_tool_with_pricing(declaration: dict, cost: int,
                             admin_user_id: int | None = None,
                             con=None) -> str:
    """Atomic commercial tool creation: platform_tools + pricing in ONE
    transaction. Either both land or neither — the "tool committed,
    pricing missing" window (which used to read as free+enabled) is
    structurally gone.

    con 未传: 自开事务; con 已传: 参与 caller 的事务 (调用方持有
    users 行 FOR UPDATE 时必须传 con — 自开第二连接会被 FK KEY
    SHARE 与外层行锁死锁)。
    """
    from governance.tool_supply import create_tool
    if cost < 0:
        raise ValueError("cost must be >= 0")
    ts = now()
    if con is not None:
        tool_key = create_tool(declaration, con=con)
        catalog_repo.upsert_pricing(con, tool_key, int(cost), True, ts,
                                    admin_user_id)
        return tool_key
    with db() as own:
        tool_key = create_tool(declaration, con=own)
        catalog_repo.upsert_pricing(own, tool_key, int(cost), True, ts,
                                    admin_user_id)
    return tool_key


def list_pricing() -> list:
    with db() as con:
        rows = catalog_repo.all_pricing(con)
    return [dict(r) for r in rows]
