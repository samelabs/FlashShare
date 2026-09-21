"""execution/gate — one metering/idempotency gate for Web and MCP.

A tool_call row is claimed before charge/execution. Its id is the durable
attempt identity used by the ledger; success finalizes that same row, failure
finalizes it and refunds the exact charge row. client_ref is scoped by user +
tool, so different tools may safely reuse the same caller job id.
"""

import json
import logging

from database import db
from economy.billing import InsufficientCredits, consume, refund, spendable_balance
from repo import ledger as ledger_repo
from repo.ledger import (claim_call, finalize_call_failed, finalize_call_ok,
                         insert_provider_earn_if_absent)
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
            staged, commission_rate_snapshot: int) -> dict:
    """The ONE success settlement transaction: durable result landing
    (uploads INSERT) + tool_call ok (upload_id/result binding) +
    provider/platform split — all-or-nothing. `staged` is a
    pipeline.StagedResult (physical Gokapi object only); every 518
    public identity is born inside this transaction.

    Self-calls are free by policy, so cost is already zero before this point.
    Value conservation for metered calls, all in credits:

      third-party tool:  user debit = provider earning + platform commission
                         provider = cost - cost*rate//100  (rate = the
                                       CLAIM-TIME commission_rate_snapshot
                                       persisted on the tool_call row —
                                       never current settings)
                         platform = cost - provider
      first-party tool:  user debit = platform revenue (full cost)

    The third-party split writes BOTH legs whenever owner is set and the
    caller is not the owner — including rate=100 (provider 0, platform
    full cost) and rate=0 (provider full cost, platform 0). Zero legs
    are skipped only when the leg amount is exactly 0.
    """
    from execution.pipeline import land_tool_result
    from config import BASE_URL

    # Validate the durable attempt fact BEFORE any settlement side effect
    # (uploads landing, tool_call ok, earning/revenue). Every runtime
    # attempt carries a claim-time snapshot (DB invariant: running →
    # NOT NULL); the validation therefore applies to EVERY call —
    # first-party/free/self-owned may not consume the rate, but they can
    # never proceed on an invalid attempt snapshot. NULL/out-of-range is
    # a writer bug, not a default-to-0% situation — fail loud and let
    # the strict outcome reconcile settle the economic failure.
    if (not isinstance(commission_rate_snapshot, int)
            or not 0 <= commission_rate_snapshot <= 100):
        raise RuntimeError("invalid_commission_snapshot")

    owner_id = tool.get("owner_id")
    payout = 0
    commission = 0
    if cost > 0 and owner_id is not None and owner_id != user["id"]:
        # claim-time snapshot ONLY — current settings are never read at
        # settlement; a config change mid-execution cannot re-split an
        # in-flight attempt.
        rate = commission_rate_snapshot
        payout = cost - (cost * rate // 100)
        commission = cost - payout

    with db() as con:
        upload_id, public_result = land_tool_result(
            con, user["id"], staged, BASE_URL)
        finalize_call_ok(
            con, call_id, upload_id,
            public_result.get("publicId"),
            json.dumps(public_result, ensure_ascii=False), now())
        if cost > 0 and owner_id is None:
            # first-party: full cost is platform revenue
            from governance.platform import record_platform_income
            record_platform_income(
                con, call_id, "platform_revenue", cost,
                f"{tool['id']} revenue", now())
        elif cost > 0:
            # third-party: two independent legs, conservation holds at
            # every commission rate (0% / 30% / 100%)
            if payout > 0:
                base = ledger_repo.earn_pool_balance(con, owner_id)
                insert_provider_earn_if_absent(
                    con, owner_id, payout, base + payout, call_id,
                    f"payout:{call_id}", f"{tool['id']} payout", now())
            if commission > 0:
                from governance.platform import record_platform_income
                record_platform_income(
                    con, call_id, "platform_commission", commission,
                    f"{tool['id']} commission", now())

    out = {"ok": True, "type": tool["type"], "result": public_result,
           "call_id": call_id}
    if cost > 0:
        try:
            out["balance"] = spendable_balance(user["id"])
        except Exception:
            logger.exception("balance fetch failed for UI chip")
    return out


OUTCOME_DURABLE_SUCCESS = "durable_success"
OUTCOME_PROVEN_ROLLBACK = "proven_rollback"
OUTCOME_UNRESOLVED = "unresolved"


def resolve_success_outcome(call_id: int) -> dict:
    """Durable reconciliation after a _finish exception — STRICT tri-state.

    A commit-time exception does NOT prove the transaction failed — the
    commit may have landed and the ACK was lost. Read the authoritative
    tool_call row under FOR UPDATE (the settlement transaction has fully
    completed by the time the lock is granted):

      OUTCOME_DURABLE_SUCCESS — row exists, status='ok', result_json
          parses: the settlement IS durable. Caller returns the stored
          result; never refunds, never deletes the staged object.
      OUTCOME_PROVEN_ROLLBACK — row exists AND status='running'. If the
          success transaction had committed, status would already be
          'ok'; a still-running row PROVES the settlement never became
          durable. Only this outcome may clean up the staged result and
          settle failure (exact refund → terminal failed).
      OUTCOME_UNRESOLVED — anything else: row missing, unreadable DB,
          a terminal status that reconciliation must not observe
          (failed/insufficient), or status='ok' with a NULL/malformed/
          unexpected result_json. A durable-ok settlement whose payload
          is corrupted is still a durable economic fact — refunding it
          would leak value — so it stays unresolved: no refund, no
          failed finalize, no staged delete; the running/ok row plus
          the existing durable recovery own the final disposition.
    """
    try:
        with db() as con:
            row = con.execute(
                "SELECT status, result_json FROM tool_calls"
                " WHERE id=%s FOR UPDATE", (call_id,)).fetchone()
    except Exception:
        logger.exception("outcome reconciliation failed call_id=%s", call_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    if row is None:
        logger.error("outcome reconciliation: tool_call row missing"
                     " call_id=%s — unresolved", call_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    if row["status"] == "running":
        return {"outcome": OUTCOME_PROVEN_ROLLBACK}
    if row["status"] != "ok":
        # terminal failed/insufficient must not be observed here in
        # single-worker execution; treat as anomalous fact, not a guess
        logger.error("outcome reconciliation: unexpected terminal status"
                     " %s call_id=%s — unresolved", row["status"], call_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    raw = row["result_json"]
    if not isinstance(raw, str):
        logger.error("durable ok but result_json NULL call_id=%s"
                     " — unresolved", call_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    try:
        durable_result = json.loads(raw)
    except Exception:
        logger.exception("durable ok but result_json malformed call_id=%s"
                         " — unresolved (economic fact stays durable)",
                         call_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    if not isinstance(durable_result, dict) \
            or not durable_result.get("publicId"):
        logger.error("durable ok but result_json shape unexpected"
                     " call_id=%s — unresolved", call_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    return {"outcome": OUTCOME_DURABLE_SUCCESS, "result": durable_result}


async def run(user: dict, tool: dict, invocation, orchestrate):
    """Claim → charge → execute → finalize; failures finalize + refund."""
    from . import ExecutionError

    tool_id = invocation.tool_id
    params = invocation.params

    # Single effective-quote snapshot: enabled + cost both come from one
    # effective_quote() read (listed price × self-owned free, the shared
    # combination point with the advisory preflight). A gate invocation
    # reads the pricing table exactly once.
    from execution.affordability import effective_quote
    quote = effective_quote(user["id"], tool)
    if not quote["enabled"]:
        return None, {"ok": False, "error": "tool_disabled"}, 403
    cost = quote["cost"]

    client_ref = invocation.client_ref

    from economy.antibuse import gate_ref_throttled
    if gate_ref_throttled(user["id"]):
        return None, {"ok": False, "error": "err_rate_limited"}, 429
    started = now()

    # Claim before charging/executing. The DB partial unique index makes this
    # race-safe for the same user + tool + client_ref. The commission config
    # read and the claim INSERT share ONE transaction: the resolved rate is
    # frozen on the row at claim time; later settings changes only affect
    # FUTURE claims, never this attempt's settlement.
    from execution.invocation import invocation_fingerprint
    invocation_hash = invocation_fingerprint(invocation)

    with db() as con:
        rate_snapshot = commission_rate(con=con)
        anchor, claimed = claim_call(
            con, client_ref, user["id"], tool["id"], cost,
            json.dumps(params, ensure_ascii=False), started,
            commission_rate_snapshot=rate_snapshot,
            invocation_hash=invocation_hash)

    if not claimed:
        # The ref is bound to ONE canonical operation. A different
        # fingerprint (or a NULL-hash historical row that cannot prove
        # anything) is a conflict — ZERO charge, ZERO execution; the
        # caller must use a new client_ref for different content.
        anchor_hash = anchor.get("invocation_hash")
        if anchor_hash != invocation_hash:
            return None, {"ok": False, "error": "idempotency_conflict"}, 409
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
    # snapshot authority = the PERSISTED attempt fact, not the stack value
    # (a claimed=True row always carries its own claim-time snapshot; the
    # runtime variable above only fed the INSERT)
    rate_snapshot = anchor["commission_rate_snapshot"]
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
        return _finish(user, tool, call_id, cost, result,
                       rate_snapshot), None, None
    except Exception:
        # The settlement transaction MAY have committed despite the
        # exception (lost ACK) — never guess. Reconcile the durable fact
        # first; only a provably-non-durable outcome may be cleaned up.
        logger.exception("metering finalize failed call_id=%s", call_id)
        outcome = resolve_success_outcome(call_id)
        if outcome["outcome"] == OUTCOME_DURABLE_SUCCESS:
            # settlement IS durable: report the stored success, no refund,
            # no staged delete, no duplicate earning (already committed).
            out = {"ok": True, "type": tool["type"],
                   "result": outcome["result"], "call_id": call_id,
                   "replay": True}
            if cost > 0:
                try:
                    out["balance"] = spendable_balance(user["id"])
                except Exception:
                    logger.exception("balance fetch failed for UI chip")
            return out, None, None
        if outcome["outcome"] == OUTCOME_PROVEN_ROLLBACK:
            # ONLY a still-running row proves the settlement never became
            # durable: staged physical cleanup (best effort — the staged
            # object belongs to THIS attempt), then the existing failure
            # settlement (exact refund → terminal failed).
            from gokapi import delete_gokapi_file
            try:
                delete_gokapi_file(result.gokapi_id)
            except Exception:
                logger.warning("staged result cleanup failed call_id=%s"
                               " gokapi=%s", call_id, result.gokapi_id)
            if _settle_failure(user, tool_id, call_id, charge,
                               "metering_failed"):
                return None, {"ok": False, "error": "err_metering_failed"}, 500
            return None, {"ok": False, "error": "err_metering_failed"}, 500
        # UNRESOLVED (missing row / unreadable DB / terminal status /
        # durable-ok with corrupted payload): no refund, no finalize, no
        # staged delete — the call keeps its durable state for recovery.
        return None, {"ok": False, "error": "err_metering_failed"}, 500
