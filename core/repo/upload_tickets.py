"""repo: mcp_upload_tickets — one-time PUT credentials for the MCP
two-stage upload (B4b).

The ticket is the credential: bound to user_id + declared_size + a short
expiry, consumed exactly once. All state transitions are single atomic
statements — no read-then-write windows.
"""
import secrets

from database import db

TICKET_TTL_S = 10 * 60


def _hash_for(filename: str, content_type: str | None, size: int,
              client_ref: str) -> str:
    """Idempotency identity of a create_upload call: the exact issued
    parameters (the ticket value and expiry are deliberately excluded)."""
    import hashlib
    import json
    payload = json.dumps({
        "filename": filename, "content_type": content_type or "",
        "size": int(size), "client_ref": client_ref,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def issue(user_id: int, filename: str, content_type: str | None,
          size: int, client_ref: str, now_ts: int) -> dict:
    """Create (or idempotently return) one usable ticket.

    Same (user, client_ref) + same parameters → the EXISTING unused,
    unexpired ticket is returned (same credential). Same client_ref with
    different parameters raises ValueError('idempotency_conflict') — the
    caller maps it to the JSON-RPC reserved code. Used/expired tickets
    do not block: a fresh credential is issued for the same intent."""
    request_hash = _hash_for(filename, content_type, size, client_ref)
    with db() as con:
        row = con.execute(
            "SELECT * FROM mcp_upload_tickets"
            " WHERE user_id = %s AND client_ref = %s"
            " ORDER BY id DESC LIMIT 1", (user_id, client_ref)).fetchone()
        if row is not None:
            if row["request_hash"] != request_hash:
                raise ValueError("idempotency_conflict")
            if row["used_at"] is None and int(row["expires_at"]) > now_ts:
                return dict(row)
        ticket = secrets.token_urlsafe(32)
        row = con.execute(
            """INSERT INTO mcp_upload_tickets
               (ticket, user_id, filename, content_type, declared_size,
                client_ref, request_hash, expires_at, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (ticket, user_id, filename, content_type, int(size),
             client_ref, request_hash, now_ts + TICKET_TTL_S,
             now_ts)).fetchone()
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
