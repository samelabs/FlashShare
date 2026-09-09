"""execution/orchestrator — 三路执行编排 (批2e, 从 api.py 收编).

local 单文件 / local 多文件 / remote。共享落地 spine:
读源 → handler(subprocess) → storage write → DB 记录 → 清理源。

🔴8 修复: 多文件上传(输入侧)的 gokapi 中途回滚在 api.create_upload 路由层
(上传批次属于应用层动作); 本模块负责执行侧: 任一源读取失败 → 已读流全部
释放, 不产生半落地。
"""

import asyncio
import io
import logging

from config import BASE_URL
from database import db  # noqa: F401 (pipeline 内使用)
from gokapi import delete_gokapi_file
from storage import read as storage_read, write as storage_write
from tools import ToolError

from . import ExecutionError
from .pipeline import save_upload_record

logger = logging.getLogger("flashshare")

MAX_MULTI_BYTES = 100 * 1024 * 1024


def validate_lifecycle(params) -> tuple:
    """expiry_days(1|3|7) + allowed_downloads(1|10|50|100|200), 非法回落默认。"""
    try:
        expiry_days = int(params.get("expiry_days", 1))
    except (ValueError, TypeError):
        expiry_days = 1
    if expiry_days not in (1, 3, 7):
        expiry_days = 1
    try:
        allowed_downloads = int(params.get("allowed_downloads", 50))
    except (ValueError, TypeError):
        allowed_downloads = 50
    if allowed_downloads not in (1, 10, 50, 100, 200):
        allowed_downloads = 50
    return expiry_days, allowed_downloads


async def _run_handler(pool, process_tool, tool_id, single, multi_sources, params):
    loop = asyncio.get_event_loop()
    if multi_sources is not None:
        return await loop.run_in_executor(
            pool, process_tool, tool_id, b"", "", "", 0, params, multi_sources)
    stream, filename, ctype, size = single
    return await loop.run_in_executor(
        pool, process_tool, tool_id, stream, filename, ctype, size, params)


async def execute_local(request_ctx, tool, uploads, params, user, process_tool, pool,
                        lifecycle=None):
    """local 单/多文件编排。uploads 非空已解析。

    lifecycle (4A-fix1): dispatch 在 params 清洗前捕获的平台生命周期字段
    (expiry_days/allowed_downloads)。与 canonical tool params 分开传递 —
    tool params 可能被 enforce_params_decl 剥离声明外键, 不能再作为
    lifecycle 的来源。None/缺键 → validate_lifecycle 默认 1 / 50。
    """
    multi = bool(tool.get("multi")) and len(uploads) > 1 or (
        bool(tool.get("multi")) and len(uploads) >= 1)
    expiry_days, allowed_downloads = validate_lifecycle(lifecycle or {})

    sources = []
    gokapi_ids = []
    total = 0
    for upload in uploads:
        ud = dict(upload) if not isinstance(upload, dict) else upload
        total += ud["size_bytes"]
        if total > MAX_MULTI_BYTES:
            raise ToolError("err_file_too_large")
        status, headers, stream = storage_read(ud["gokapi_id"])
        sources.append((stream.read(), ud["file_name"], ud["content_type"],
                        ud["size_bytes"]))
        gokapi_ids.append(ud["gokapi_id"])

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

    ip = request_ctx.get("ip", "")
    info = gokapi_result.get("FileInfo", {})
    record = save_upload_record(
        user["id"], ip, info.get("Id", ""),
        result["filename"], result["content_type"], output_size,
        expiry_days, allowed_downloads, info.get("ExpireAt"),
        info.get("UrlHotlink", ""), BASE_URL)
    record["origSize"] = sources[0][0].__len__() if len(sources) == 1 else None

    for gid in gokapi_ids:
        try:
            delete_gokapi_file(gid)
        except Exception:
            logger.warning("Failed to cleanup source %s after tool execution", gid)
    return record


async def execute_remote(request_ctx, tool, uploads, params, user, lifecycle=None):
    """remote (http) 编排: 拉 URL 签名 → drive_http → 零信任落地。

    lifecycle (4A-fix1): 与 execute_local 同协议 — dispatch 在 params
    清洗前捕获, 独立于 canonical tool params 传递。None/缺键 → 默认 1 / 50。
    """
    import remote as _remote
    from security import sign_fetch_token

    decl = json.loads(tool["endpoint_json"]) if tool.get("endpoint_json") else {}
    if not decl.get("url"):
        raise ExecutionError("err_tool_not_configured")
    trusted = (tool.get("owner_id") is None) or bool(tool.get("trusted"))
    sources = []
    gokapi_ids = []
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
        gokapi_ids.append(ud["gokapi_id"])

    auth_headers = {}
    if decl.get("auth_header") and decl.get("auth_token"):
        from secrets_vault import unseal
        auth_headers[decl["auth_header"]] = unseal(decl["auth_token"])

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
    expiry_days, allowed_downloads = validate_lifecycle(lifecycle or {})
    gokapi_result = storage_write(
        io.BytesIO(output_bytes), out["filename"], out["content_type"],
        expiry_days, allowed_downloads, output_size)
    info = gokapi_result.get("FileInfo", {})
    record = save_upload_record(
        user["id"], request_ctx.get("ip", ""), info.get("Id", ""),
        out["filename"], out["content_type"], output_size,
        expiry_days, allowed_downloads, info.get("ExpireAt"),
        info.get("UrlHotlink", ""), BASE_URL)
    for gid in gokapi_ids:
        try:
            delete_gokapi_file(gid)
        except Exception:
            logger.warning("Failed to cleanup source %s after remote execution", gid)
    return record


import json  # noqa: E402
