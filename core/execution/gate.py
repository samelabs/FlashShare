"""execution/gate — one metering/idempotency gate for Web, REST v1 and MCP.

A tool_call row is claimed before charge/execution. Its id is the durable
attempt identity used by the ledger; success finalizes that same row, failure
finalizes it and refunds the exact charge row. client_ref is scoped by user +
tool, so different tools may safely reuse the same caller job id.
"""

import json
import logging
import secrets

from database import db
from economy.billing import InsufficientCredits, consume, refund, spendable_balance
from governance.pricing import get_pricing
from repo.ledger import (claim_call, finalize_call_failed, finalize_call_ok,
                         insert_provider_earn_if_absent, total_balance)
from utils import now

logger = logging.getLogger("flashshare")

from governance.commission import commission_rate


def _record_failure(call_id: int, error, status: str = "failed") -> bool:
    """Transition the claimed invocation fact to a terminal failed state.

    Returns True on success. A False return leaves the row in 'running' —
    a durable unresolved obligation that startup recovery will settle.
    """
    try:
        with db() as con:
            row = finalize_call_failed(con, call_id, status, error, now())
        return row is not None
    except Exception:
        logger.exception("failed-call finalize failed call_id=%s", call_id)
        return False


def _refund_charge(user: dict, tool_id: str, charge: dict | None,
                   reason: str) -> bool:
    """Refund exactly one durable consume row; concurrent callers converge.

    Returns True when no refund was needed (no charge) or the refund
    committed (including idempotent replay of an already-refunded row).
    Returns False only when the refund genuinely failed — the caller must
    then keep the call 'running' and report err_metering_failed.
    """
    if not charge:
        return True
    charge_id = int(charge["id"])
    amount = -int(charge["delta"])
    if amount <= 0:
        return True
    try:
        refund(user["id"], amount,
               ref_type="ledger", ref_id=str(charge_id),
               idem_key=f"refund:ledger:{charge_id}",
               note=f"{tool_id}: {reason}"[:200])
        return True
    except Exception:
        logger.exception("refund failed for ledger charge %s", charge_id)
        return False


def _settle_failure(user: dict, tool_id: str, call_id: int, charge,
                    error: str, status: str = "failed") -> bool:
    """Durable failure settlement: refund the exact charge FIRST, then
    finalize the fact. If the refund fails the call stays 'running' (an
    unresolved obligation); if only the finalize fails after a successful
    refund, the row also stays 'running' — recovery replays the same
    refund idem key, so it can never double-refund.
    """
    if not _refund_charge(user, tool_id, charge, error):
        return False
    return _record_failure(call_id, error, status=status)


def recover_abandoned_calls() -> dict:
    """Startup settlement for invocation facts left 'running' by a dead
    process (single-worker deployment: every surviving running row belongs
    to the previous process).

    Per row: no consume row -> finalize failed (died before charging).
    Consume present -> refund via the same exact ledger identity, then
    finalize failed. Any refund failure keeps the row running for the next
    restart. No queue table, no cron — the running fact IS the worklist.
    """
    from repo.ledger import by_idem_key
    out = {"scanned": 0, "failed_no_charge": 0, "refunded": 0,
           "kept_running": 0}
    with db() as con:
        rows = con.execute(
            "SELECT id, user_id, tool_id FROM tool_calls "
            "WHERE status = 'running' ORDER BY id").fetchall()
    out["scanned"] = len(rows)
    for row in rows:
        call_id = int(row["id"])
        charge = None
        with db() as con:
            charge = by_idem_key(con, int(row["user_id"]),
                                 f"call:{call_id}")
        if charge is None:
            if _record_failure(call_id, "process_restarted"):
                out["failed_no_charge"] += 1
            else:
                out["kept_running"] += 1
            continue
        if _refund_charge({"id": int(row["user_id"])}, row["tool_id"],
                          dict(charge), "process_restarted"):
            if _record_failure(call_id, "process_restarted"):
                out["refunded"] += 1
            else:
                out["kept_running"] += 1
        else:
            out["kept_running"] += 1
    if out["kept_running"]:
        logger.error("startup recovery: %d running calls could not be "
                     "settled and remain unresolved", out["kept_running"])
    return out


