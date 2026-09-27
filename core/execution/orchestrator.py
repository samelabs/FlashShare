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
from storage import read as storage_read, write as storage_write, delete as storage_delete
from tools import ToolError

from . import ExecutionError
from .results import (StagedItems, StagedFileItem, OutputsError,
                      declared_outputs, enforce_output_kinds)
from gokapi import InternalDownloadUnavailable

logger = logging.getLogger("flashshare")

MAX_MULTI_BYTES = 100 * 1024 * 1024


class _SourceTooLarge(Exception):
    """storage 实际返回体超出平台单文件上限 (fail before worker)。"""


def _enforce_outputs(tool: dict, items: list) -> None:
    """B2b-1 outputs 运行时强制 — local/remote 两条执行路径共用的唯一
    校验点 (results.enforce_output_kinds), 在任何文件 staged 之前运行:
    每个结果项的 kind 都必须在该工具声明的 outputs 内, 违反 = 带类型化
    错误失败的调用 (gate 标准退款链结算), 零结果项落地。声明本身损坏
    也是拒绝执行 (tool_unavailable), 绝不静默放行。"""
    try:
        declared = declared_outputs(tool)
        enforce_output_kinds(items, declared)
    except OutputsError as exc:
        raise ExecutionError(str(exc)) from exc


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


async def _run_handler(pool, process_tool, tool_id, sources, params):
    """worker 调用 (sources list, possibly empty); BrokenProcessPool
    (worker 进程死亡) → 池重建 + 本 invocation 走 gate 失败收敛
    (exact refund, terminal failed, 不自动重跑)。"""
    from concurrent.futures.process import BrokenProcessPool
    from . import pool as pool_mod
    loop = asyncio.get_event_loop()
    try:
        fut = loop.run_in_executor(
            pool, process_tool, tool_id, sources, params)
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
    """local execution: read 0..N sources (bounded), run the handler in
    the worker (items protocol), stage every file item to Gokapi, and
    return StagedItems — settlement belongs to gate._finish."""
    expiry_days = delivery["expiry_days"]
    allowed_downloads = delivery["allowed_downloads"]

    sources = []
    total = 0
    for upload in uploads:
        ud = dict(upload) if not isinstance(upload, dict) else upload
        budget = min(MAX_UPLOAD_BYTES, MAX_MULTI_BYTES - total)
        if budget < 0:
            raise ToolError("err_file_too_large")
        try:
            status, headers, stream = await asyncio.to_thread(
                storage_read, ud["gokapi_id"])
        except InternalDownloadUnavailable as exc:
            raise ExecutionError("tool_failed") from exc
        except ValueError:
            raise ToolError("err_file_too_large")
        try:
            data = _read_source_bounded(stream, int(ud["size_bytes"] or 0),
                                        budget)
        except _SourceTooLarge:
            raise ToolError("err_file_too_large")
        total += len(data)
        sources.append((data, ud["file_name"], ud["content_type"],
                        len(data)))

    out = await _run_handler(pool, process_tool, tool["id"],
                             sources, params)
    if not isinstance(out, dict) or not isinstance(out.get("items"), list):
        raise ToolError("tool_failed")
    _enforce_outputs(tool, out["items"])

    order, inline, files = [], [], []
    staged_ids: list[str] = []
    try:
        for it in out["items"]:
            order.append(it["kind"])
            if it["kind"] == "file":
                body = it["data"]
                gokapi_result = await asyncio.to_thread(
                    storage_write,
                    io.BytesIO(body), it["filename"], it["content_type"],
                    expiry_days, allowed_downloads, len(body))
                info = gokapi_result.get("FileInfo", {})
                gid = info.get("Id", "")
                if not gid:
                    raise ToolError("tool_failed")
                staged_ids.append(gid)
                files.append(StagedFileItem(
                    gokapi_id=gid,
                    filename=it["filename"],
                    content_type=it["content_type"],
                    size=len(body),
                    expiry_days=expiry_days,
                    allowed_downloads=allowed_downloads,
                    expire_at=info.get("ExpireAt"),
                    url_hotlink=info.get("UrlHotlink", "")))
            else:
                inline.append(it)
    except BaseException:
        # A later item failing must not strand the objects staged for the
        # earlier ones — clean every staged-this-call object, then let the
        # existing failure settlement (typed refund) take over.
        for gid in staged_ids:
            try:
                await asyncio.to_thread(storage_delete, gid)
            except Exception:
                logger.exception("staged cleanup failed gid=%s", gid)
        raise

    return StagedItems(
        order=order, inline=inline, files=files,
        ip=request_ctx.get("ip", ""),
        orig_size=len(sources[0][0]) if len(sources) == 1 else None)


