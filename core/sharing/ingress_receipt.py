"""sharing/ingress_receipt — ingress idempotency orchestration.

Owns: request-hash usage (via ingress_identity), the ATOMIC
quota+receipt claim (one DB transaction), Gokapi staging after the
receipt is running, success finalize (receipt ok + canonical stored
response), strict commit-outcome tri-state resolution, failure
settlement, and startup recovery. Quota decisions stay in
sharing.uploads._quota_denial — this module calls them under the
caller transaction; the algorithm is never copied.
"""

import json as _json
import logging

from database import db
from repo import ingress_requests as _repo
from repo import upload_usage as usage_repo
from utils import now

logger = logging.getLogger("flashshare")


class IngressConflict(Exception):
    """409 idempotency_conflict — same ref bound to different content."""
    def __init__(self, anchor: dict):
        super().__init__("idempotency_conflict")
        self.anchor = anchor


class IngressInProgress(Exception):
    """409 err_upload_in_progress — same ref/hash receipt is running."""
    def __init__(self, anchor: dict):
        super().__init__("err_upload_in_progress")
        self.anchor = anchor


class IngressQuotaDenied(Exception):
    """429 — structured rolling-quota denial (contract passthrough)."""
    def __init__(self, denial: dict):
        super().__init__(denial.get("error", "err_ingress_denied"))
        self.denial = denial


class IngressUnresolved(Exception):
    """500 err_ingress_unresolved — durable outcome cannot be proven."""


def claim_ingress_quota(user_id: int, ip: str, request_ref: str,
                        request_hash: str, sizes: list[int],
                        scope: str = ""):
    """ONE transaction: user lock → receipt binding check → quota read
    → quota decision → upload_usage INSERT → receipt INSERT(running).

    `scope` partitions the client_ref uniqueness: the web upload surface
    passes the selected tool_key, the MCP inline path the called tool,
    the tool-agnostic ticket PUT a fixed marker — one user's ref is
    unique PER scope, not globally.

    Returns (receipt_row, usage_ids, replay_result_or_None).
    Raises IngressConflict / IngressInProgress / IngressQuotaDenied.
    A quota denial creates neither receipt nor usage rows.
    """
    from governance.entitlements import resolve as _resolve_entitlement
    from sharing.uploads import _quota_denial
    # Canonicalize + validate the request identity HERE — the single
    # entry point every surface (web/v1/MCP) funnels through. Bad
    # identity fails loud before any lock/read/insert.
    if not isinstance(request_ref, str):
        raise ValueError("invalid_request_ref")
    request_ref = request_ref.strip()
    if not request_ref or len(request_ref) > 128:
        raise ValueError("invalid_request_ref")
    _HEX = set("0123456789abcdef")
    if (not isinstance(request_hash, str) or len(request_hash) != 64
            or any(c not in _HEX for c in request_hash)):
        raise ValueError("invalid_request_hash")
    clean = [int(s) for s in sizes]
    if not clean or any(s <= 0 for s in clean):
        raise IngressQuotaDenied({"error": "err_empty_file"})
    start = now() - 24 * 3600
    ts = now()
    ent = _resolve_entitlement(user_id)
    with db() as con:
        # repo.claim locks the user row FIRST, then checks the earliest
        # receipt binding — but the binding check must precede the quota
        # usage insert, so the usage insert is driven from here between
        # the two repo steps. Simplest correct composition: lock user,
        # read binding, decide quota, insert usage, insert receipt — all
        # in THIS transaction via repo primitives.
        usage_repo.lock_user(con, user_id)
        hist = _repo.earliest_by_ref(con, user_id, request_ref, scope)
        if hist is not None and hist["request_hash"] != request_hash:
            raise IngressConflict(dict(hist))
        if hist is not None and hist["status"] == "ok":
            # payload validity has ONE authority: _decode_durable_result.
            # No truthiness pre-check — an ok receipt with an empty or
            # otherwise unresolvable payload resolves UNRESOLVED here,
            # never a silent in-progress/conflict fallback.
            return None, [], _decode_durable_result(hist["result_json"])
        if hist is not None and hist["status"] == "running":
            raise IngressInProgress(dict(hist))
        if hist is not None:
            # earliest is failed/insufficient — a LATER ok receipt may
            # still own the live slot (failed-then-succeeded history):
            # replay it instead of re-claiming under the same ref.
            live = _repo.live_by_ref(con, user_id, request_ref, scope)
            if live is not None:
                if live["status"] == "ok":
                    return None, [], _decode_durable_result(live["result_json"])
                raise IngressInProgress(dict(live))
        row = usage_repo.user_usage(con, user_id, start)
        denial = _quota_denial(row, ent, sum(clean), len(clean))
        if denial:
            raise IngressQuotaDenied(denial)
        usage_ids = usage_repo.insert_many(con, user_id, ip, clean, ts)
        receipt = _repo.insert_running(con, user_id, request_ref, scope,
                                       request_hash, list(usage_ids), ts)
        if receipt is None:
            # lost a live-unique race inside the same locked transaction —
            # impossible for a different hash (binding above), only a same
            # hash concurrent claim; treat as in-progress.
            prior = _repo.live_by_ref(con, user_id, request_ref, scope)
            raise IngressInProgress(dict(prior) if prior else {})
        return dict(receipt), list(usage_ids), None


