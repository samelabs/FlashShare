"""repo/ingress_requests — row persistence for ingress side-effect receipts.

Pure CRUD under a CALLER-supplied transaction: claim / read / finalize /
settle-failure rows. No quota business logic, no Gokapi calls, no hash
computation. Business orchestration lives in sharing.ingress_receipt.
"""

import json as _json

_HEX64 = set("0123456789abcdef")


def by_id(con, ingress_id: int) -> dict | None:
    row = con.execute(
        "SELECT * FROM ingress_requests WHERE id=%s", (ingress_id,)).fetchone()
    return dict(row) if row else None


def by_id_for_update(con, ingress_id: int) -> dict | None:
    """Row-level lock for outcome resolution / recovery arbitration."""
    row = con.execute(
        "SELECT * FROM ingress_requests WHERE id=%s FOR UPDATE",
        (ingress_id,)).fetchone()
    return dict(row) if row else None


def earliest_by_ref(con, user_id: int, request_ref: str,
                    scope: str = "") -> dict | None:
    """Earliest historical receipt for the (user_id, scope, request_ref)
    binding — the client_ref uniqueness scope is (user, tool, ref)."""
    row = con.execute(
        """
        SELECT id, request_hash, status, result_json
        FROM ingress_requests
        WHERE user_id=%s AND scope=%s AND request_ref=%s
        ORDER BY id ASC LIMIT 1
        """,
        (user_id, scope, request_ref)).fetchone()
    return dict(row) if row else None


def live_by_ref(con, user_id: int, request_ref: str,
                scope: str = "") -> dict | None:
    """Latest receipt still holding the live slot (running|ok) within
    the (user, scope) partition."""
    row = con.execute(
        """
        SELECT * FROM ingress_requests
        WHERE user_id=%s AND scope=%s AND request_ref=%s
          AND status IN ('running','ok')
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, scope, request_ref)).fetchone()
    return dict(row) if row else None


def insert_running(con, user_id: int, request_ref: str, scope: str,
                   request_hash: str, usage_ids: list[int],
                   started_at: int) -> dict | None:
    """INSERT running receipt under the partial live unique index on
    (user_id, scope, request_ref).

    ON CONFLICT ... DO NOTHING — a lost live-unique race returns None
    (row mapping only; race semantics belong to sharing.ingress_receipt)."""
    row = con.execute(
        """
        INSERT INTO ingress_requests (user_id, scope, request_ref,
           request_hash, status, usage_ids, started_at)
        VALUES (%s, %s, %s, %s, 'running', %s, %s)
        ON CONFLICT (user_id, scope, request_ref)
          WHERE status IN ('running','ok')
        DO NOTHING
        RETURNING *
        """,
        (user_id, scope, request_ref, request_hash, usage_ids,
         started_at)).fetchone()
    return dict(row) if row else None


def running_receipts(con) -> list[dict]:
    rows = con.execute(
        "SELECT id, user_id, usage_ids FROM ingress_requests"
        " WHERE status='running'").fetchall()
    return [dict(r) for r in rows]


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
