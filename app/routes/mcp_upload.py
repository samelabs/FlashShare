"""/mcp/upload/{ticket} — the PUT leg of the MCP two-stage upload (B4b).

The ticket itself is the ONLY credential (no Bearer, no session): it was
issued by the create_upload MCP tool, is bound to user_id + declared
size + a 10-minute expiry, and is consumed exactly once by the atomic
claim in repo.upload_tickets.

Success runs the SAME ingress path as an inline-base64 source on
tools/call: ingress receipt (quota usage) → Gokapi staging → receipt
finalize; the response carries {public_id, source_token} for the
files[] reference form. Idempotency is client_ref + file identity
(NOT the ticket): a failed/expired ticket can be re-issued and the
PUT retried; an already-successful upload replays the same handles.
"""
import asyncio
import io
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import BASE_URL, MAX_UPLOAD_BYTES
from database import db
from app.web_auth import client_ip

router = APIRouter()
logger = logging.getLogger("flashshare")

_READ_CHUNK = 256 * 1024


def _err(status: int, code: str, **extra) -> JSONResponse:
    body = {"ok": False, "error": code}
    body.update(extra)
    return JSONResponse(body, status_code=status)


@router.put("/mcp/upload/{ticket}")
async def mcp_upload_put(ticket: str, request: Request):
    from utils import now
    ts = now()
    from repo import upload_tickets as tickets

    # A ticket is credential-by-possession, but an EXPLICIT identity that
    # belongs to someone else is a hard reject BEFORE the ticket is
    # consumed (a mismatched attempt must never burn it).
    auth = request.headers.get("authorization", "")
    bearer_uid = None
    if auth.lower().startswith("bearer "):
        from identity.apikeys import authenticate_key
        other = authenticate_key(auth[7:].strip())
        if other is not None:
            bearer_uid = int(other["id"])
    if bearer_uid is not None:
        owner = tickets.owner_of(ticket)
        if owner is None:
            return _err(404, "ticket_not_found")
        if owner != bearer_uid:
            return _err(403, "ticket_owner_mismatch")

    claimed = tickets.claim(ticket, ts)
    if claimed == "not_found":
        return _err(404, "ticket_not_found")
    if claimed == "used":
        return _err(409, "ticket_used")
    if claimed == "expired":
        return _err(410, "ticket_expired")
    row = claimed
    user_id = int(row["user_id"])

    # ── stream the body under the declared cap ──
    # The cap is the ticket's declared size (≤ the platform per-file cap,
    # enforced at issue time). One extra byte is read on purpose: any
    # overflow aborts immediately without storing anything.
    cap = min(int(row["declared_size"]), MAX_UPLOAD_BYTES)
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > cap:
            return _err(413, "err_file_too_large", limit=cap,
                        incoming=len(buf))
    body = bytes(buf)
    if not body:
        return _err(400, "err_empty_file")

    # ── same sniff/format gate as every other ingress surface ──
    from utils import sanitize_filename
    from security import sniff_allowed, mismatch_detail
    filename = sanitize_filename(row["filename"] or "upload")
    ok, final_type, final_name = sniff_allowed(filename,
                                               row["content_type"] or "",
                                               body[:512])
    if not ok:
        return _err(415, final_type, **mismatch_detail(filename, body[:512]))
    # same-family admission may re-identify the file; storage, ingress
    # hash and every downstream fact use the final name
    filename = final_name

    ip = client_ip(request)

    # ── ingress path identical to the inline-base64 MCP source ──
    from sharing.ingress_identity import ingress_request_hash
    from sharing.ingress_receipt import (
        claim_ingress_quota, finalize_ok as receipt_ok,
        settle_failure as receipt_fail,
        IngressConflict, IngressInProgress, IngressQuotaDenied,
        IngressUnresolved)
    import hashlib
    # Idempotency identity = client_ref + the FILE (name/type/size/digest)
    # — deliberately NOT the ticket id: a re-issued ticket after a failed
    # or expired one must hash identically so the ingress binding treats
    # it as the same logical upload (re-claim after failure, replay after
    # success) instead of a conflicting one.
    req_hash = ingress_request_hash(
        "mcp_upload_ticket",
        [{"filename": filename, "content_type": final_type,
          "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}],
        {})  # staging is tool-agnostic: scope stays '' (ref unique per user)
    try:
        receipt, usage_ids, replay = claim_ingress_quota(
            user_id, ip, row["client_ref"], req_hash, [len(body)], scope="")
    except IngressQuotaDenied as exc:
        return JSONResponse({"ok": False, **exc.denial}, 429)
    except IngressConflict:
        return _err(409, "idempotency_conflict")
    except IngressInProgress:
        return _err(409, "err_upload_in_progress")
    except IngressUnresolved:
        return _err(500, "err_ingress_unresolved")

    if replay is not None:
        info = replay if isinstance(replay, dict) else (
            replay[0] if isinstance(replay, list) and replay else None)
        if isinstance(info, dict) and info.get("stagingId"):
            return {"public_id": info["stagingId"],
                    "source_token": info.get("sourceToken", ""),
                    "expires_at": info.get("expireAt")}

    from governance.delivery import (INTERMEDIATE_TTL_DAYS,
                                     INTERMEDIATE_DOWNLOAD_LIMIT)
    from storage import write as storage_write, delete as storage_delete
    from security import sign_source_token
    gid = ""
    try:
        gokapi = await asyncio.to_thread(
            storage_write, io.BytesIO(body), filename, final_type,
            INTERMEDIATE_TTL_DAYS,
            INTERMEDIATE_DOWNLOAD_LIMIT, len(body))
        info = gokapi.get("FileInfo", {})
        gid = info.get("Id", "")
        if not gid:
            raise RuntimeError("gokapi_missing_id")
        file_info = {
            "name": filename, "filename": filename,
            "contentType": final_type, "size": len(body),
            "stagingId": gid,
            "sourceToken": sign_source_token(user_id, gid),
            "expireAt": info.get("ExpireAt"),
            "expiryDays": INTERMEDIATE_TTL_DAYS,
            "allowedDownloads": INTERMEDIATE_DOWNLOAD_LIMIT,
        }
    except Exception:
        logger.exception("mcp ticket staging failed ticket_id=%s", row["id"])
        if gid:
            try:
                await asyncio.to_thread(storage_delete, gid)
            except Exception:
                pass
        try:
            receipt_fail(int(receipt["id"]), usage_ids, user_id)
        except Exception:
            pass
        return _err(500, "err_upload_failed")

    try:
        with db() as con:
            receipt_ok(con, int(receipt["id"]), [file_info])
    except Exception:
        # Finalize loss settles the receipt FAILED (the staged object is
        # already durable but unreferenced): a retry with the same
        # client_ref — via a NEW ticket, which now hashes identically —
        # re-claims cleanly instead of conflicting.
        logger.exception("mcp ticket finalize failed ticket_id=%s", row["id"])
        try:
            receipt_fail(int(receipt["id"]), usage_ids, user_id)
        except Exception:
            pass
        return _err(500, "err_upload_failed")

    return {"public_id": gid, "source_token": file_info["sourceToken"],
            "expires_at": info.get("ExpireAt")}
