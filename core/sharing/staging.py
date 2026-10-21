"""sharing/staging — 文件暂存单点 (P4)。

Web 上传 (app/routes/api.py)、MCP ticket PUT (app/routes/mcp_upload.py)、
MCP 内联 / URL 源 (app/routes/mcp.py _call_tool) 三处入口全部经由
stage_sources(): ingress receipt claim (配额) → Gokapi 暂存 → 成功
finalize + 严格三态核验 (resolve_outcome)。入口保留的职责只有三件:
身份/哈希语义 (kind + context, 各面的操作身份定义)、sniff 收容
(调用之前完成, 传入的已是 final 身份)、错误映射 (捕获后按各面
协议作答)。

引用已有 public_id 不经过本函数 — 引用是权限解析 (resolve_upload),
不暂存、不计入入站配额。

文件来源准备单点 (A4): ingest_sources 把 URL 拉取 (钉扎传输)、sniff
收容、accept 闸门、批量上限、ingress 请求哈希与 stage_sources 串在
一起; MCP (tools/call) 与 /v1 (POST /v1/tools/{key}) 都只调用它。
"""

import asyncio
import io
import logging

logger = logging.getLogger("flashshare")


class StagingError(Exception):
    """已知回滚: 暂存失败或 finalize 判定回滚 — 暂存对象已清理、
    精确配额已释放、receipt 已落 failed。调用面按 err_upload_failed
    类错误作答。"""


