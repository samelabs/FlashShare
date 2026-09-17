"""economy/settlement — tool-call cash settlement (economy public service).

The execution gate orchestrates (claim → quote → charge → execute →
settle) but owns NO economic formulas. This module is the single place
that interprets a successful paid tool call into its cash movement:

  accrue_tool_success(...) — third-party tool: accrue the declared
                             provider payout (provider_earning pool,
                             usd_cent) to the tool owner.

That is the only cash fact of a successful call: the user side is a
credits consume (economy.billing), the provider side is declared cash.
Self-calls and first-party tools accrue nothing. There is no
credits→cash conversion and no commission percentage anywhere.

Dependency direction (one-way): execution → economy.settlement → repo.
Settlement never imports execution; it receives plain facts.
"""

import json
import logging

from repo.ledger import (cash_pool_balance,
                         finalize_call_ok,
                         insert_provider_earn_if_absent)
from utils import now

logger = logging.getLogger("flashshare")

EARNING_POOL = "provider_earning"
EARNING_UNIT = "usd_cent"


def accrue_tool_success(con, call_id: int, tool: dict, user_id: int,
                        payout_minor: int, result: dict) -> None:
    """Settle one successful call INSIDE the caller's transaction.

    finalize_call_ok (the metering fact) and the provider payout row
    commit together — a client that received success can never lose the
    earning. Idempotency: payout:{call_id} (unique idem + NOT EXISTS),
    so replays converge instead of double-crediting.
    """
    ts = now()
    finalize_call_ok(
        con, call_id, result.get("publicId"),
        json.dumps(result, ensure_ascii=False), ts)
    owner_id = tool.get("owner_id")
    if payout_minor <= 0 or owner_id is None or owner_id == user_id:
        return
    base = cash_pool_balance(con, owner_id, EARNING_POOL, EARNING_UNIT)
    insert_provider_earn_if_absent(
        con, owner_id, payout_minor, base + payout_minor, call_id,
        f"payout:{call_id}", f"{tool['id']} payout", ts)
