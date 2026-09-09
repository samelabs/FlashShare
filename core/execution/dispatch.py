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
    """统一请求形状 (dict → 属性), 复用 app.models 的字段约定。

    params 规范化 (4A-fix2): 缺失/None/{} 统一为独立 dict, 非 dict
    ([]/"abc"/123) → .params = None, 由 dispatch 以 422 invalid_request
    拒绝 — 避免 .clear()/.update() 在非 dict 上 500。调用方必须只从
    req.params 读写 params, 禁止清洗后再次从 raw 重建本对象 (空 dict
    falsy, `raw.get("params") or {}` 两次调用会得到两个不同 dict)。
    """

    def __init__(self, raw: dict):
        self.tool_id = str(raw.get("tool_id") or "")
        p = raw.get("params")
        if p is None:
            self.params = {}
        elif isinstance(p, dict):
            self.params = p
        else:
            self.params = None
        self.public_id = str(raw.get("public_id") or "")
        self.public_ids = [str(x) for x in (raw.get("public_ids") or [])][:20]
        self.source_token = str(raw.get("source_token") or "")
        self.source_tokens = [str(x) for x in (raw.get("source_tokens") or [])][:20]
        self.client_ref = str(raw.get("client_ref") or "")


def client_ip(request) -> str:
    return request.headers.get("X-Real-IP",
                               request.client.host if request.client else "").strip()


async def dispatch_execute(request, user: dict, raw: dict, process_tool=None, pool=None):
    """返回 (payload_dict | None, error_body | None, status | None)。"""
    # 4A-fix2: 先构造规范化 req — tool_id/params/source ids/client_ref 全部
    # 从同一个 req 读取, params 是唯一事实对象。此前 `raw.get("params") or {}`
    # 出现两次: 空 dict 为 falsy, 两次各自创建脱离 raw 的新 dict — canonical
    # default 写入第一个 dict, gate 却从重建后的 req 读第二个空 dict, 用户
    # remote tool 的 default 真实丢失 (compress 靠 handler fallback 掩盖)。
    req = _RawRequest(raw)
    if req.params is None:  # 非 dict params — 受控 422, 不 500
        return None, {"ok": False, "error": "invalid_request"}, 422

    tool, terr = resolve_tool(req.tool_id, user["id"])
    if terr:
        if terr == "disabled":
            return None, {"ok": False, "error": "tool_disabled"}, 403
        return None, {"ok": False, "error": "tool_not_found"}, 404
    tool["id"] = tool["tool_key"]
    tool["type"] = tool["tool_type"]

    params = req.params

    # 批 4A: canonical params 是唯一参数事实源 — 有声明时 default 由
    # enforce_params_decl 注入, 不再走 presets 通道 (get_default_preset 仅
    # 为存量无声明工具保留 compatibility shim)。
    import json as _json
    try:
        decl = _json.loads(tool.get("params_json") or "[]")
    except (TypeError, ValueError):
        decl = []
    if not decl and "preset" not in params:
        default = get_default_preset(req.tool_id)
        if default:
            params["preset"] = default

    # 4A-fix1: 平台生命周期字段 (expiry_days/allowed_downloads) 是请求 shape
    # 的一部分 (Web/API/MCP 仍放在 params 中), 不属于 canonical tool params
    # 声明 — enforce_params_decl 会剥离声明外键。必须在清洗前捕获, 与 tool
    # params 分开传给 orchestrator, 否则 canonical 工具的产物保存会回落到
    # 默认 1 天 / 50 次。校验仍由 orchestrator.validate_lifecycle 执行。
    lifecycle = {k: params.get(k) for k in ("expiry_days", "allowed_downloads")
                 if k in params}

    # tool modes v3 M4: 按 params_json 声明校验+剥离 (来源无关, 单点在此)。
    # 无声明的工具 params 保持自由直通 (兼容现状)。preset/expiry 等系统键
    # 属于 presets/生命周期机制, 不受声明管辖 (lifecycle 已在清洗前捕获)。
    # 4A-fix2: enforce 结果直接写入 req.params (同一对象), gate 从 req.params
    # 读取 — 清洗前不再有第二个 params 对象。
    if decl:
        from governance.tool_params import enforce_params_decl, ParamsValueError
        try:
            declared = enforce_params_decl(decl, params)
        except ParamsValueError as exc:
            return None, {"ok": False, "error": str(exc)}, 422
        params.clear()
        params.update(declared)

    request_ctx = {"ip": client_ip(request)}

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

        # ── 格式边界闸门 (声明即契约, 执行即强制) ──
        # 声明来源: local 工具 resolve() 附 handler.accept; 用户工具在
        # accept_json (行 dict 无 accept 键)。None=全部, 不设闸。
        # 单点收口: web/token/MCP/selftest 四面共用本函数, 无旁路。
        # 4C-1: 精确 accept — declared MIME → 具体扩展名集合 (formats.
        # MIME_EXTS 单一词表), 上传 ext 必须 ∈ 该集合。不再经
        # families_for_mimes() 扩大成 family (application/pdf 只放 .pdf)。
        declared = tool.get("accept")
        if declared is None and tool.get("accept_json"):
            try:
                declared = json.loads(tool["accept_json"])
            except (TypeError, ValueError):
                declared = None
        if declared:
            from governance.formats import extensions_for_mimes
            try:
                allowed_exts = extensions_for_mimes(declared)
            except KeyError:
                allowed_exts = None  # 声明含词表外 MIME (存量脏数据): 不拦截, 不 500
            if allowed_exts:
                import os as _os
                for u in uploads:
                    ext = _os.path.splitext(str(u.get("file_name", "")).lower())[1]
                    if ext not in allowed_exts:
                        raise ExecutionError("err_format_mismatch")

        from .orchestrator import execute_local, execute_remote
        from tools import ToolError as _TE
        try:
            if tool.get("execution") in ("http", "mcp"):
                return await execute_remote(request_ctx, tool, uploads, p, user,
                                            lifecycle)
            # local: worker pool 由调用方注入; 未注入 → 执行域自持池
            if process_tool is None or pool is None:
                from .pool import get_pool, default_process_tool
                return await execute_local(request_ctx, tool, uploads, p, user,
                                           default_process_tool(), get_pool(),
                                           lifecycle)
            return await execute_local(request_ctx, tool, uploads, p, user,
                                       process_tool, pool, lifecycle)
        except _TE as exc:
            raise ExecutionError(str(exc)) from exc

    return await gate_run(user, tool, req, orchestrate)
