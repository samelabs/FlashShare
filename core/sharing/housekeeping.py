"""Periodic resource lifecycle reconciliation for 518 shares/results.

Gokapi owns physical-byte expiry and download-count exhaustion. 518 owns the
public-id mapping and quota facts. This module closes those two lifecycles
without inventing another scheduler or a second source of truth.
"""

from database import db
from gokapi import list_gokapi_files
from repo import uploads as uploads_repo
from repo import upload_usage as usage_repo
from utils import now


USAGE_RETENTION_SECONDS = 24 * 3600


def housekeeping_once() -> dict:
    """Converge local lifecycle state with time and authoritative storage.

    Storage absence is acted on ONLY after a successful Gokapi list response.
    Network/auth/JSON failures never become destructive evidence.
    """
    ts = now()
    with db() as con:
        expired = uploads_repo.purge_expired(con, ts)
        usage = usage_repo.purge_before(con, ts - USAGE_RETENTION_SECONDS)

    storage_ok, files = list_gokapi_files()
    reconciled = 0
    if storage_ok:
        live_ids = {
            str(item.get("Id"))
            for item in files
            if isinstance(item, dict) and item.get("Id")
        }
        with db() as con:
            local = uploads_repo.active_storage_rows(con, ts)
            stale_ids = [
                int(row["id"])
                for row in local
                if row["gokapi_id"] and str(row["gokapi_id"]) not in live_ids
            ]
            reconciled = uploads_repo.delete_rows_by_ids(con, stale_ids)

    return {
        "expired": expired,
        "usage_purged": usage,
        "storage_checked": storage_ok,
        "storage_reconciled": reconciled,
    }
