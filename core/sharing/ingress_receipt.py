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
                        request_hash: str, sizes: list[int]):
    """ONE transaction: user lock → receipt binding check → quota read
    → quota decision → upload_usage INSERT → receipt INSERT(running).

    Returns (receipt_row, usage_ids, replay_result_or_None).
    Raises IngressConflict / IngressInProgress / IngressQuotaDenied.
    A quota denial creates neither receipt nor usage rows.
    """
    from governance.entitlements import resolve as _resolve_entitlement
    from sharing.uploads import _quota_denial
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
        con.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
        hist = con.execute(
            """
            SELECT id, request_hash, status, result_json
            FROM ingress_requests
            WHERE user_id=%s AND request_ref=%s
            ORDER BY id ASC LIMIT 1
            """,
            (user_id, request_ref)).fetchone()
        if hist is not None and hist["request_hash"] != request_hash:
            raise IngressConflict(dict(hist))
        if hist is not None and hist["status"] in ("running", "ok"):
            if hist["status"] == "ok" and hist["result_json"]:
                return None, [], _json.loads(hist["result_json"])
            raise IngressInProgress(dict(hist))
        if hist is not None:
            # earliest is failed/insufficient — a LATER ok receipt may
            # still own the live slot (failed-then-succeeded history):
            # replay it instead of re-claiming under the same ref.
            live = con.execute(
                """
                SELECT * FROM ingress_requests
                WHERE user_id=%s AND request_ref=%s
                  AND status IN ('running','ok')
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, request_ref)).fetchone()
            if live is not None:
                if live["status"] == "ok" and live["result_json"]:
                    return None, [], _json.loads(live["result_json"])
                raise IngressInProgress(dict(live))
        row = usage_repo.user_usage(con, user_id, start)
        denial = _quota_denial(row, ent, sum(clean), len(clean))
        if denial:
            raise IngressQuotaDenied(denial)
        usage_ids = usage_repo.insert_many(con, user_id, ip, clean, ts)
        receipt = con.execute(
            """
            INSERT INTO ingress_requests (user_id, request_ref, request_hash,
               status, usage_ids, started_at)
            VALUES (%s, %s, %s, 'running', %s, %s)
            ON CONFLICT (user_id, request_ref)
              WHERE status IN ('running','ok')
            DO NOTHING
            RETURNING *
            """,
            (user_id, request_ref.strip(), request_hash, list(usage_ids),
             ts)).fetchone()
        if receipt is None:
            # lost a live-unique race inside the same locked transaction —
            # impossible for a different hash (binding above), only a same
            # hash concurrent claim; treat as in-progress.
            prior = con.execute(
                """
                SELECT * FROM ingress_requests
                WHERE user_id=%s AND request_ref=%s
                  AND status IN ('running','ok')
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, request_ref)).fetchone()
            raise IngressInProgress(dict(prior) if prior else {})
        return dict(receipt), list(usage_ids), None


def finalize_ok(ingress_id: int, canonical_result: list[dict] | dict) -> dict:
    """ONE transaction: receipt running → ok + exact canonical response.
    The HTTP response is returned only after this commits."""
    with db() as con:
        row = _repo.finalize_ok(
            con, ingress_id, _json.dumps(canonical_result,
                                         ensure_ascii=False), now())
    return row


def settle_failure(ingress_id: int, usage_ids: list[int],
                   user_id: int) -> None:
    """Failure settlement: release EXACT usage ids + receipt failed in
    one transaction. Best-effort staged Gokapi cleanup happens in the
    caller BEFORE this — physical objects are never this table's
    authority."""
    if not usage_ids:
        usage_ids = []
    with db() as con:
        usage_repo.delete_ids(con, user_id, usage_ids)
        _repo.settle_failed(con, ingress_id, now())


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
            row = con.execute(
                "SELECT * FROM ingress_requests WHERE id=%s FOR UPDATE",
                (ingress_id,)).fetchone()
    except Exception:
        logger.exception("ingress outcome read failed id=%s", ingress_id)
        return {"outcome": OUTCOME_UNRESOLVED}
    if row is None:
        return {"outcome": OUTCOME_UNRESOLVED}
    if row["status"] == "running":
        return {"outcome": OUTCOME_PROVEN_ROLLBACK, "row": dict(row)}
    if row["status"] == "ok":
        try:
            stored = _json.loads(row["result_json"])
        except Exception:
            logger.exception("ingress ok receipt malformed id=%s", ingress_id)
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
        rows = con.execute(
            "SELECT id, user_id, usage_ids FROM ingress_requests"
            " WHERE status='running'").fetchall()
    for r in rows:
        with db() as con:
            live = con.execute(
                "SELECT status FROM ingress_requests WHERE id=%s"
                " FOR UPDATE", (r["id"],)).fetchone()
            if live is None or live["status"] != "running":
                continue
            usage_repo.delete_ids(con, int(r["user_id"]),
                                  [int(u) for u in r["usage_ids"] or []])
            _repo.settle_failed(con, int(r["id"]), now())
            recovered += 1
    return {"scanned": len(rows), "recovered": recovered}
