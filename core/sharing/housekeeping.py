"""Periodic resource lifecycle reconciliation for 518 shares/results.

Gokapi owns physical-byte expiry and download-count exhaustion. 518 owns the
public-id mapping and quota facts. This module closes those two lifecycles
without inventing another scheduler or a second source of truth.
"""

from database import db
from gokapi import list_gokapi_files
from repo import uploads as uploads_repo
from repo import upload_usage as usage_repo
from repo import upload_tickets as tickets_repo
from utils import now


USAGE_RETENTION_SECONDS = 24 * 3600
TICKET_USED_RETENTION_SECONDS = 24 * 3600


def housekeeping_once() -> dict:
    """Converge local lifecycle state with time and authoritative storage.

    Storage absence is acted on ONLY after a successful Gokapi list response.
    Network/auth/JSON failures never become destructive evidence.
    """
    ts = now()
    with db() as con:
        expired = uploads_repo.purge_expired(con, ts)
        usage = usage_repo.purge_before(con, ts - USAGE_RETENTION_SECONDS)
    # one-time PUT credentials: expired-unused immediately, used after
    # the 24h audit window (same cadence as upload_usage retention)
    tickets = tickets_repo.purge(ts)
    # receipts stuck running by an in-process settlement failure wedged
    # their (user, scope, client_ref) until now — settle anything older
    # than 30 minutes (fresh claims are left alone)
    from sharing.ingress_receipt import recover_abandoned_ingress_requests
    receipts = recover_abandoned_ingress_requests(older_than_s=1800)
    # OAuth channel hygiene: expired authorization codes and tokens
    # past their refresh lifetime (revoked included) leave the tables
    with db() as con:
        from repo import oauth_provider as _oauth_repo
        _oauth_repo.delete_expired(con, ts)

    reconciled = 0
    # Order matters: read the local row set FIRST, snapshot Gokapi SECOND.
    # A share lands as Gokapi-write (W) then uploads-row insert (R); if the
    # snapshot were taken before R, a landing in between (T0 < W < R < read)
    # would present a gokapi_id absent from the snapshot and the fresh row
    # would be mistaken for stale storage. With read-first, every row in the
    # comparison set predates the snapshot — its absence there is real
    # evidence the physical file is gone.
    with db() as con:
        local = uploads_repo.active_storage_rows(con, ts)
    storage_ok, files = list_gokapi_files(timeout=60)
    if storage_ok:
        live_ids = {
            str(item.get("Id"))
            for item in files
            if isinstance(item, dict) and item.get("Id")
        }
        stale_ids = [
            int(row["id"])
            for row in local
            if row["gokapi_id"] and str(row["gokapi_id"]) not in live_ids
        ]
        if stale_ids:
            with db() as con:
                reconciled = uploads_repo.delete_rows_by_ids(con, stale_ids)

    return {
        "expired": expired,
        "usage_purged": usage,
        "tickets_purged": tickets,
        "receipts_recovered": receipts.get("recovered", 0),
        "storage_checked": storage_ok,
        "storage_reconciled": reconciled,
    }
