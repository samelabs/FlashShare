"""execution/dispatch — 统一执行请求入口 (Stage C: canonical ToolInvocation).

Web(api.py) / REST v1 / MCP 三面构造完全相同的 canonical ToolInvocation,
dispatch 只认识这一种 schema:

    parse_tool_invocation(raw)
        ↓
    resolve tool
        ↓
    enforce source count vs tool.multi
        ↓
    resolve source.files
        ↓
    enforce accept
        ↓
    enforce params
        ↓
    gate (client_ref 幂等/扣费)
        ↓
    orchestrator (delivery)

旧协议 (flat source 字段 / delivery 混入 params) 已删除 —
无 compatibility adapter。

调用协议:
    payload = await dispatch_execute(request, user, raw)
    payload: dict → 直接返回; (error_body, status) → JSONResponse。
"""

import json

from config import PUBLIC_ID_RE
from governance.catalog import resolve as resolve_tool

from . import ExecutionError
from .gate import run as gate_run
from .invocation import parse_tool_invocation, InvocationError
from .source import resolve_upload


def client_ip(request) -> str:
    return request.headers.get("X-Real-IP",
                               request.client.host if request.client else "").strip()


async def dispatch_execute(request, user: dict, raw: dict, process_tool=None, pool=None):
    """返回 (payload_dict | None, error_body | None, status | None)。"""
    # Stage C: 单点 parser — root 非 dict / 旧 shape / 未知字段 / 非法
    # delivery 全部在此受控 422, 不 500。
    try:
        inv = parse_tool_invocation(raw)
    except InvocationError:
        return None, {"ok": False, "error": "invalid_request"}, 422

    # delivery entitlement authorization — 在 protocol validity 之后、
    # tool resolve/params/gate 之前。entitlement denial 是 403 语义,
    # 与 protocol invalid (422) 分开, 不合并回 validate_delivery。
    from governance.entitlements import check_delivery
    denial = check_delivery(user["id"], inv.delivery)
    if denial:
        body = {"ok": False}
        body.update(denial)
        return None, body, 403

    tool, terr = resolve_tool(inv.tool_id, user["id"])
    if terr:
        if terr == "disabled":
            return None, {"ok": False, "error": "tool_disabled"}, 403
        return None, {"ok": False, "error": "tool_not_found"}, 404
    tool["id"] = tool["tool_key"]
    tool["type"] = tool["tool_type"]

    params = inv.params

    # canonical params 是唯一参数事实源 — default 由 enforce_params_decl
    # 注入。delivery 属 invocation 边界字段, 永不出现在 params。
    import json as _json
    try:
        decl = _json.loads(tool.get("params_json") or "[]")
    except (TypeError, ValueError):
        decl = []

    # tool modes v3 M4: 按 params_json 声明校验+剥离 (来源无关, 单点在此)。
    # 无声明的工具 params 保持自由直传 (兼容现状)。
    if decl:
        from governance.tool_params import enforce_params_decl, ParamsValueError
        try:
            declared = enforce_params_decl(decl, params)
        except ParamsValueError as exc:
            return None, {"ok": False, "error": str(exc)}, 422
        params.clear()
        params.update(declared)

    request_ctx = {"ip": client_ip(request)}

    async def orchestrate():
        # ── source resolve: 只认 source.files[] ──
        # multi=false: len(files) 必须 == 1; multi=true: parser 已保证 1..20。
        uploads = []
        multi = bool(tool.get("multi"))
        if not multi and len(inv.files) > 1:
            raise ExecutionError("err_too_many_files")
        for f in inv.files:
            if not PUBLIC_ID_RE.match(f.public_id):
                raise ExecutionError("invalid_id")
            upload, rerr = await resolve_upload(f.public_id, user, f.source_token)
            if rerr or not upload:
                raise ExecutionError(rerr or "not_found")
            uploads.append(upload)

        # ── 格式边界闸门 (声明即契约, 执行即强制) ──
        # 声明来源: local 工具 resolve() 附 handler.accept; 用户工具在
        # accept_json (行 dict 无 accept 键)。None=全部, 不设闸。
        # 单点收口: web/token/MCP 三面共用本函数, 无旁路。
        # 4C-2: canonical AcceptToken — declared tokens (canonical ext 或
        # legacy MIME) → extensions_for_accept 精确扩展名集合, 上传 ext 必须
        # ∈ 该集合。
        declared_accept = tool.get("accept")
        if declared_accept is None and tool.get("accept_json"):
            try:
                declared_accept = json.loads(tool["accept_json"])
            except (TypeError, ValueError):
                declared_accept = None
        if declared_accept:
            from governance.formats import extensions_for_accept
            try:
                allowed_exts = extensions_for_accept(declared_accept)
            except (KeyError, TypeError):
                # 声明含词表外 token (存量脏数据/裸串): 保持线上容错,
                # 整体不拦截, 不产生 500。
                allowed_exts = None
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
                return await execute_remote(request_ctx, tool, uploads,
                                            params, user, inv.delivery)
            # local: worker pool 由调用方注入; 未注入 → 执行域自持池
            if process_tool is None or pool is None:
                from .pool import get_pool, default_process_tool
                return await execute_local(request_ctx, tool, uploads,
                                           params, user,
                                           default_process_tool(), get_pool(),
                                           inv.delivery)
            return await execute_local(request_ctx, tool, uploads, params, user,
                                       process_tool, pool, inv.delivery)
        except _TE as exc:
            raise ExecutionError(str(exc)) from exc

    return await gate_run(user, tool, inv, orchestrate)
