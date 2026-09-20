"""execution/orchestrator — 三路执行编排 (批2e, 从 api.py 收编).

local 单文件 / local 多文件 / remote。共享执行 spine:
读源 → handler(subprocess) → stage physical result (Gokapi)。

durable result settlement (uploads row / tool_calls binding /
earning/revenue) 由 gate._finish 在单一 DB transaction 内落地。
source lifecycle 不由 execution 删除: intermediate source 由 Gokapi
INTERMEDIATE_TTL_DAYS 到期自清; durable upload source 沿用自己的
uploads/Gokapi lifecycle。execution 只读 source, 不拥有 source
deletion authority — settlement 失败不得销毁 invocation 输入
(否则 failed attempt 的同源重试契约是假的, durable source 也会被误删)。

🔴8 修复: 多文件上传(输入侧)的 gokapi 中途回滚在 api.create_upload 路由层
(上传批次属于应用层动作); 本模块负责执行侧: 任一源读取失败 → 已读流全部
释放, 不产生半落地。
"""

import asyncio
import io
import logging

from config import BASE_URL, MAX_UPLOAD_BYTES
from database import db  # noqa: F401 (pipeline 内使用)
from storage import read as storage_read, write as storage_write
from tools import ToolError

from . import ExecutionError
from .pipeline import StagedResult
from gokapi import InternalDownloadUnavailable

logger = logging.getLogger("flashshare")

MAX_MULTI_BYTES = 100 * 1024 * 1024


class _SourceTooLarge(Exception):
    """storage 实际返回体超出平台单文件上限 (fail before worker)。"""


def _read_source_bounded(stream, declared_size: int, budget: int) -> bytes:
    """边读边判定的 bounded read — 不信任 DB 记录的 size_bytes, storage
    实际返回多少就读多少, 超过 (单文件上限 / 剩余 multi 预算) 立即失败。
    stream 无论如何都关闭。"""
    try:
        if declared_size and declared_size > budget:
            raise _SourceTooLarge()
        chunks = []
        total = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > budget:
                raise _SourceTooLarge()
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


# Stage C: delivery 合法性在 invocation boundary (governance.delivery
# validate_delivery) 已严格保证; orchestrator 直读 canonical delivery,
# 不再从 params 取 lifecycle / silent fallback / 猜默认值。


async def _run_handler(pool, process_tool, tool_id, single, multi_sources, params):
    """worker 调用; BrokenProcessPool (worker 进程死亡) → 池重建 +
    本 invocation 走 gate 失败收敛 (exact refund, terminal failed,
    不自动重跑)。"""
    from concurrent.futures.process import BrokenProcessPool
    from . import pool as pool_mod
    loop = asyncio.get_event_loop()
    try:
        if multi_sources is not None:
            fut = loop.run_in_executor(
                pool, process_tool, tool_id, b"", "", "", 0, params,
                multi_sources)
        else:
            stream, filename, ctype, size = single
            fut = loop.run_in_executor(
                pool, process_tool, tool_id, stream, filename, ctype,
                size, params)
        return await fut
    except BrokenProcessPool as exc:
        # worker 进程死亡 (submit 或 await 阶段): CAS 重建池 (同一 broken
        # pool 的并发观察者只产生一个 replacement), 本 invocation 重抛为
        # 普通执行失败 → 既有 gate 收敛 (exact refund + terminal failed,
        # 对外 tool_failed)。不自动重跑。
        pool_mod.reset_pool(pool, "broken_process_pool")
        raise RuntimeError("worker_pool_broken") from exc


