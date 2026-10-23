import time

from database import db
from governance.entitlements import resolve as _resolve_entitlement
from repo import uploads as uploads_repo
from repo import upload_usage as usage_repo
from storage import delete as storage_delete
from utils import now, public_file_url


def _quota_denial(row: dict, ent: dict, incoming_bytes: int,
                  incoming_count: int) -> dict | None:
    """Single quota decision point — the only constructor of structured
    rolling-quota denials.

    Priority keeps the existing semantics: files first, then bytes.
    remaining = max(limit - used, 0) measured BEFORE this request (the
    window's spare capacity at decision time), never the post-incoming
    remainder; incoming is never truncated to remaining.
    """
    used_files, used_bytes = int(row["c"]), int(row["b"])
    if used_files + incoming_count > ent["ingress_files_24h"]:
        limit = ent["ingress_files_24h"]
        return {"error": "err_ingress_files_24h", "limit": limit,
                "used": used_files, "incoming": incoming_count,
                "remaining": max(limit - used_files, 0),
                "plan_code": ent["plan_code"]}
    if used_bytes + incoming_bytes > ent["ingress_bytes_24h"]:
        limit = ent["ingress_bytes_24h"]
        return {"error": "err_ingress_bytes_24h", "limit": limit,
                "used": used_bytes, "incoming": incoming_bytes,
                "remaining": max(limit - used_bytes, 0),
                "plan_code": ent["plan_code"]}
    return None


def _usage_24h_row(user_id: int) -> dict:
    """Rolling-24h usage facts (single internal read helper — the same
    query check_quota previews and the public reader report; one window
    algorithm, no second copy)."""
    start = now() - 24 * 3600
    with db() as con:
        return usage_repo.user_usage(con, user_id, start)


def usage_24h(user_id: int) -> dict:
    """Public read-only rolling-24h ingress usage projection.

    Returns {"ingress_files_24h": n, "ingress_bytes_24h": n}. No
    reserve/insert/delete/lock and no IP-quota reads — pure facts for
    read models (access projection).
    """
    row = _usage_24h_row(user_id)
    return {"ingress_files_24h": int(row["c"]),
            "ingress_bytes_24h": int(row["b"])}


def check_quota(user_id: int, ip: str, incoming_bytes: int,
                incoming_count: int = 1) -> tuple:
    """Read-only quota preview against ingress facts.

    Limits come from the user's effective entitlement (governance.
    entitlements.resolve). ip is accepted for the audit-fact record but
    takes no part in quota decisions. Denials carry the full structured
    contract from _quota_denial — the same shape reserve_ingress emits.
    """
    ent = _resolve_entitlement(user_id)
    row = _usage_24h_row(user_id)
    denial = _quota_denial(row, ent, int(incoming_bytes), int(incoming_count))
    return (denial is None, denial)


def reserve_ingress(user_id: int, ip: str, sizes: list[int]) -> tuple:
    """Atomically reserve the rolling-24h source-file ingress budget.

    Same resolver, same limit semantics and same denial contract as
    check_quota (single _quota_denial); the decision and the insert both
    happen under the user row lock so same-user concurrency cannot
    exceed the entitlement.
    """
    clean = [int(s) for s in sizes]
    if not clean or any(s <= 0 for s in clean):
        return False, {"error": "err_empty_file"}, []
    start = now() - 24 * 3600
    ts = now()
    ent = _resolve_entitlement(user_id)
    with db() as con:
        usage_repo.lock_user(con, user_id)
        row = usage_repo.user_usage(con, user_id, start)
        denial = _quota_denial(row, ent, sum(clean), len(clean))
        if denial:
            return False, denial, []
        ids = usage_repo.insert_many(con, user_id, ip, clean, ts)
    return True, None, ids


def release_ingress(user_id: int, usage_ids: list[int]) -> None:
    if not usage_ids:
        return
    with db() as con:
        usage_repo.delete_ids(con, user_id, usage_ids)


def get_upload_by_public_id(public_id: str):
    """Raw owner/internal lookup; callers decide whether expiry is relevant."""
    with db() as con:
        return uploads_repo.by_public_id(con, public_id)


def get_live_upload_by_public_id(public_id: str):
    """Canonical read gate for serving a share/result to a consumer."""
    row = get_upload_by_public_id(public_id)
    if not row:
        return None
    if int(row["expire_at"] or 0) < now():
        return None
    return row


def live_status_for(gokapi_ids) -> dict:
    """Live Gokapi statuses for exactly these storage ids — one
    single-file query per id (bounded by the page size), stopping at
    the first unavailable hop: ids after it are simply absent from the
    map. Unavailability is never forged into an authoritative 'gone'
    (absent-from-map and confirmed-deleted stay distinguishable at the
    caller via the adapter's tuple contract)."""
    from gokapi import get_gokapi_file
    out = {}
    for gid in gokapi_ids:
        if not gid:
            continue
        ok, info = get_gokapi_file(gid, timeout=2)
        if not ok:
            break
        if info is not None:
            out[gid] = info
    return out


def list_uploads(user_id: int, offset: int = 0, limit: int = 20) -> list:
    with db() as con:
        rows = uploads_repo.by_owner(con, user_id, int(time.time()), limit, offset)
    gokapi_map = live_status_for([r["gokapi_id"] for r in rows])
    uploads = []
    for row in rows:
        item = dict(row)
        live = gokapi_map.get(item["gokapi_id"])
        if live:
            item["downloads_remaining"] = live.get("DownloadsRemaining")
            item["download_count"] = live.get("DownloadCount")
            exp = live.get("ExpireAt")
            try:
                item["expire_at"] = int(exp) if exp else item["expire_at"]
            except (TypeError, ValueError):
                pass
        item["url_download"] = public_file_url(item["public_id"])
        # Storage binding is server-side implementation detail, not an account
        # API contract. Keeping it out also prevents bypassing /f lifecycle.
        item.pop("gokapi_id", None)
        item.pop("url_hotlink", None)
        uploads.append(item)
    return uploads


def delete_upload(upload_id: int, user_id: int, audit=None) -> bool:
    """The ONE delete implementation: FOR UPDATE read + row delete (+ the
    optional operator audit callback) in a single transaction; the Gokapi
    physical delete runs AFTER commit and only warns on failure (natural
    TTL is the backstop).

    audit(con, row) — called inside the transaction, after the DELETE, so
    the audit fact and the mapping-row deletion commit or roll back
    together. `row` is the locked pre-delete snapshot (owner-scoped).
    Returns False when the row is absent (delete-by-absence converged).
    """
    with db() as con:
        row = uploads_repo.owned_row_for_update(con, upload_id, user_id)
        if not row:
            return False
        uploads_repo.delete_upload_row(con, upload_id, user_id)
        if audit is not None:
            audit(con, dict(row))
    try:
        storage_delete(row["gokapi_id"])
    except Exception:
        import logging
        logging.getLogger("flashshare").warning(
            "gokapi delete failed for %s (will expire naturally)", row["gokapi_id"])
    return True
