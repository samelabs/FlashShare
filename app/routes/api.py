"""Internal API routes: /api/*

These endpoints are called exclusively by the frontend (app.js + detail.html).
Not for external consumption — no version prefix, no public documentation.

Response format (all endpoints):
    Success: {"ok": true, ...data}
    Error:   {"ok": false, "error": "error_code"}
"""

import io
import os
import sys
import json
import asyncio
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response

from app.web_auth import require_session_auth as _require_auth

_CORE = Path(__file__).resolve().parent.parent.parent / "core"
sys.path.insert(0, str(_CORE))

from config import MAX_BATCH_BYTES, PUBLIC_ID_RE, BASE_URL
from database import db
from sharing.uploads import (reserve_ingress, release_ingress,
                             get_upload_by_public_id, list_uploads, delete_upload)
from sharing.ingress_receipt import (
    claim_ingress_quota, finalize_ok as ingress_finalize_ok,
    settle_failure as ingress_settle_failure, resolve_outcome,
    IngressConflict, IngressInProgress, IngressQuotaDenied,
    IngressUnresolved, OUTCOME_DURABLE_OK, OUTCOME_PROVEN_ROLLBACK)
from sharing.ingress_identity import ingress_request_hash
from security import sniff_allowed, sign_source_token, verify_source_token
from storage import (read as storage_read, write as storage_write,
                     info as storage_info, delete as storage_delete)
from tools import ToolError
from execution.source import resolve_upload
from identity.auth import public_user
from utils import now, sanitize_filename, generate_public_id, public_file_url, list_offset
from app.models import RenameRequest
from execution.pool import get_pool as _get_pool
from execution.pool import default_process_tool as process_tool

logger = logging.getLogger("flashshare")
router = APIRouter()

from app.web_auth import client_ip as _client_ip


# ── Tools ──

@router.post("/tools/preflight")
async def tool_preflight(request: Request):
    """Advisory read-only credits affordability preflight.

    Never a reservation: no tool_call row, no ledger write, no ingress
    reservation, no Gokapi access, no client_ref, no consume(). Only an
    explicit affordability denial (402) blocks here; everything else —
    including resolve failures — stays with the authoritative dispatch
    chain. Executed before the browser starts uploading source files.
    """
    user, err = _require_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    tool_id = body.get("tool_id") if isinstance(body, dict) else None
    if not isinstance(tool_id, str) or not tool_id.strip():
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)

    from governance.catalog import resolve
    tool, terr = resolve(tool_id, user["id"])
    if terr:
        # Resolve/routing stays authoritative in dispatch — preflight only
        # answers affordability, so it stays silent (ok) about tool state.
        return {"ok": True}
    from execution.affordability import check_affordability
    denial = check_affordability(user["id"], tool)
    if denial:
        return JSONResponse({"ok": False, **denial}, 402)
    return {"ok": True}


@router.post("/tools/execute")
async def execute_tool(request: Request):
    """Canonical ToolInvocation JSON → the single dispatch/gate chain."""
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from execution.dispatch import dispatch_execute
    payload, error_body, status = await dispatch_execute(
        request, user, raw, process_tool(), _get_pool())
    if error_body is not None:
        return JSONResponse(error_body, status)
    return payload


@router.get("/source/{gokapi_id}")
async def fetch_source(request: Request, gokapi_id: str):
    """One-time signed fetch used by remote tool endpoints."""
    from security import verify_fetch_token
    token = request.query_params.get("t", "")
    user_id_raw = request.query_params.get("u", "")
    if not token or len(token) > 128 or not user_id_raw.isdigit():
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if not verify_fetch_token(token, int(user_id_raw), gokapi_id):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    info = await asyncio.to_thread(storage_info, gokapi_id)
    if not info:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    from gokapi import InternalDownloadUnavailable
    try:
        status, headers, stream = await asyncio.to_thread(
            storage_read, gokapi_id)
    except InternalDownloadUnavailable:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if status >= 400:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    data = stream.read()
    ctype = info.get("ContentType") or "application/octet-stream"
    if ctype.startswith("text/html") or data[:15].lower().startswith(b"<!doctype html"):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return Response(
        content=data,
        media_type=ctype,
        headers={"Content-Disposition": "attachment",
                 "X-Content-Type-Options": "nosniff"},
    )


# ── Uploads ──

