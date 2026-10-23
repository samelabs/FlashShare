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
from .results import StagedItems

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


def _resolve_consume_failure(user: dict, tool_id: str, call_id: int) -> str:
    """Deterministic claim resolution after a consume() exception that is
    NOT InsufficientCredits (DB error, commit ACK loss, ...).

    The exception alone does not prove the charge rolled back. Read the
    durable consume fact (idem call:{call_id}) under the payer's users
    row lock — the same serialization anchor consume() takes, so any
    in-flight charge transaction has fully ended when the lock is
    granted:
      no charge row  → the charge provably never committed: finalize the
                       call failed in that same locked transaction
                       (zero charge, zero refund)            → "no_charge"
      charge row     → it DID commit: the existing failure settlement
                       (exact refund → failed)               → "refunded"
      any read/write failure → the row stays running; startup
                       recover_abandoned_calls() settles it  → "unresolved"
    """
    try:
        with db() as con:
            ledger_repo.lock_user(con, int(user["id"]))
            charge = ledger_repo.by_idem_key(con, int(user["id"]),
                                             f"call:{call_id}")
            if charge is None:
                row = finalize_call_failed(con, call_id, "failed",
                                           "metering_failed", now())
                return "no_charge" if row is not None else "unresolved"
    except Exception:
        logger.exception("consume outcome resolution failed call_id=%s",
                         call_id)
        return "unresolved"
    if _settle_failure(user, tool_id, call_id, dict(charge),
                       "metering_failed"):
        return "refunded"
    return "unresolved"