def finalize_ok(con, ingress_id: int,
                canonical_result: list[dict] | dict) -> dict:
    """Canonical receipt finalize under the CALLER's transaction: receipt
    running → ok + exact canonical JSON response. The caller commits —
    for direct-share this lands uploads rows + receipt-ok in ONE commit;
    for tool-source the caller opens its own `with db() as con:`.

    FAIL LOUD: if the receipt is no longer running (already settled by
    recovery, a concurrent finalize, or a lost-ack retry), the guarded
    UPDATE matches zero rows and we raise instead of letting the caller
    treat a non-transition as success — the route-level reconciliation
    then decides from the durable outcome."""
    row = _repo.finalize_ok(
        con, ingress_id, _json.dumps(canonical_result,
                                     ensure_ascii=False), now())
    if row is None:
        raise RuntimeError("ingress_request_not_running")
    return row


def _decode_durable_result(raw) -> list | dict:
    """Single fail-closed decoder for a receipt's stored result.

    A durable-ok replay payload must be a JSON object or array — a
    NULL/malformed/scalar stored value proves nothing about what the
    caller originally received, so it resolves UNRESOLVED instead of
    leaking a JSONDecodeError or fabricating a success."""
    if not isinstance(raw, str):
        raise IngressUnresolved("err_ingress_unresolved")
    try:
        parsed = _json.loads(raw)
    except Exception:
        raise IngressUnresolved("err_ingress_unresolved") from None
    if not isinstance(parsed, (dict, list)):
        raise IngressUnresolved("err_ingress_unresolved")
    return parsed


def settle_failure(ingress_id: int, usage_ids: list[int],
                   user_id: int) -> bool:
    """Failure settlement, ONE transaction, ORDER MATTERS:

    1. guarded transition receipt running → failed (row-level; a
       receipt already ok is NEVER rewritten to failed — its quota is
       durable and stays);
    2. only if the guard matched, delete the EXACT usage ids.

    If the receipt was already terminal the usage rows are NOT
    touched: an ok receipt's quota is durable (no speculative
    release), and an already-failed receipt released its usage at its
    own settlement (deleting again would be a no-op at best and a
    double-release bug at worst). Best-effort staged Gokapi cleanup
    happens in the caller BEFORE this.
    Returns True when this call performed the settlement."""
    if not usage_ids:
        usage_ids = []
    with db() as con:
        settled = _repo.settle_failed(con, ingress_id, now())
        if settled is None:
            return False
        usage_repo.delete_ids(con, user_id, usage_ids)
        return True


OUTCOME_DURABLE_OK = "durable_ok"
OUTCOME_PROVEN_ROLLBACK = "proven_rollback"
OUTCOME_UNRESOLVED = "unresolved"


def resolve_outcome(ingress_id: int) -> dict:
    """Strict tri-state for a finalize whose COMMIT ack was lost.

    ok + parseable result_json → DURABLE_OK (return stored response; no
    Gokapi delete, no quota release). running → PROVEN_ROLLBACK (the
    ONLY outcome allowed to clean up staged objects and release exact
    usage ids). Anything else — missing row, failed, ok with malformed
    payload, DB unavailable — UNRESOLVED: no release, no delete, no
    fabricated success; the durable recovery owns the disposition.
    """
    try:
        with db() as con:
            row = _repo.by_id_for_update(con, ingress_id)
    except Exception:
        logger.exception("ingress outcome read failed id=%s", ingress_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    if row is None:
        return {"outcome": OUTCOME_UNRESOLVED}
    if row["status"] == "running":
        return {"outcome": OUTCOME_PROVEN_ROLLBACK, "row": dict(row)}
    if row["status"] == "ok":
        try:
            stored = _decode_durable_result(row["result_json"])
        except IngressUnresolved:
            logger.exception("ingress ok receipt payload unresolvable id=%s",
                             ingress_id)
            return {"outcome": OUTCOME_UNRESOLVED}
        return {"outcome": OUTCOME_DURABLE_OK, "row": dict(row),
                "result": stored}
    return {"outcome": OUTCOME_UNRESOLVED}


def recover_abandoned_ingress_requests() -> dict:
    """Startup (single-worker): every running receipt settled to failed
    with its EXACT usage ids released in one transaction per receipt.
    No Gokapi deletion — the receipt never held durable object-id
    authority; physical orphans carry no public mapping and expire by
    the existing Gokapi TTL."""
    recovered = 0
    with db() as con:
        rows = _repo.running_receipts(con)
    for r in rows:
        with db() as con:
            live = _repo.by_id_for_update(con, int(r["id"]))
            if live is None or live["status"] != "running":
                continue
            usage_repo.delete_ids(con, int(r["user_id"]),
                                  [int(u) for u in r["usage_ids"] or []])
            _repo.settle_failed(con, int(r["id"]), now())
            recovered += 1
    return {"scanned": len(rows), "recovered": recovered}
