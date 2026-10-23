"""/mcp/upload/{ticket} — the PUT leg of the MCP two-stage upload (B4b).

The ticket itself is the ONLY credential (no Bearer, no session): it was
issued by the create_upload MCP tool, is bound to user_id + declared
size + a 10-minute expiry, and is consumed exactly once by the atomic
claim in repo.upload_tickets.

Success runs the SAME ingress path as an inline-base64 source on
tools/call: ingress receipt (quota usage) → Gokapi staging → receipt
finalize — through the ONE staging entry (sharing.staging.stage_sources,
P4 单点) whose finalize loss is reconciled by the strict tri-state
(ACK 丢失先核验: 已落地 → 按成功作答; 确认回滚 → 立即清理暂存对象 +
精确释放配额). The response carries {public_id, source_token} for the
files[] reference form. Idempotency is client_ref + file identity
(NOT the ticket): a failed/expired ticket can be re-issued and the
PUT retried; an already-successful upload replays the same handles.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import MAX_UPLOAD_BYTES
from app.web_auth import client_ip

router = APIRouter()
logger = logging.getLogger("flashshare")

_READ_CHUNK = 256 * 1024


def _err(status: int, code: str, **extra) -> JSONResponse:
    body = {"ok": False, "error": code}
    body.update(extra)
    return JSONResponse(body, status_code=status)


def _claim_ticket(ticket: str, ts, bearer_token: str | None):
    """Sync ticket segment (thread): optional bearer identity check +
    the atomic single-use claim. Returns (row, None) or (None, error)."""
    from repo import upload_tickets as tickets

    # A ticket is credential-by-possession, but an EXPLICIT identity that
    # belongs to someone else is a hard reject BEFORE the ticket is
    # consumed (a mismatched attempt must never burn it).
    if bearer_token is not None:
        from identity.apikeys import authenticate_key
        other = authenticate_key(bearer_token)
        if other is not None:
            bearer_uid = int(other["id"])
            owner = tickets.owner_of(ticket)
            if owner is None:
                return None, _err(404, "ticket_not_found")
            if owner != bearer_uid:
                return None, _err(403, "ticket_owner_mismatch")

    claimed = tickets.claim(ticket, ts)
    if claimed == "not_found":
        return None, _err(404, "ticket_not_found")
    if claimed == "used":
        return None, _err(409, "ticket_used")
    if claimed == "expired":
        return None, _err(410, "ticket_expired")
    return claimed, None


@router.put("/mcp/upload/{ticket}")
async def mcp_upload_put(ticket: str, request: Request):
    import asyncio
    from utils import now
    ts = now()

    auth = request.headers.get("authorization", "")
    bearer_token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    row, terr = await asyncio.to_thread(_claim_ticket, ticket, ts, bearer_token)
    if terr is not None:
        return terr
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

    # ── 文件暂存单点 (P4): claim → 暂存 → finalize 三态核验 ──
    # Idempotency identity = client_ref + the FILE (name/type/size/digest)
    # — deliberately NOT the ticket id: a re-issued ticket after a failed
    # or expired one must hash identically so the ingress binding treats
    # it as the same logical upload (re-claim after failure, replay after
    # success) instead of a conflicting one.
    from sharing.ingress_identity import ingress_request_hash
    from sharing.ingress_receipt import (
        IngressConflict, IngressInProgress, IngressQuotaDenied,
        IngressUnresolved)
    from sharing.staging import stage_sources, StagingError
    import hashlib
    req_hash = ingress_request_hash(
        "mcp_upload_ticket",
        [{"filename": filename, "content_type": final_type,
          "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}],
        {})  # staging is tool-agnostic: scope stays '' (ref unique per user)
    try:
        staged = await stage_sources(
            user_id, ip, row["client_ref"], "", req_hash,
            [(filename, final_type, body)])
    except IngressQuotaDenied as exc:
        return JSONResponse({"ok": False, **exc.denial}, 429)
    except IngressConflict:
        return _err(409, "idempotency_conflict")
    except IngressInProgress:
        return _err(409, "err_upload_in_progress")
    except IngressUnresolved:
        return _err(500, "err_ingress_unresolved")
    except StagingError:
        # 已知回滚: 暂存对象已清理、配额已精确释放、receipt 已落
        # failed — 同 client_ref 换新 ticket 重试是干净重申。
        logger.exception("mcp ticket staging failed ticket_id=%s", row["id"])
        return _err(500, "err_upload_failed")

    file_info = staged[0] if isinstance(staged, list) else staged
    return {"public_id": file_info["stagingId"],
            "source_token": file_info.get("sourceToken", ""),
            "expires_at": file_info.get("expireAt")}