async def execute_local(request_ctx, tool, uploads, params, user, process_tool, pool,
                        delivery):
    """local 单/多文件编排。uploads 非空已解析。

    delivery (Stage C): canonical invocation delivery — 在 invocation
    boundary 已严格合法 (expiry_days/allowed_downloads), 直接读取。
    handler 只收到 params, 永远看不到 delivery。
    """
    multi = bool(tool.get("multi")) and len(uploads) > 1 or (
        bool(tool.get("multi")) and len(uploads) >= 1)
    expiry_days = delivery["expiry_days"]
    allowed_downloads = delivery["allowed_downloads"]

    sources = []
    total = 0
    for upload in uploads:
        ud = dict(upload) if not isinstance(upload, dict) else upload
        # 早期拒绝: DB 记录超限 → 不打 storage; 实际读取仍 bounded
        # (storage 真实返回体不信任 DB size_bytes)
        budget = min(MAX_UPLOAD_BYTES, MAX_MULTI_BYTES - total)
        if budget < 0:
            raise ToolError("err_file_too_large")
        try:
            status, headers, stream = storage_read(ud["gokapi_id"])
        except InternalDownloadUnavailable as exc:
            # source read rides the authenticated non-counting channel;
            # if that channel is unavailable the invocation fails typed —
            # never a silent counted fallback.
            raise ExecutionError("tool_failed") from exc
        except ValueError:
            # storage 侧 bounded read 已拒 (实际体 > 平台单文件上限)
            raise ToolError("err_file_too_large")
        try:
            data = _read_source_bounded(stream, int(ud["size_bytes"] or 0),
                                        budget)
        except _SourceTooLarge:
            raise ToolError("err_file_too_large")
        total += len(data)
        sources.append((data, ud["file_name"], ud["content_type"],
                        len(data)))

    if tool.get("multi") and len(sources) >= 1:
        result = await _run_handler(pool, process_tool, tool["id"],
                                    None, sources, params)
    else:
        stream, filename, ctype, size = sources[0]
        result = await _run_handler(pool, process_tool, tool["id"],
                                    (stream, filename, ctype, size), None, params)

    output_bytes = result["output_bytes"]
    output_size = len(output_bytes)
    gokapi_result = storage_write(
        io.BytesIO(output_bytes), result["filename"], result["content_type"],
        expiry_days, allowed_downloads, output_size)
    info = gokapi_result.get("FileInfo", {})

    # staging-only: Gokapi 物理结果已落, 但 518 durable/public 身份
    # (uploads row / publicId / downloadUrl) 由 gate 的 settlement
    # transaction 生成 — 本函数绝不写 uploads。
    staged = StagedResult(
        gokapi_id=info.get("Id", ""),
        filename=result["filename"],
        content_type=result["content_type"],
        size=output_size,
        expiry_days=expiry_days,
        allowed_downloads=allowed_downloads,
        expire_at=info.get("ExpireAt"),
        url_hotlink=info.get("UrlHotlink", ""),
        ip=request_ctx.get("ip", ""),
        orig_size=len(sources[0][0]) if len(sources) == 1 else None)

    # source 不做 eager-delete (execution 只读 source; 生命周期归
    # intermediate TTL / durable uploads 自身)
    return staged


async def execute_remote(request_ctx, tool, uploads, params, user, delivery):
    """remote (http) 编排: 拉 URL 签名 → drive_http → 零信任落地。

    delivery (Stage C): canonical invocation delivery, boundary 已严格
    合法。drive_http 只收到 params — delivery/client_ref 等平台字段
    不泄漏给 remote endpoint。
    """
    import remote as _remote
    from security import sign_fetch_token

    decl = json.loads(tool["endpoint_json"]) if tool.get("endpoint_json") else {}
    if not decl.get("url"):
        raise ExecutionError("err_tool_not_configured")
    trusted = (tool.get("owner_id") is None) or bool(tool.get("trusted"))
    sources = []
    total = 0
    for upload in uploads:
        ud = dict(upload) if not isinstance(upload, dict) else upload
        total += ud["size_bytes"]
        if total > MAX_MULTI_BYTES:
            raise ExecutionError("err_file_too_large")
        token = sign_fetch_token(user["id"], ud["gokapi_id"])
        sources.append({
            "filename": ud["file_name"],
            "content_type": ud["content_type"],
            "size": ud["size_bytes"],
            "fetch_url": (f"{BASE_URL}/api/source/{ud['gokapi_id']}"
                          f"?u={user['id']}&t={token}"),
        })

    auth_headers = {}
    if decl.get("auth_header") and decl.get("auth_token"):
        from secrets_vault import unseal, VaultUnavailable, VaultDecryptError
        try:
            auth_headers[decl["auth_header"]] = unseal(decl["auth_token"])
        except (VaultUnavailable, VaultDecryptError) as exc:
            # Never send anything to the endpoint and never log the
            # secret: typed reason + tool identity only. Outward this is
            # a normal remote execution failure (refund path unchanged).
            logger.warning("remote tool %s credential unavailable: %s",
                           tool["id"], type(exc).__name__)
            raise ExecutionError("err_endpoint_failed") from exc

    loop = asyncio.get_event_loop()
    try:
        out = await loop.run_in_executor(
            None, _remote.drive_http, decl, sources, params, auth_headers, trusted)
    except _remote.RemoteError as exc:
        logger.warning("remote tool %s failed: %s", tool["id"], exc)
        code = {"ssrf_blocked": "err_endpoint_blocked",
                "timeout": "err_endpoint_timeout"}.get(exc.code, "err_endpoint_failed")
        raise ExecutionError(code) from exc

    output_bytes = out["bytes"]
    output_size = len(output_bytes)
    expiry_days = delivery["expiry_days"]
    allowed_downloads = delivery["allowed_downloads"]
    gokapi_result = storage_write(
        io.BytesIO(output_bytes), out["filename"], out["content_type"],
        expiry_days, allowed_downloads, output_size)
    info = gokapi_result.get("FileInfo", {})

    # staging-only (与 local 同一 StagedResult 类型): durable/public
    # landing 只发生在 gate settlement transaction。
    staged = StagedResult(
        gokapi_id=info.get("Id", ""),
        filename=out["filename"],
        content_type=out["content_type"],
        size=output_size,
        expiry_days=expiry_days,
        allowed_downloads=allowed_downloads,
        expire_at=info.get("ExpireAt"),
        url_hotlink=info.get("UrlHotlink", ""),
        ip=request_ctx.get("ip", ""))

    # source 不做 eager-delete (与 local 同机制)
    return staged


import json  # noqa: E402