class SourceError(Exception):
    """来源准备期的输入拒绝 (调用方可修复): 无效引用 id / 引用解析
   失败 / URL 拉取失败 / sniff 拒绝 / accept 闸门 / 批量上限。
    code = 平台错误码, extra = 随码结构字段 (如 mismatch 详情、批量
    上限 denial), status = REST 面的建议 HTTP 状态。"""

    def __init__(self, code: str, status: int = 400, extra: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.extra = extra or {}


def _guess_ctype(filename: str) -> str:
    import mimetypes
    ct, _ = mimetypes.guess_type(filename)
    return ct or "application/octet-stream"


def fetch_url_source(url: str) -> bytes:
    """GET one https source over the pinned SSRF-guarded transport
    (every hop: https only, all resolved addresses public, connect pinned
    to a validated address; redirects re-validated). Body cap = the
    per-file ingress cap."""
    from config import MAX_UPLOAD_BYTES
    from remote import RemoteError, pinned_request
    URL_FETCH_TIMEOUT_S = 30
    status, data, _ctype, _cd = pinned_request(url, method="GET",
                                               timeout_s=URL_FETCH_TIMEOUT_S,
                                               max_bytes=MAX_UPLOAD_BYTES)
    if status != 200:
        raise RemoteError("http_status", str(status))
    if len(data) > MAX_UPLOAD_BYTES:
        raise RemoteError("too_large", f"over {MAX_UPLOAD_BYTES} bytes")
    if not data:
        raise RemoteError("empty", "empty body")
    return data


def url_source_upload(url: str, filename, body: bytes) -> tuple:
    """(filename, ctype, body) for a fetched URL source — same shape as
    an inline file; the name comes from the caller or the URL path."""
    import os
    import urllib.parse
    from utils import sanitize_filename
    if not filename:
        path = urllib.parse.urlsplit(url).path
        filename = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    fn = sanitize_filename(str(filename or "download"))
    if not os.path.splitext(fn)[1]:
        from security import detect_ext
        fn += detect_ext(body[:64]) or ""
    return fn, _guess_ctype(fn), body


async def ingest_sources(user: dict, tool: dict, items: list,
                         ip: str, client_ref: str, scope: str) -> tuple:
    """文件来源准备 + 暂存 (MCP 与 /v1 的唯一实现)。

    items: [(kind, val)] 按请求序 — kind "inline"|"new" (val =
    (filename, ctype, body_bytes)) | "ref" (val = (public_id,
    source_token)) | "url" (val = (url, filename|None))。

    链: 引用权限解析 (零暂存零配额) → URL 钉扎拉取 → sniff 收容
    (同族收容 final 身份) → accept 闸门 (声明窗口) → 批量上限 →
    ingress 请求哈希 → stage_sources (claim→暂存→finalize 三态)。

    Returns (source_files, canon): source_files = canonical 调用源
    (请求序 [{public_id, source_token}]), canon = 暂存文件 final 身份
    列表 [(filename, ctype, body)] (调用面用于输入摘要)。
    输入拒绝抛 SourceError; 暂存期拒绝原样上抛 StagingError /
    IngressConflict / IngressInProgress / IngressQuotaDenied /
    IngressUnresolved。
    """
    from config import PUBLIC_ID_RE
    from execution.source import resolve_upload
    ref_names = {}
    for i, (kind, val) in enumerate(items):
        if kind != "ref":
            continue
        pid, tok = val
        if not PUBLIC_ID_RE.match(pid):
            raise SourceError("invalid_id")
        upload, rerr = await resolve_upload(pid, user, tok)
        if rerr or not upload:
            raise SourceError(rerr or "not_found",
                              {"not_found": 404, "forbidden": 403}.get(rerr, 400))
        ref_names[i] = upload.get("file_name", "")

    staged_specs = {}
    for i, (kind, val) in enumerate(items):
        if kind in ("inline", "new"):
            staged_specs[i] = val
        elif kind == "url":
            url, fname = val
            try:
                data = await asyncio.to_thread(fetch_url_source, url)
            except Exception as exc:  # RemoteError (typed) from the transport
                from remote import RemoteError
                if isinstance(exc, RemoteError):
                    raise SourceError(exc.code, extra={
                        "message": str(exc.detail)[:200]}) from exc
                raise
            staged_specs[i] = url_source_upload(url, fname, data)

    from security import sniff_allowed, mismatch_detail
    from execution.dispatch import source_accept_error
    from governance.ingress import validate_batch
    from utils import sanitize_filename
    canon = []
    for i in sorted(staged_specs):
        fn, ct, data = staged_specs[i]
        ok, final_t, final_fn = sniff_allowed(fn, ct, data[:512])
        if not ok:
            code = ("err_format_not_supported"
                    if final_t == "err_file_type_blocked" else final_t)
            raise SourceError(code, extra=mismatch_detail(
                sanitize_filename(fn), data[:512]))
        canon.append((sanitize_filename(final_fn), final_t, data))
    names = {**ref_names,
             **{i: c[0] for i, c in zip(sorted(staged_specs), canon)}}
    aerr = source_accept_error(tool, [names.get(i, "")
                                      for i in range(len(items))])
    if aerr:
        raise SourceError(aerr)
    bdenial = validate_batch([len(b) for _, _, b in canon])
    if bdenial:
        status = 400 if bdenial.get("error") == "err_too_many_files" else 413
        raise SourceError(bdenial.get("error", "err_batch_too_large"),
                          status=status, extra=dict(bdenial))

    infos = []
    if canon:
        import hashlib as _hl
        from sharing.ingress_identity import ingress_request_hash
        ctx = {"tool_id": tool["tool_key"]}
        if any(kind not in ("inline", "new") for kind, _ in items):
            # url / reference forms are part of the operation identity:
            # same client_ref + a different source list → conflict.
            ctx["sources"] = [
                {"kind": "url", "url": val[0]} if kind == "url"
                else {"kind": "ref", "public_id": val[0]} if kind == "ref"
                else {"kind": "inline"}
                for kind, val in items]
        req_hash = ingress_request_hash(
            "tool_source",
            [{"filename": fn, "content_type": ct, "size": len(b),
              "sha256": _hl.sha256(b).hexdigest()} for fn, ct, b in canon],
            ctx)
        staged = await stage_sources(int(user["id"]), ip, client_ref,
                                     scope, req_hash, canon)
        infos = staged if isinstance(staged, list) else [staged]

    # canonical source order = request order (staged handles + references)
    ingested = iter(infos)
    source_files = []
    for kind, val in items:
        if kind == "ref":
            source_files.append({"public_id": val[0],
                                 "source_token": val[1]})
        else:
            info = next(ingested)
            source_files.append({"public_id": info["stagingId"],
                                 "source_token": info.get("sourceToken", "")})
    return source_files, canon


async def stage_sources(user_id: int, ip: str, client_ref: str,
                        scope: str, req_hash: str, files: list) -> list | dict:
    """一个逻辑上传的完整暂存链 (三入口唯一实现)。

    files: [(final_filename, final_ctype, body_bytes)] — 已过 sniff
    收容 (同族收容后的 final 身份)。req_hash: 本面的 canonical
    ingress 内容哈希 (ingress_request_hash 产物)。

    返回 durable canonical file-info 列表 (新暂存或 receipt 重放;
    重放形态 verbatim 透传 — 历史单文件 receipt 可能是 dict, 由调用
    面自行适配)。claim 的幂等/配额拒绝原样上抛 (IngressConflict /
    IngressInProgress / IngressQuotaDenied / IngressUnresolved);
    已知回滚抛 StagingError。

    receipt 原语 (claim/finalize/settle/resolve) 一律调用期导入 —
    测试注入按模块属性生效, 本模块不做 import 期快照。
    """
    from sharing.ingress_receipt import (
        claim_ingress_quota, finalize_ok, resolve_outcome,
        IngressUnresolved, OUTCOME_DURABLE_OK, OUTCOME_PROVEN_ROLLBACK)
    receipt, usage_ids, replay = claim_ingress_quota(
        user_id, ip, client_ref, req_hash,
        [len(body) for _, _, body in files], scope=scope)
    if replay is not None:
        return replay

    from governance.delivery import (INTERMEDIATE_TTL_DAYS,
                                     INTERMEDIATE_DOWNLOAD_LIMIT)
    from security import sign_source_token
    from storage import write as storage_write
    infos = []
    gids = []
    for filename, ctype, body in files:
        try:
            gokapi = await asyncio.to_thread(
                storage_write, io.BytesIO(body), filename, ctype,
                INTERMEDIATE_TTL_DAYS, INTERMEDIATE_DOWNLOAD_LIMIT,
                len(body))
        except Exception:
            logger.exception("staging write failed (%s)", filename)
            await _rollback(gids, receipt, usage_ids, user_id)
            raise StagingError("staging_write_failed") from None
        gid = (gokapi.get("FileInfo", {}) or {}).get("Id", "")
        if not gid:
            await _rollback(gids, receipt, usage_ids, user_id)
            raise StagingError("gokapi_missing_id")
        gids.append(gid)
        infos.append({
            "name": filename, "filename": filename,
            "contentType": ctype, "size": len(body),
            "stagingId": gid,
            "sourceToken": sign_source_token(user_id, gid),
            "expireAt": (gokapi.get("FileInfo", {}) or {}).get("ExpireAt"),
            "expiryDays": INTERMEDIATE_TTL_DAYS,
            "allowedDownloads": INTERMEDIATE_DOWNLOAD_LIMIT,
        })

    # 成功 finalize, 严格三态核验 (ACK 丢失先核验再回答):
    #   DURABLE_OK      → 提交已落地 — 返回存储结果, 零删除零释放;
    #   PROVEN_ROLLBACK → 仍 running = 成功事务未落地 — 立即清理
    #                     暂存对象 + 精确释放 + 落 failed, 抛 StagingError;
    #   UNRESOLVED      → 零清理零释放零改写, 抛 IngressUnresolved。
    from database import db
    try:
        with db() as con:
            finalize_ok(con, int(receipt["id"]), infos)
    except Exception:
        logger.exception("ingress finalize failed id=%s — reconciling",
                         receipt["id"])
        out = resolve_outcome(int(receipt["id"]))
        if out["outcome"] == OUTCOME_DURABLE_OK:
            return out["result"]
        if out["outcome"] == OUTCOME_PROVEN_ROLLBACK:
            await _rollback(gids, receipt, usage_ids, user_id)
            raise StagingError("finalize_proven_rollback")
        raise IngressUnresolved("err_ingress_unresolved") from None
    return infos


async def _rollback(gids, receipt, usage_ids, user_id):
    """已知回滚结算: 尽力删除暂存对象, 然后同一事务内精确配额释放 +
    receipt 落 failed。DB 结算本身失败则 receipt 留 running — 启动
    recovery 接管; 配额永不投机释放。"""
    from sharing.ingress_receipt import settle_failure
    from storage import delete as storage_delete
    for gid in gids:
        try:
            await asyncio.to_thread(storage_delete, gid)
        except Exception:
            logger.warning("rollback: gokapi cleanup failed for %s", gid)
    try:
        settle_failure(int(receipt["id"]), usage_ids, user_id)
    except Exception:
        logger.exception("ingress failure settlement failed id=%s "
                         "(receipt stays running for recovery)",
                         receipt["id"])
