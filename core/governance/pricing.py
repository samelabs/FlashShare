"""Pricing + tool-call metering service (batch C).

Single source of truth for a tool's commercial contract lives in the
pricing table (owned by /samelabs + the owner's publish form):

    user_cost_credits     what a caller pays per call (credits)
    provider_payout_minor what the tool owner earns per successful
                          third-party call (cash, usd_cent)
    payout_currency       currently 'USD'
    enabled               the sell switch

These are independent commercial fields — there is NO conversion between
user_cost_credits and provider_payout_minor.

A missing pricing row is NOT free-by-default: the commercial contract is
part of tool creation (declaration + pricing insert in one transaction),
so a tool without a pricing row is not commercially created. Reads
fail closed: absent row → enabled=False (the gate answers tool_disabled).

The metering point is ONE place: the tool execution gate. Every billable
execution path (web UI, /api/v1, MCP) funnels through execute with
client_ref idempotency + cost snapshot, so credits are charged exactly
once per logical call.
"""

from database import db
from repo import catalog as catalog_repo
from utils import now


_MAX_COST = 100_000          # credits
_MAX_PAYOUT_MINOR = 10_000_000_00  # usd_cent ($1,000,000)


def seed_pricing(tool_ids: list) -> dict:
    """Idempotent seeding: INSERT ... ON CONFLICT DO NOTHING.

    Called at startup (after load_all) — first-party registry tools get a
    free first-party row (0 credits / 0 payout). User/operator creations
    write their commercial terms in the same transaction as the
    declaration (create_tool_with_pricing), never here.
    """
    ts = now()
    added = 0
    with db() as con:
        for tid in tool_ids:
            added += catalog_repo.seed_pricing_row(con, tid, ts)
    return {"added": added}


def get_pricing(tool_id: str) -> dict:
    """Return the pricing contract row; absent row fails closed
    (enabled=False — the tool is not commercially created)."""
    with db() as con:
        row = catalog_repo.pricing_row(con, tool_id)
    if not row:
        return {"tool_id": tool_id, "user_cost_credits": 0,
                "provider_payout_minor": 0, "payout_currency": "USD",
                "enabled": False}
    return dict(row)


def get_cost(tool_id: str) -> int:
    """Cost in credits for one execution. 0 = free."""
    return int(get_pricing(tool_id)["user_cost_credits"])


def is_enabled(tool_id: str) -> bool:
    return bool(get_pricing(tool_id)["enabled"])


def validate_commercial_terms(cost: int, payout_minor: int,
                              currency: str) -> None:
    """Canonical validation for the commercial fields (single writer
    gate). Raises ValueError with a stable key on violation."""
    if not isinstance(cost, int) or not (0 <= cost <= _MAX_COST):
        raise ValueError("bad_cost")
    if not isinstance(payout_minor, int) or not (
            0 <= payout_minor <= _MAX_PAYOUT_MINOR):
        raise ValueError("bad_payout")
    if currency != "USD":
        raise ValueError("bad_currency")


def set_pricing(tool_id: str, cost: int, payout_minor: int = 0,
                currency: str = "USD", enabled: bool = True,
                admin_user_id: int | None = None) -> dict:
    """Update an existing tool's commercial contract (operator route).

    The route layer validates first; this is the single writer.
    """
    validate_commercial_terms(cost, payout_minor, currency)
    ts = now()
    with db() as con:
        row = catalog_repo.upsert_pricing(
            con, tool_id, int(cost), int(payout_minor), currency,
            bool(enabled), ts, admin_user_id)
    return dict(row)


def create_tool_with_pricing(declaration: dict, cost: int,
                             payout_minor: int, currency: str = "USD",
                             admin_user_id: int | None = None,
                             con=None) -> str:
    """Atomic tool creation: declaration + commercial contract in ONE
    transaction. Either both land or neither — the free-by-absence
    window (tool committed, pricing missing) is structurally gone.

    con 未传: 自开事务; con 已传: 参与 caller 的事务 (调用方持有
    users 行锁时必须传 con — 自开第二连接会被 FK KEY SHARE 与外层
    FOR UPDATE 死锁)。

    Payout is only meaningful for third-party tools; first-party
    (owner_id None) rows always carry payout 0.
    """
    from governance.tool_supply import create_tool
    validate_commercial_terms(cost, payout_minor, currency)
    if declaration.get("owner_id") is None:
        payout_minor = 0
    ts = now()
    if con is not None:
        tool_key = create_tool(declaration, con=con)
        catalog_repo.upsert_pricing(
            con, tool_key, int(cost), int(payout_minor), currency,
            True, ts, admin_user_id)
        return tool_key
    with db() as own:
        tool_key = create_tool(declaration, con=own)
        catalog_repo.upsert_pricing(
            own, tool_key, int(cost), int(payout_minor), currency,
            True, ts, admin_user_id)
    return tool_key


def list_pricing() -> list:
    with db() as con:
        rows = catalog_repo.all_pricing(con)
    return [dict(r) for r in rows]
