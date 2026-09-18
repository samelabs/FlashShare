"""repo/ingress_requests — row persistence for ingress side-effect receipts.

Pure CRUD under a CALLER-supplied transaction: claim / read / finalize /
settle-failure rows. No quota business logic, no Gokapi calls, no hash
computation. Business orchestration lives in sharing.ingress_receipt.
"""

import json as _json

_HEX64 = set("0123456789abcdef")


def _valid_ref(ref) -> bool:
    return (isinstance(ref, str) and bool(ref.strip())
            and len(ref.strip()) <= 128)


def _valid_hash(h) -> bool:
    return (isinstance(h, str) and len(h) == 64
            and all(c in _HEX64 for c in h))


def _claim_primitives_note():
    """claim composition (user lock → binding → usage insert → receipt
    insert, one transaction) lives in sharing.ingress_receipt
    .claim_ingress_quota — the repo keeps only row primitives."""


def by_id(con, ingress_id: int) -> dict | None:
    row = con.execute(
        "SELECT * FROM ingress_requests WHERE id=%s", (ingress_id,)).fetchone()
    return dict(row) if row else None


def finalize_ok(con, ingress_id: int, result_json: str,
                finished_at: int) -> dict:
    """Success finalize — RUNNING → ok only, one UPDATE.

    A receipt already terminal (failed/ok) can never be re-finalized:
    RETURNING is empty and the caller gets None — a terminal rewrite
    (e.g. overwriting a settled failure with ok, or double-finalize
    racing recovery) is refused at the row level."""
    row = con.execute(
        """
        UPDATE ingress_requests
        SET status='ok', result_json=%s, finished_at=%s
        WHERE id=%s AND status='running'
        RETURNING *
        """,
        (result_json, finished_at, ingress_id)).fetchone()
    return dict(row) if row else None


def settle_failed(con, ingress_id: int, finished_at: int) -> dict:
    """Failure settlement — RUNNING → failed only, one UPDATE.

    Same guard as finalize_ok: a receipt already ok must never be
    rewritten to failed (that would mark a durable success as failed);
    already-failed stays idempotently untouched (None returned)."""
    row = con.execute(
        """
        UPDATE ingress_requests
        SET status='failed', result_json=NULL, finished_at=%s
        WHERE id=%s AND status='running'
        RETURNING *
        """,
        (finished_at, ingress_id)).fetchone()
    return dict(row) if row else None
