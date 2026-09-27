"""repo/payment_reviews — partial-refund operator review queue.

One row per provider webhook EVENT that needs a human decision (today:
refund.created against a partially_refunded transaction). The queue is
purely a worklist: creating/resolving rows NEVER moves balances — the
economic rules for partial refunds stay "zero Credits, zero Membership".
Idempotency: unique (provider, event_id) — a replayed webhook converges
to the existing row instead of duplicating it.
"""
from typing import Optional

import json

from database import db  # noqa: F401  (module convention)


def insert_review(con, event_type: str, event_id: str, txn_id: str,
                  detail: dict, ts: int) -> Optional[int]:
    """Insert one review row; returns the row id, or None when the event
    was already queued (replay). NEVER raises on duplicate."""
    row = con.execute(
        """
        INSERT INTO payment_reviews
          (event_type, event_id, txn_id, detail_json, created_at)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (provider, event_id) DO NOTHING
        RETURNING id
        """,
        (event_type, event_id, txn_id,
         json.dumps(detail, ensure_ascii=False), ts)
    ).fetchone()
    return int(row["id"]) if row else None


def reviews_page(con, limit: int = 50, offset: int = 0,
                 status: str | None = None) -> tuple[list, int]:
    """Operator queue (paged, open first). Returns (rows, total)."""
    rows = con.execute(
        """
        SELECT * FROM payment_reviews
        WHERE (%s::text IS NULL OR status = %s::text)
        ORDER BY (status = 'open') DESC, id DESC
        LIMIT %s OFFSET %s
        """,
        (status, status, limit, offset)).fetchall()
    total = con.execute(
        """
        SELECT COUNT(*) AS n FROM payment_reviews
        WHERE (%s::text IS NULL OR status = %s::text)
        """,
        (status, status)).fetchone()["n"]
    return rows, int(total)


def review_for_update(con, review_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM payment_reviews WHERE id = %s FOR UPDATE",
        (review_id,)).fetchone()


def mark_review_resolved(con, review_id: int, ts: int,
                         admin_user_id: int) -> Optional[dict]:
    return con.execute(
        """
        UPDATE payment_reviews SET status = 'resolved',
           resolved_at = %s, resolved_by = %s
        WHERE id = %s RETURNING *
        """,
        (ts, admin_user_id, review_id)).fetchone()
