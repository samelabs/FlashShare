"""repo: mcp_upload_tickets — one-time PUT credentials for the MCP
two-stage upload (B4b).

The ticket is the credential: bound to user_id + declared_size + a short
expiry, consumed exactly once. All state transitions are single atomic
statements — no read-then-write windows.
"""
import secrets

from database import db

TICKET_TTL_S = 10 * 60


def issue(user_id: int, filename: str, content_type: str | None,
          size: int, client_ref: str, now_ts: int) -> dict:
    """Create (or idempotently return) one usable ticket.

    Same (user, client_ref) + same parameters → the EXISTING unused,
    unexpired ticket is returned (same credential). Same client_ref with
    different parameters raises ValueError('idempotency_conflict') — the
    caller maps it to the JSON-RPC reserved code. Used/expired tickets
    do not block: a fresh credential is issued for the same intent.

    Serialized on the SAME user-row anchor the tool-call claim and the
    ingress receipt claim use: the lookup + insert pair has no
    read-then-write window, so concurrent create_upload calls for one
    client_ref cannot mint two simultaneously-live tickets (backed by
    the partial unique index on live tickets)."""
    with db() as con:
        from repo.ledger import lock_user
        lock_user(con, user_id)
        row = con.execute(
            "SELECT * FROM mcp_upload_tickets"
            " WHERE user_id = %s AND client_ref = %s"
            " ORDER BY id DESC LIMIT 1", (user_id, client_ref)).fetchone()
        if row is not None:
            _same = (row["filename"] == filename
                     and (row["content_type"] or None) == (content_type or None)
                     and int(row["declared_size"]) == int(size))
            if not _same:
                raise ValueError("idempotency_conflict")
            if row["used_at"] is None and int(row["expires_at"]) > now_ts:
                return dict(row)
            if row["used_at"] is None:
                # expired-unused: clear it so the partial unique index
                # (one unused row per user+ref) admits the fresh ticket;
                # housekeeping would have purged it anyway
                con.execute(
                    "DELETE FROM mcp_upload_tickets WHERE id = %s",
                    (row["id"],))
        ticket = secrets.token_urlsafe(32)
        row = con.execute(
            """INSERT INTO mcp_upload_tickets
               (ticket, user_id, filename, content_type, declared_size,
                client_ref, expires_at, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (ticket, user_id, filename, content_type, int(size),
             client_ref, now_ts + TICKET_TTL_S, now_ts)).fetchone()
        return dict(row)


def owner_of(ticket: str) -> int | None:
    """Read-only ticket → user_id (no state change; identity precheck)."""
    with db() as con:
        row = con.execute(
            "SELECT user_id FROM mcp_upload_tickets WHERE ticket = %s",
            (ticket,)).fetchone()
    return int(row["user_id"]) if row else None


def claim(ticket: str, now_ts: int) -> dict | str:
    """Atomically consume a ticket: the single UPDATE flips used_at, so
    concurrent PUTs cannot both win.

    Returns the claimed row dict, or a typed reason string:
    'not_found' | 'used' | 'expired'."""
    with db() as con:
        row = con.execute(
            "UPDATE mcp_upload_tickets SET used_at = %s"
            " WHERE ticket = %s AND used_at IS NULL AND expires_at > %s"
            " RETURNING *", (now_ts, ticket, now_ts)).fetchone()
        if row is not None:
            return dict(row)
    with db() as con:
        row = con.execute(
            "SELECT used_at, expires_at FROM mcp_upload_tickets"
            " WHERE ticket = %s", (ticket,)).fetchone()
    if row is None:
        return "not_found"
    if row["used_at"] is not None:
        return "used"
    if int(row["expires_at"]) <= now_ts:
        return "expired"
    return "used"  # lost the race — the winner consumed it


def purge(ts: int) -> int:
    """Housekeeping purge: (a) expired AND never used, (b) used more
    than 24h ago (the audit/replay window has closed). Returns the
    number of rows deleted."""
    with db() as con:
        cur = con.execute(
            """
            DELETE FROM mcp_upload_tickets
            WHERE (used_at IS NULL AND expires_at < %s)
               OR (used_at IS NOT NULL AND used_at < %s)
            """,
            (ts, ts - 24 * 3600))
        return cur.rowcount