@router.get("/uploads")
async def get_uploads(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    offset = list_offset(request.query_params.get("offset", "0"))
    uploads = list_uploads(user["id"], offset=offset, limit=20)
    return {"ok": True, "uploads": uploads, "has_more": len(uploads) == 20}


@router.post("/client-error")
async def client_error(request: Request):
    """Browser-side crash tap. Diagnostics only."""
    import logging
    try:
        body = await request.json()
        logging.getLogger("flashshare.client").error(
            "CLIENT-CRASH %s %s | %s", body.get("lang"), body.get("url"), body.get("msg"))
        stack = (body.get("stack") or "").replace("\n", " | ")[:1500]
        if stack:
            logging.getLogger("flashshare.client").error("CLIENT-STACK %s", stack)
    except Exception:
        pass
    return {"ok": True}


async def _staged_cleanup(gokapi_ids, user_id, ingress_id, usage_ids):
    """Known-rollback settlement: best-effort delete staged objects, then
    EXACT quota release + receipt failed in one transaction. If the DB
    settlement itself fails, the receipt stays running — startup
    recovery owns it; quota is never speculatively released."""
    for gid in gokapi_ids:
        try:
            await asyncio.to_thread(storage_delete, gid)
        except Exception:
            logger.warning("rollback: gokapi cleanup failed for %s", gid)
    try:
        ingress_settle_failure(ingress_id, usage_ids, user_id)
    except Exception:
        logger.exception("ingress failure settlement failed id=%s "
                         "(receipt stays running for recovery)", ingress_id)


async def _finalize_or_reconcile(ingress_id, gokapi_ids, user_id, usage_ids,
                                 canonical_result):
    """Success finalize with STRICT commit-outcome reconciliation.

    Try the running→ok finalize. If the finalize transaction's commit
    ack is lost (or it fails for any reason), resolve the durable
    outcome BEFORE touching anything:
      DURABLE_OK      → return the stored result (zero Gokapi delete,
                        zero quota release) — the commit landed;
      PROVEN_ROLLBACK → receipt still running → staged cleanup + exact
                        release + failed (the ONLY cleanup path);
      UNRESOLVED      → zero cleanup, zero release, zero terminal
                        rewrite — raise for err_ingress_unresolved.
    Returns the durable canonical result on success."""
    import json as _j
    try:
        from database import db as _db
        with _db() as con:
            ingress_finalize_ok(con, ingress_id, canonical_result)
    except Exception:
        logger.exception("ingress finalize failed id=%s — reconciling", ingress_id)
        out = resolve_outcome(ingress_id)
        if out["outcome"] == OUTCOME_DURABLE_OK:
            return out["result"]
        if out["outcome"] == OUTCOME_PROVEN_ROLLBACK:
            await _staged_cleanup(gokapi_ids, user_id, ingress_id,
                                   usage_ids)
            raise RuntimeError("err_upload_failed")
        # UNRESOLVED — no cleanup, no release, no rewrite
        raise IngressUnresolved("err_ingress_unresolved")
    return canonical_result


@router.post("/uploads")
async def create_upload(request: Request):
    """Upload one or more source files for a selected tool.

    Every upload stages a tool source (tool field required — sharing is the
    share tool). Custody is the INTERMEDIATE_* platform policy; result
    delivery entitlement is enforced at the canonical execute preflight.
    Idempotent on client_ref + content: a response-loss retry replays the
    exact stored staging response.
    """
    user, err = _require_auth(request)
    if err:
        return err

    form = await request.form()
    uploads = form.getlist("file")
    if not uploads:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    # Operation identity FIRST — before any quota reservation or Gokapi
    # storage. A missing/invalid ref never leaves side effects behind.
    client_ref = form.get("client_ref")
    if not isinstance(client_ref, str) or not client_ref.strip() \
            or len(client_ref.strip()) > 128:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    client_ref = client_ref.strip()

    ip = _client_ip(request)
    from governance.delivery import (INTERMEDIATE_TTL_DAYS,
                                     INTERMEDIATE_DOWNLOAD_LIMIT)
    # the upload surface ONLY stages tool sources. Sharing is the
    # share tool (files 1..20 → one file item per input); result delivery
    # entitlement is enforced at the canonical execute preflight, not here.
    has_tool = form.get("tool", "").strip()
    if not has_tool:
        return JSONResponse({"ok": False, "error": "err_tool_required"}, 422)

    pending = []
    # Transport safety only: a declared Content-Length far beyond the batch
    # cap never needs reading. This is NOT a second batch algorithm — the
    # authoritative source-byte contract stays in governance.ingress
    # validate_batch() below. `incoming` on this early-reject path means the
    # transport-declared request bytes (headers included), never the
    # authoritative source batch total.
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > MAX_BATCH_BYTES + 1024 * 1024:
        return JSONResponse({"ok": False, "error": "err_batch_too_large",
                             "limit": MAX_BATCH_BYTES,
                             "incoming": declared}, 413)

    for file in uploads:
        if not file.filename:
            continue
        content = await file.read()
        size = len(content)
        if size == 0:
            return JSONResponse({"ok": False, "error": "err_empty_file"}, 400)
        filename = sanitize_filename(file.filename or "upload")
        valid, final_type = sniff_allowed(filename, file.content_type or "", content[:512])
        if not valid:
            # Same structured rejection shape as every other ingress
            # surface; the offending file is named (batch uploads are
            # still rejected as a whole).
            from security import mismatch_detail
            return JSONResponse({"ok": False, "error": final_type,
                                 **mismatch_detail(filename, content[:512])},
                                400)
        pending.append((content, size, filename, final_type))

    if not pending:
        return JSONResponse({"ok": False, "error": "err_select_file"}, 400)

    # Authoritative platform boundaries: one validator for Web/MCP.
    from governance.ingress import validate_batch
    denial = validate_batch([size for _, size, _, _ in pending])
    if denial:
        status = 400 if denial["error"] == "err_too_many_files" else 413
        return JSONResponse({"ok": False, **denial}, status)

    # Canonical ingress content hash (after sanitize/sniff/validation).
    import hashlib as _hl
    kind = "web_tool_source"
    context = {"tool_id": has_tool}
    req_hash = ingress_request_hash(
        kind,
        [{"filename": fn, "content_type": ct, "size": size,
          "sha256": _hl.sha256(content).hexdigest()}
         for content, size, fn, ct in pending],
        context)
    try:
        receipt, usage_ids, replay_result = claim_ingress_quota(
            user["id"], ip, client_ref, req_hash,
            [size for _, size, _, _ in pending],
            scope=has_tool)
    except IngressQuotaDenied as exc:
        # Authoritative denial — the web layer never recomputes
        # used/remaining, it only relays the full contract.
        return JSONResponse({"ok": False, **exc.denial}, 429)
    except IngressConflict:
        return JSONResponse({"ok": False, "error": "idempotency_conflict"}, 409)
    except IngressInProgress:
        return JSONResponse({"ok": False, "error": "err_upload_in_progress"}, 409)
    except IngressUnresolved:
        # durable-ok replay payload unresolvable (NULL/malformed/scalar):
        # fail closed BEFORE any storage side effect — zero new quota,
        # zero Gokapi, the receipt stays exactly as it is.
        return JSONResponse({"ok": False, "error": "err_ingress_unresolved"},
                            500)
    if replay_result is not None:
        # same ref + same content, previously ok → exact stored response
        if isinstance(replay_result, dict) and "file" in replay_result:
            return replay_result
        if isinstance(replay_result, list):
            return {"ok": True, "files": replay_result} \
                if len(replay_result) > 1 else {"ok": True, "file": replay_result[0]}
        return replay_result

    source_expiry = INTERMEDIATE_TTL_DAYS
    source_downloads = INTERMEDIATE_DOWNLOAD_LIMIT

    files_result = []
    uploaded_gokapi_ids = []
    try:
        for content, size, filename, final_type in pending:
            gokapi = await asyncio.to_thread(
                storage_write, io.BytesIO(content), filename, final_type,
                source_expiry, source_downloads, size)
            info = gokapi.get("FileInfo", {})
            gid = info.get("Id", "")
            if not gid:
                raise RuntimeError("gokapi_missing_id")
            uploaded_gokapi_ids.append(gid)
            files_result.append({
                "name": filename,
                "filename": filename,
                "contentType": final_type,
                "size": size,
                # staging identity of a tool-source intermediate (the
                # Gokapi id IS the staging handle here); storage ids
                # beyond this handle never leave
                "stagingId": gid,
                "sourceToken": sign_source_token(user["id"], gid),
                "expireAt": info.get("ExpireAt"),
                "expiryDays": source_expiry,
                "allowedDownloads": source_downloads,
            })
    except Exception:
        await _staged_cleanup(uploaded_gokapi_ids, user["id"],
                              int(receipt["id"]), usage_ids)
        logger.exception("multi-upload failed, %d files rolled back",
                         len(uploaded_gokapi_ids))
        return JSONResponse({"ok": False, "error": "err_upload_failed"}, 500)

    stored = ({"ok": True, "file": files_result[0]}
              if len(files_result) == 1
              else {"ok": True, "files": files_result})
    try:
        await _finalize_or_reconcile(int(receipt["id"]), uploaded_gokapi_ids,
                                      user["id"], usage_ids, files_result)
    except IngressUnresolved:
        return JSONResponse(
            {"ok": False, "error": "err_ingress_unresolved"}, 500)
    except Exception:
        return JSONResponse({"ok": False, "error": "err_upload_failed"}, 500)
    return stored


@router.delete("/uploads/{public_id}")
async def delete_upload_route(public_id: str, request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    if not PUBLIC_ID_RE.match(public_id):
        return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
    upload = get_upload_by_public_id(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if upload["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "error": "forbidden"}, 403)
    try:
        delete_upload(upload["id"], user["id"])
    except Exception:
        logger.exception("delete failed for %s", public_id)
        return JSONResponse({"ok": False, "error": "delete_failed"}, 500)
    return {"ok": True}


@router.patch("/uploads/{public_id}")
async def rename_upload_route(public_id: str, request: Request):
    """Rename an upload (only stem, extension preserved)."""
    user, err = _require_auth(request)
    if err:
        return err
    if not PUBLIC_ID_RE.match(public_id):
        return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
    upload = get_upload_by_public_id(public_id)
    if not upload:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if upload["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "error": "forbidden"}, 403)

    try:
        raw = await request.json()
        req = RenameRequest(**raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    new_name = req.name.strip()
    original_ext = os.path.splitext(upload["file_name"])[1]
    clean_stem = sanitize_filename(new_name)
    clean_stem = os.path.splitext(clean_stem)[0]
    if not clean_stem or len(clean_stem) > 200:
        return JSONResponse({"ok": False, "error": "invalid_name"}, 400)
    final_name = clean_stem + original_ext
    from repo.uploads import rename as rename_upload_row
    with db() as con:
        rename_upload_row(con, upload["id"], final_name)
    return {"ok": True, "name": final_name}


# ── User ──

@router.get("/me")
async def get_me(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    return {"ok": True, "user": public_user(user)}


# ── API token management: account-control surface, session only ──

@router.get("/keys")
async def list_keys_route(request: Request):
    """The owner's keys (prefixes + viewability). Session-only: the
    module's _require_auth is require_session_auth, so Bearer access
    can never list or read keys."""
    user, err = _require_auth(request)
    if err:
        return err
    from identity.apikeys import list_keys
    return {"ok": True, "keys": list_keys(user["id"])}


@router.post("/keys")
async def create_key_route(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
        name = str(raw.get("name") or "").strip()[:64]
    except Exception:
        name = ""
    from identity.apikeys import create_key
    try:
        key = create_key(user["id"], name or "key")
    except ValueError as exc:
        token = str(exc)
        status = 409 if token == "err_key_limit" else 503
        return JSONResponse({"ok": False, "error": token}, status)
    # the sealed copy is server-side storage — never part of a response
    key.pop("key_sealed", None)
    return {"ok": True, "key": key}


@router.get("/keys/{key_id}/reveal")
async def reveal_key_route(key_id: int, request: Request):
    """The full key plaintext — owner's browser session only (Bearer
    requests never reach this handler)."""
    user, err = _require_auth(request)
    if err:
        return err
    from identity.apikeys import reveal_key
    from secrets_vault import VaultDecryptError
    try:
        plaintext = reveal_key(user["id"], key_id)
    except VaultDecryptError:
        return JSONResponse({"ok": False, "error": "err_vault"}, 503)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 503)
    if plaintext is None:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True, "key": plaintext}


@router.delete("/keys/{key_id}")
async def delete_key_route(key_id: int, request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    from identity.apikeys import delete_key
    if not delete_key(user["id"], key_id):
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True}
