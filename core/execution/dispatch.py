"""execution/dispatch — 统一执行请求入口 (批3a).

api.py / apiv1 / mcp / selftest 四个消费面共用的 execute 请求处理:
解析请求对象 → resolve → gate.run(orchestrate)。消灭 request.state._v1_raw
借道与 routes 互 import (A5)。

调用协议:
    payload = await dispatch_execute(request, user, raw)
    payload: dict → 直接返回; (error_body, status) → JSONResponse。
"""

import json

from config import PUBLIC_ID_RE
from governance.catalog import resolve as resolve_tool, get_default_preset

from . import ExecutionError
from .gate import run as gate_run
from .source import resolve_upload


class _RawRequest:
    """统一请求形状 (dict → 属性), 复用 app.models 的字段约定。"""

    def __init__(self, raw: dict):
        self.tool_id = str(raw.get("tool_id") or "")
        self.params = raw.get("params") or {}
        self.public_id = str(raw.get("public_id") or "")
        self.public_ids = [str(x) for x in (raw.get("public_ids") or [])][:20]
        self.source_token = str(raw.get("source_token") or "")
        self.source_tokens = [str(x) for x in (raw.get("source_tokens") or [])][:20]
        self.client_ref = str(raw.get("client_ref") or "")


def _client_ip(request) -> str:
    return request.headers.get("X-Real-IP",
                               request.client.host if request.client else "").strip()


async def dispatch_execute(request, user: dict, raw: dict, process_tool=None, pool=None):
    """返回 (payload_dict | None, error_body | None, status | None)。"""
    tool_id = str(raw.get("tool_id") or "")
    params = raw.get("params") or {}

    tool, terr = resolve_tool(tool_id, user["id"])
    if terr:
        if terr == "disabled":
            return None, {"ok": False, "error": "tool_disabled"}, 403
        return None, {"ok": False, "error": "tool_not_found"}, 404
    tool["id"] = tool["tool_key"]
    tool["type"] = tool["tool_type"]

    if "preset" not in params:
        default = get_default_preset(tool_id)
        if default:
            params["preset"] = default

    req = _RawRequest(raw)
    request_ctx = {"ip": _client_ip(request)}

    async def orchestrate(p):
        uploads = []
        if tool.get("multi") and req.public_ids:
            if len(req.public_ids) > 20:
                raise ExecutionError("err_too_many_files")
            for i, pid in enumerate(req.public_ids):
                if not PUBLIC_ID_RE.match(pid):
                    raise ExecutionError("invalid_id")
                token = req.source_tokens[i] if i < len(req.source_tokens) else ""
                upload, rerr = await resolve_upload(pid, user, token)
                if rerr or not upload:
                    raise ExecutionError(rerr or "not_found")
                uploads.append(upload)
        else:
            public_id = req.public_id
            if not public_id:
                raise ExecutionError("invalid_id")
            if not PUBLIC_ID_RE.match(public_id):
                raise ExecutionError("invalid_id")
            upload, rerr = await resolve_upload(public_id, user, req.source_token)
            if rerr or not upload:
                raise ExecutionError(rerr or "not_found")
            uploads.append(upload)

        from .orchestrator import execute_local, execute_remote
        from tools import ToolError as _TE
        try:
            if tool.get("execution") in ("http", "mcp"):
                return await execute_remote(request_ctx, tool, uploads, p, user)
            # local: worker pool 由调用方注入 (web 进程内单例)
            if process_tool is None or pool is None:
                from app.routes.api import _get_pool  # noqa: 只是池引用
                from app.worker import process_tool as _pt
                return await execute_local(request_ctx, tool, uploads, p, user,
                                           _pt, _get_pool())
            return await execute_local(request_ctx, tool, uploads, p, user,
                                       process_tool, pool)
        except _TE as exc:
            raise ExecutionError(str(exc)) from exc

    return await gate_run(user, tool, req, orchestrate)