def _mint_hosted_capability(endpoint_url: str, sources: list, params: dict,
                            trusted: bool) -> str | None:
    """Mint the /tool-endpoints/* execution capability for trusted
    same-origin hosted endpoints; None for everything else.

    Trust is an explicit caller input — the canonical execution trust
    fact execute_remote() already computed (owner_id IS NULL or trusted
    row). Fail closed: untrusted rows NEVER receive a capability, no
    matter the URL. The payload digest covers files + params, so
    the capability cannot be spliced onto a different input set."""
    if not trusted:
        return None
    from urllib.parse import urlsplit
    from security import sign_hosted_exec_token

    def _effective_port(parts) -> int | None:
        """Origin-effective port: explicit port wins; otherwise the
        scheme default (https 443 / http 80). Any other scheme yields
        None and is never same-origin for the hosted mint."""
        if parts.port:
            return parts.port
        return {"https": 443, "http": 80}.get(parts.scheme)

    ep = urlsplit(endpoint_url)
    base = urlsplit(BASE_URL)
    if (ep.scheme != base.scheme
            or (ep.hostname or "").lower() != (base.hostname or "").lower()
            or _effective_port(ep) is None
            or _effective_port(ep) != _effective_port(base)):
        return None
    path = ep.path or "/"
    if not path.startswith("/tool-endpoints/"):
        return None
    payload = {
        "files": [
            {"filename": s["filename"], "content_type": s["content_type"],
             "size": s["size"], "url": s["url"]}
            for s in sources
        ],
        "params": params,
    }
    return sign_hosted_exec_token(path, payload)


async def execute_remote(request_ctx, tool, uploads, params, user, delivery):
    """remote (http) execution — the ONE southbound contract (IO-1):

      request  {"files": [{"filename", "content_type", "size", "url"}],
                "params": {...}}
      response {"items": [{"kind":"text","text"} | {"kind":"json","data"}
                | {"kind":"file","filename","content_type",
                   "data_b64" | "url"}]}

    File items are fetched (url) or decoded (data_b64) through the same
    SSRF-guarded pinned transport + per-file cap; a response that does
    not match the shape fails the call (refund path unchanged).
    delivery is a platform fact — the endpoint never sees it.
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
            "url": (f"{BASE_URL}/api/source/{ud['gokapi_id']}"
                    f"?u={user['id']}&t={token}"),
        })

    auth_headers = {}
    _hosted_cap = _mint_hosted_capability(decl.get("url") or "", sources, params, trusted)
    if _hosted_cap:
        auth_headers["X-Hosted-Execution"] = _hosted_cap
    if decl.get("auth_header") and decl.get("auth_token"):
        from secrets_vault import unseal, VaultUnavailable, VaultDecryptError
        try:
            auth_headers[decl["auth_header"]] = unseal(decl["auth_token"])
        except (VaultUnavailable, VaultDecryptError) as exc:
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

    _enforce_outputs(tool, out["items"])

    # items protocol: every file item is already staged by drive_http's
    # adapter (gokapi_id present); inline items carried raw.
    expiry_days = delivery["expiry_days"]
    allowed_downloads = delivery["allowed_downloads"]
    order, inline, files = [], [], []
    staged_ids: list[str] = []
    try:
        for it in out["items"]:
            order.append(it["kind"])
            if it["kind"] == "file":
                body = it["bytes"]
                gokapi_result = await asyncio.to_thread(
                    storage_write,
                    io.BytesIO(body), it["filename"], it["content_type"],
                    expiry_days, allowed_downloads, len(body))
                info = gokapi_result.get("FileInfo", {})
                gid = info.get("Id", "")
                if not gid:
                    raise ExecutionError("err_endpoint_failed")
                staged_ids.append(gid)
                files.append(StagedFileItem(
                    gokapi_id=gid,
                    filename=it["filename"],
                    content_type=it["content_type"],
                    size=len(body),
                    expiry_days=expiry_days,
                    allowed_downloads=allowed_downloads,
                    expire_at=info.get("ExpireAt"),
                    url_hotlink=info.get("UrlHotlink", "")))
            else:
                inline.append({"kind": it["kind"],
                               **({"text": it["text"]} if it["kind"] == "text"
                                  else {"data": it["data"]})})
    except BaseException:
        # same all-or-nothing staging rule as the local path
        for gid in staged_ids:
            try:
                await asyncio.to_thread(storage_delete, gid)
            except Exception:
                logger.exception("staged cleanup failed gid=%s", gid)
        raise

    return StagedItems(
        order=order, inline=inline, files=files,
        ip=request_ctx.get("ip", ""))


import json  # noqa: E402
