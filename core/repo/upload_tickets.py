"""repo: mcp_upload_tickets — one-time PUT credentials for the MCP
two-stage upload (B4b).

The ticket VALUE is the credential but is never stored: only its SHA-256
hash persists (ticket_hash). The plaintext exists exactly once — in the
create_upload response — and a same-client_ref retry INVALIDATES the old
credential and mints a fresh one (the old plaintext can never be
re-served from storage). Lookup paths hash the presented value.
"""
import hashlib
import secrets

from database import db

TICKET_TTL_S = 10 * 60


def _thash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def issue(user_id: int, filename: str, content_type: str | None,
          size: int, client_ref: str, now_ts: int) -> dict:
    """Create one usable ticket (hash-persisted).

    Same (user, client_ref) + same parameters → the prior unused
    credential is INVALIDATED and a FRESH one minted (the old plaintext
    is unrecoverable from storage by design). Same client_ref with
    different parameters raises ValueError('idempotency_conflict') — the
    caller maps it to the JSON-RPC reserved code. Used/expired tickets
    do not block: a fresh credential is issued for the same intent.

    Serialized on the SAME user-row anchor the tool-call claim and the
    ingress receipt claim use: the lookup + invalidate + insert pair has
    no read-then-write window, so concurrent create_upload calls for one
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
            if row["used_at"] is None:
                # live or expired-unused prior credential: invalidate so
                # the partial unique index (one unused row per user+ref)
                # admits the fresh ticket — the old plaintext is dead
                con.execute(
                    "DELETE FROM mcp_upload_tickets WHERE id = %s",
                    (row["id"],))
        ticket = secrets.token_urlsafe(32)
        row = con.execute(
            """INSERT INTO mcp_upload_tickets
               (ticket_hash, user_id, filename, content_type, declared_size,
                client_ref, expires_at, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (_thash(ticket), user_id, filename, content_type, int(size),
             client_ref, now_ts + TICKET_TTL_S, now_ts)).fetchone()
        out = dict(row)
        out["ticket"] = ticket  # plaintext exactly once, to the response
        return out


def owner_of(ticket: str) -> int | None:
    """Read-only ticket → user_id (no state change; identity precheck)."""
    with db() as con:
        row = con.execute(
            "SELECT user_id FROM mcp_upload_tickets WHERE ticket_hash = %s",
            (_thash(ticket),)).fetchone()
    return int(row["user_id"]) if row else None


def claim(ticket: str, now_ts: int) -> dict | str:
    """Atomically consume a ticket: the single UPDATE flips used_at, so
    concurrent PUTs cannot both win.

    Returns the claimed row dict, or a typed reason string:
    'not_found' | 'used' | 'expired'."""
    with db() as con:
        row = con.execute(
            "UPDATE mcp_upload_tickets SET used_at = %s"
            " WHERE ticket_hash = %s AND used_at IS NULL AND expires_at > %s"
            " RETURNING *", (now_ts, _thash(ticket), now_ts)).fetchone()
        if row is not None:
            return dict(row)
    with db() as con:
        row = con.execute(
            "SELECT used_at, expires_at FROM mcp_upload_tickets"
            " WHERE ticket_hash = %s", (_thash(ticket),)).fetchone()
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
