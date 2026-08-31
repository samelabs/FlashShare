"""Pricing + tool-call metering service (batch C).

Single source of truth for per-tool cost lives in the pricing table
(owned by /samelabs). register() declares no cost — seeding is
idempotent: every registered tool gets a pricing row (default 0 =
free until the operator prices it); removing a tool leaves its row
(enabled=false) so history stays interpretable.

The metering point is ONE place: the tool execution gate. Every
billable execution path (web UI, /api/v1, MCP) funnels through
execute with client_ref idempotency + cost snapshot + lazy signup
grant, so credits are charged exactly once per logical call.
"""

import json

from database import db
from repo import catalog as catalog_repo
from utils import now


def seed_pricing(tool_ids: list) -> dict:
    """Idempotent seeding: INSERT ... ON CONFLICT DO NOTHING.

    Called at startup (after load_all) and from /samelabs when new
    tools appear. Returns {"added": n}.
    """
    ts = now()
    added = 0
    with db() as con:
        for tid in tool_ids:
            added += catalog_repo.seed_pricing_row(con, tid, ts)
    return {"added": added}


def get_pricing(tool_id: str) -> dict:
    """Return pricing row for a tool; default free when absent."""
    with db() as con:
        row = catalog_repo.pricing_row(con, tool_id)
    if not row:
        return {"tool_id": tool_id, "cost": 0, "enabled": True}
    return dict(row)


def get_cost(tool_id: str) -> int:
    """Cost in credits for one execution. 0 = free."""
    return int(get_pricing(tool_id)["cost"])


def is_enabled(tool_id: str) -> bool:
    return bool(get_pricing(tool_id)["enabled"])


def set_pricing(tool_id: str, cost: int, enabled: bool, admin_user_id: int | None = None) -> dict:
    """/samelabs updates a tool's price/switch."""
    if cost < 0:
        raise ValueError("cost must be >= 0")
    ts = now()
    with db() as con:
        row = catalog_repo.upsert_pricing(con, tool_id, int(cost),
                                          bool(enabled), ts, admin_user_id)
    return dict(row)


def list_pricing() -> list:
    with db() as con:
        rows = catalog_repo.all_pricing(con)
    return [dict(r) for r in rows]