def _finish(user: dict, tool: dict, call_id: int, cost: int,
            result: dict) -> dict:
    """Finalize call fact + provider/platform split in one transaction.

    Self-calls are free by policy, so cost is already zero before this point.
    For metered calls the invariant is cost = provider payout + platform share,
    or cost = platform revenue for first-party tools.
    """
    owner_id = tool.get("owner_id")
    payout = 0
    if cost > 0 and owner_id and owner_id != user["id"]:
        rate = commission_rate()
        payout = cost - (cost * rate // 100)

    with db() as con:
        finalize_call_ok(
            con, call_id, result.get("publicId"),
            json.dumps(result, ensure_ascii=False), now())
        if cost > 0:
            from governance.platform import record_platform_income
            if payout > 0 and owner_id is not None:
                base = total_balance(con, owner_id)
                insert_provider_earn_if_absent(
                    con, owner_id, payout, base + payout, call_id,
                    f"payout:{call_id}", f"{tool['id']} payout", now())
                record_platform_income(
                    con, call_id, "platform_commission", cost - payout,
                    f"{tool['id']} commission", now())
            elif not owner_id:
                record_platform_income(
                    con, call_id, "platform_revenue", cost,
                    f"{tool['id']} revenue", now())

    out = {"ok": True, "type": tool["type"], "result": result,
           "call_id": call_id}
    if cost > 0:
        try:
            out["balance"] = spendable_balance(user["id"])
        except Exception:
            logger.exception("balance fetch failed for UI chip")
    return out


async def run(user: dict, tool: dict, invocation, orchestrate):
    """Claim → charge → execute → finalize; failures finalize + refund."""
    from . import ExecutionError

    tool_id = invocation.tool_id
    params = invocation.params

    price = get_pricing(tool_id)
    if not price["enabled"]:
        return None, {"ok": False, "error": "tool_disabled"}, 403

    client_ref = invocation.client_ref or secrets.token_hex(16)

    from economy.antibuse import gate_ref_throttled
    if gate_ref_throttled(user["id"]):
        return None, {"ok": False, "error": "err_rate_limited"}, 429

    # Product rule: a provider may run their own tool for self-test/use without
    # paying themselves or the platform. The call fact still records success.
    listed_cost = int(price["cost"])
    cost = 0 if tool.get("owner_id") == user["id"] else listed_cost
    started = now()

    # Claim before charging/executing. The DB partial unique index makes this
    # race-safe for the same user + tool + client_ref.
    with db() as con:
        anchor, claimed = claim_call(
            con, client_ref, user["id"], tool["id"], cost,
            json.dumps(params, ensure_ascii=False), started)

    if not claimed:
        if anchor.get("status") == "ok" and anchor.get("result_json"):
            try:
                replay_result = json.loads(anchor["result_json"])
            except Exception:
                logger.exception("stored replay payload invalid call_id=%s",
                                 anchor.get("id"))
                return None, {"ok": False, "error": "err_metering_failed"}, 500
            return ({"ok": True, "type": tool["type"],
                     "result": replay_result, "replay": True,
                     "call_id": anchor["id"]}, None, None)
        # A concurrent request already owns this logical invocation. Do not run
        # the tool twice; caller can retry the same ref and receive the result.
        return None, {"ok": False, "error": "err_call_in_progress"}, 409

    call_id = int(anchor["id"])
    charge = None
    if cost > 0:
        try:
            charge = consume(
                user["id"], cost, "consume",
                ref_type="tool_call", ref_id=str(call_id),
                idem_key=f"call:{call_id}", note=tool_id)
        except InsufficientCredits as exc:
            _record_failure(call_id, f"balance:{exc.balance}",
                            status="insufficient")
            return (None,
                    {"ok": False, "error": "err_insufficient_credits",
                     "balance": exc.balance, "cost": exc.cost}, 402)

    try:
        result = await orchestrate()
    except ExecutionError as exc:
        # Durable settlement: refund the exact charge before finalizing; if
        # either leg fails the row stays running (recovery settles it) and
        # the client must not be told the economy is closed.
        if _settle_failure(user, tool_id, call_id, charge, exc.code):
            return None, {"ok": False, "error": exc.code}, 400
        return None, {"ok": False, "error": "err_metering_failed"}, 500
    except Exception:
        logger.exception("tool execution failed call_id=%s", call_id)
        if _settle_failure(user, tool_id, call_id, charge, "tool_failed"):
            return None, {"ok": False, "error": "tool_failed"}, 500
        return None, {"ok": False, "error": "err_metering_failed"}, 500

    try:
        return _finish(user, tool, call_id, cost, result), None, None
    except Exception:
        # Output execution may already have succeeded, but accounting is not
        # allowed to report success without the authoritative fact/split.
        logger.exception("metering finalize failed call_id=%s", call_id)
        if _settle_failure(user, tool_id, call_id, charge, "metering_failed"):
            return None, {"ok": False, "error": "err_metering_failed"}, 500
        return None, {"ok": False, "error": "err_metering_failed"}, 500