def recover_abandoned_calls() -> dict:
    """Startup settlement for invocation facts left 'running' by a dead
    process (single-worker deployment: every surviving running row belongs
    to the previous process).

    Per row: no consume row -> finalize failed (died before charging).
    Consume present -> refund via the same exact ledger identity, then
    finalize failed. Any refund failure keeps the row running for the next
    restart. No queue table, no cron — the running fact IS the worklist.
    """
    from repo.ledger import by_idem_key, running_calls
    out = {"scanned": 0, "failed_no_charge": 0, "refunded": 0,
           "kept_running": 0}
    with db() as con:
        rows = running_calls(con)
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
            staged: StagedItems, commission_rate_snapshot: int,
            delivery: dict) -> dict:
    """The ONE success settlement transaction: EVERY file item's durable
    landing (uploads rows) + the tool_call ok (result binding) +
    provider/platform split — all-or-nothing. `staged` is the execution
    output (items model); text/json items live inside result_json.
    `delivery` is the invocation's delivery policy — the result record's
    expiry authority for every result shape.

    Self-calls are free by policy, so cost is already zero before this
    point. Value conservation for metered calls across the S3 unit
    boundary (consumer Credits vs provider/platform USD-millis, the only
    conversion point = economy.money):
      third-party: provider = cost - cost*rate//100 (claim-time
                   commission_rate_snapshot), platform = cost - provider
      first-party:  platform revenue = full cost
    """
    from config import BASE_URL
    from execution.results import land_items_result

    owner_id = tool.get("owner_id")
    payout = 0
    commission = 0
    if cost > 0 and owner_id is not None and owner_id != user["id"]:
        rate = commission_rate_snapshot
        payout = cost - (cost * rate // 100)
        commission = cost - payout

    from economy.money import credits_to_millis
    payout_millis = credits_to_millis(payout)
    commission_millis = credits_to_millis(commission)
    revenue_millis = credits_to_millis(cost if owner_id is None else 0)

    with db() as con:
        # Lock order: third-party payout locks BOTH endpoints' users rows
        # (payer + owner, ascending by id) BEFORE land_items_result — the
        # uploads INSERT's FK takes FOR KEY SHARE on the payer row, which
        # must never order after another transaction's FOR UPDATE on a
        # users row (two reciprocal provider calls could otherwise cycle).
        # The platform row is always locked last (see governance.platform).
        if payout_millis > 0:
            for uid in sorted((int(user["id"]), int(owner_id))):
                ledger_repo.lock_user(con, uid)
        _upload_id, result_public_id, public_result = land_items_result(
            con, user["id"], staged, BASE_URL, delivery)
        finalize_call_ok(
            con, call_id, result_public_id,
            json.dumps(public_result, ensure_ascii=False), now())
        if cost > 0 and owner_id is None:
            # first-party: full cost is platform revenue (USD-millis)
            from governance.platform import record_platform_income
            record_platform_income(
                con, call_id, "platform_revenue", revenue_millis,
                f"{tool['id']} revenue", now())
        elif cost > 0:
            # third-party: two independent legs, conservation holds at
            # every commission rate (0% / 30% / 100%) — USD-millis legs
            if payout_millis > 0:
                # S4: the owner row was already locked up front (with the
                # payer) — the SAME cash-pool anchor withdrawal
                # request/approve use, so a concurrent withdrawal cannot
                # interleave between the pool read and the earning write.
                base = ledger_repo.earn_pool_balance(con, owner_id)
                insert_provider_earn_if_absent(
                    con, owner_id, payout_millis, base + payout_millis, call_id,
                    f"payout:{call_id}", f"{tool['id']} payout", now())
            if commission_millis > 0:
                from governance.platform import record_platform_income
                record_platform_income(
                    con, call_id, "platform_commission", commission_millis,
                    f"{tool['id']} commission", now())

    out = {"ok": True, "result": public_result,
           "call_id": call_id, "cost": cost}
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
    from repo.ledger import call_outcome_for_update
    try:
        with db() as con:
            row = call_outcome_for_update(con, call_id)
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
            or not isinstance(durable_result.get("items"), list):
        logger.error("durable ok but result_json shape unexpected"
                     " call_id=%s — unresolved", call_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    return {"outcome": OUTCOME_DURABLE_SUCCESS, "result": durable_result}


def _run_enter(user, tool, invocation, oauth_client_id):
    """Sync enter block (runs on the default executor): quote → antibuse →
    claim transaction → replay/conflict branches → consume. Early exits
    return (payload, err_body, status); reaching the end returns
    ("GO", call_id, charge, rate_snapshot, cost). The backpressure slots
    are acquired by the async caller BEFORE this runs and released in its
    finally — an early exit here can never leak them."""
    tool_id = invocation.tool_id
    params = invocation.params
    client_ref = invocation.client_ref
    from execution.affordability import effective_quote
    quote = effective_quote(user["id"], tool)
    if not quote["enabled"]:
        return None, {"ok": False, "error": "tool_disabled"}, 403
    cost = quote["cost"]

    from economy.antibuse import gate_ref_throttled
    if gate_ref_throttled(user["id"]):
        return None, {"ok": False, "error": "err_rate_limited"}, 429
    started = now()

    from execution.invocation import invocation_fingerprint
    invocation_hash = invocation_fingerprint(invocation)

    third_party_metered = (cost > 0
                           and tool.get("owner_id") is not None
                           and tool["owner_id"] != user["id"])
    with db() as con:
        rate_snapshot = commission_rate(con=con)
        if rate_snapshot is None:
            if third_party_metered:
                return None, \
                    {"ok": False, "error": "err_commission_unconfigured"}, 503
            rate_snapshot = 0
        anchor, claimed = claim_call(
            con, client_ref, user["id"], tool["id"], cost,
            json.dumps(params, ensure_ascii=False), started,
            commission_rate_snapshot=rate_snapshot,
            invocation_hash=invocation_hash,
            oauth_client_id=oauth_client_id)

    if not claimed:
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
            return ({"ok": True,
                     "result": replay_result, "replay": True,
                     "call_id": anchor["id"],
                     "cost": anchor.get("cost_snapshot", 0)}, None, None)
        return None, {"ok": False, "error": "err_call_in_progress"}, 409

    call_id = int(anchor["id"])
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
        except Exception:
            logger.exception("consume failed call_id=%s", call_id)
            _resolve_consume_failure(user, tool_id, call_id)
            return None, {"ok": False, "error": "err_metering_failed"}, 500
    return ("GO", call_id, charge, rate_snapshot, cost)


async def run(user: dict, tool: dict, invocation, orchestrate,
              oauth_client_id: str | None = None):
    """Claim → charge → execute → finalize; failures finalize + refund.
    The synchronous enter and settle blocks run on the default executor —
    no DB call blocks the event loop. The sequence (claim → consume →
    execute → settle/refund, the recovery semantics, the single-effective-
    quote read, idempotency and the backpressure finally) is unchanged.

    Cancellation (caller disconnect): a submitted thread cannot be
    stopped, so every durable stage is awaited to convergence —
    await_durable/converge keep the settlement alive across the cancel.
    A claimed+charged call is settled exactly (refund → failed) before
    the cancel propagates; an already-durable success stands (replay
    returns it later, never refunded, never deleted). The backpressure
    slots are released only after the real work and its settlement have
    ended."""
    from . import ExecutionError
    import asyncio
    from .durable import spawn, await_durable, converge
    from .pool import (try_acquire as _pool_acquire,
                       release as _pool_release,
                       inflight_count as _pool_inflight,
                       LOCAL_MAX_INFLIGHT as _POOL_CAP,
                       remote_try_acquire as _remote_acquire,
                       remote_release as _remote_release,
                       remote_inflight_count as _remote_inflight,
                       REMOTE_MAX_INFLIGHT as _REMOTE_CAP)

    local_slot = False
    remote_slot = False
    if tool.get("execution") == "local":
        if not _pool_acquire():
            logger.warning("local execution pool saturated: in_flight=%d "
                           "limit=%d", _pool_inflight(), _POOL_CAP)
            return None, {"ok": False, "error": "err_busy"}, 503
        local_slot = True
    elif tool.get("execution") == "http":
        denial = _remote_acquire(user["id"])
        if denial == "user":
            return None, {"ok": False, "error": "err_rate_limited"}, 429
        if denial == "global":
            logger.warning("remote execution saturated: in_flight=%d "
                           "limit=%d", _remote_inflight(), _REMOTE_CAP)
            return None, {"ok": False, "error": "err_busy"}, 503
        remote_slot = True

    try:
        enter, cancelled = await await_durable(spawn(
            _run_enter, user, tool, invocation, oauth_client_id))
        if cancelled:
            # the enter thread converged durably; a claimed+charged call
            # must be settled now (exact refund → failed) — recovery by
            # restart is not the settlement path for a live cancel
            if enter[0] == "GO":
                _, call_id, charge, _rate, _cost = enter
                await converge(spawn(
                    _settle_failure, user, invocation.tool_id, call_id,
                    charge, "cancelled"))
            raise asyncio.CancelledError
        if enter[0] != "GO":
            return enter[0], enter[1], enter[2]
        _, call_id, charge, rate_snapshot, cost = enter
        tool_id = invocation.tool_id

        # The WHOLE execution (worker process / remote HTTP + every
        # output staging write) runs as one tracked task. Started real
        # work cannot be dropped: on cancellation the task is converged —
        # bounded by the existing local-execution/remote timeouts, which
        # stay in force inside it — and only then is the settlement
        # derived from the work's actual outcome (success → durable ok;
        # failure → exact refund). This also carries an output write
        # that already landed but has not returned: its object identity
        # stays inside the converged task, never an orphan.
        orch = asyncio.ensure_future(orchestrate())
        exec_cancelled = False
        result = None
        failure = None          # ExecutionError, or the string "tool_failed"
        try:
            result = await asyncio.shield(orch)
        except asyncio.CancelledError:
            exec_cancelled = True
            try:
                result = await converge(orch)
            except ExecutionError as exc:
                failure = exc
            except Exception:
                logger.exception("tool execution failed call_id=%s", call_id)
                failure = "tool_failed"
        except ExecutionError as exc:
            failure = exc
        except Exception:
            logger.exception("tool execution failed call_id=%s", call_id)
            failure = "tool_failed"

        if failure is not None:
            if isinstance(failure, str):
                code = "tool_failed"
                body = {"ok": False, "error": code, "call_id": call_id}
                status = 500
            else:
                code = failure.code
                body = {"ok": False, "error": failure.code,
                        "call_id": call_id}
                if failure.detail:
                    body["detail"] = failure.detail[:4096]
                status = failure.http_status
            settled, scancel = await await_durable(spawn(
                _settle_failure, user, tool_id, call_id, charge, code))
            if scancel or exec_cancelled:
                raise asyncio.CancelledError
            if settled:
                return None, body, status
            return None, {"ok": False, "error": "err_metering_failed",
                          "call_id": call_id}, 500

        # the success settlement transaction is durable work: converge it
        # across a cancel — its durable outcome stands either way
        finish_cancelled = False
        try:
            finish_task = spawn(
                _finish, user, tool, call_id, cost, result,
                rate_snapshot, invocation.delivery)
            try:
                out = await asyncio.shield(finish_task)
            except asyncio.CancelledError:
                # record the intent BEFORE converging: converge may raise
                # the finish task's own exception straight into the
                # reconciliation below — the pending cancel must survive
                # that handoff and propagate once obligations are settled
                finish_cancelled = True
                await converge(finish_task)
                raise
            if exec_cancelled:
                raise asyncio.CancelledError
            return out, None, None
        except Exception:
            logger.exception("metering finalize failed call_id=%s", call_id)
            # Reconciliation is itself a durable obligation: the verified
            # outcome is processed IN FULL — durable success kept (never
            # refunded, never deleted), proven rollback cleaned + exactly
            # refunded + failed, unresolved left to the existing recovery
            # rules — and only then may a pending cancellation propagate.
            outcome, rcancel = await await_durable(spawn(
                resolve_success_outcome, call_id))
            cancel_now = rcancel or exec_cancelled or finish_cancelled

            def _cancel_pending() -> bool:
                # Intent decoupled from HOW each durable await resolved:
                # every cancel observed at any await of THIS task stays
                # counted on the task until the error actually
                # propagates (nothing here calls uncancel), so the flag
                # survives even when an underlying task raises and the
                # await_durable result tuple never comes back.
                return asyncio.current_task().cancelling() > 0
            if outcome["outcome"] == OUTCOME_DURABLE_SUCCESS:
                out = {"ok": True, "result": outcome["result"],
                       "call_id": call_id, "replay": True,
                       "cost": cost}
                if cost > 0:
                    try:
                        balance, bcancel = await await_durable(spawn(
                            spendable_balance, user["id"]))
                        out["balance"] = balance
                        cancel_now = cancel_now or bcancel
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("balance fetch failed for UI chip")
                if cancel_now or _cancel_pending():
                    raise asyncio.CancelledError
                return out, None, None
            if outcome["outcome"] == OUTCOME_PROVEN_ROLLBACK:
                from storage import delete as storage_delete
                for f in result.files:
                    try:
                        _, dcancel = await await_durable(spawn(
                            storage_delete, f.gokapi_id))
                        cancel_now = cancel_now or dcancel
                    except Exception:
                        logger.warning("staged result cleanup failed call_id=%s"
                                       " gokapi=%s", call_id, f.gokapi_id)
                settled, scancel = await await_durable(spawn(
                    _settle_failure, user, tool_id, call_id, charge,
                    "metering_failed"))
                if scancel or cancel_now or _cancel_pending():
                    raise asyncio.CancelledError
                return None, {"ok": False, "error": "err_metering_failed"}, 500
            # UNRESOLVED: the row keeps its durable state; the existing
            # recovery rules own it — a pending cancel may propagate now
            if cancel_now or _cancel_pending():
                raise asyncio.CancelledError
            return None, {"ok": False, "error": "err_metering_failed"}, 500
    finally:
        if local_slot:
            _pool_release()
        if remote_slot:
            _remote_release(user["id"])

