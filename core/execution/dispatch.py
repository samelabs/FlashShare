"""execution/dispatch — 统一执行请求入口 (the canonical-invocation unification: canonical ToolInvocation).

Web(api.py) / MCP 两面构造完全相同的 canonical ToolInvocation,
dispatch 只认识这一种 schema:

    parse_tool_invocation(raw)
        ↓
    resolve tool
        ↓
    enforce source count vs the declared files window
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


def preflight(user: dict, tool_id: str, delivery: dict, params: dict):
    """Pre-gate validation of one canonical invocation's non-source parts:
    delivery entitlement → tool resolve → stored contract → params decl.

    Single authority for dispatch_execute AND the MCP adapter (which runs
    it BEFORE any source ingress, so a request this rejects never uploads
    or claims quota). Pure: no claim, no charge, no network.

    Returns (tool, declared_params | None, None, None) on success, where
    declared_params is the enforce_params_decl result (None = the tool
    declares no params, params pass through); or (None, None, error_body,
    status) on denial."""
    # delivery entitlement authorization — 在 protocol validity 之后、
    # tool resolve/params/gate 之前。entitlement denial 是 403 语义,
    # 与 protocol invalid (422) 分开, 不合并回 validate_delivery。
    from governance.entitlements import check_delivery
    denial = check_delivery(user["id"], delivery)
    if denial:
        body = {"ok": False}
        body.update(denial)
        return None, None, body, 403

    tool, terr = resolve_tool(tool_id, user["id"])
    if terr:
        if terr == "disabled":
            return None, None, {"ok": False, "error": "tool_disabled"}, 403
        return None, None, {"ok": False, "error": "tool_not_found"}, 404
    tool["id"] = tool["tool_key"]
    tool["type"] = tool["tool_type"]

    # Stored-contract validation BEFORE the metering gate, via THE
    # single stored parser (formats.parse_stored_accept): SQL NULL = any;
    # anything non-NULL that is not a valid non-empty canonical ext list
    # ('' / 'null' / '[]' / JSON object / scalar / MIME / unknown token)
    # is a DB invariant violation — reject without claiming a tool_call,
    # charging credits, executing, or touching the network. This is not
    # a public error contract for dirty data; it mirrors the startup
    # catalog validator at the execution boundary.
    from governance.formats import parse_stored_accept
    if tool.get("accept_json") is not None:
        try:
            parse_stored_accept(tool["accept_json"])
        except (KeyError, TypeError, ValueError):
            return None, None, {"ok": False, "error": "tool_unavailable"}, 503

    # Stored execution class: exactly the canonical supply vocabulary
    # (local | http). Anything else — including the retired 'mcp' notion —
    # is a stored contract violation: rejected HERE, before the metering
    # gate (no claim, no charge, no network) and with no default-else
    # local fallback. Mirrors the startup catalog invariant.
    from governance.tool_supply import _EXECUTIONS
    if tool.get("execution") not in _EXECUTIONS:
        return None, None, {"ok": False, "error": "tool_unavailable"}, 503

    # Stored outputs declaration : the runtime enforcement point
    # (orchestrator) parses the same stored JSON — a corrupt declaration
    # is refused HERE before any claim/charge/network, mirroring the
    # accept/execution stored-contract checks above.
    from .results import declared_outputs, OutputsError
    try:
        declared_outputs(tool)
    except OutputsError:
        return None, None, {"ok": False, "error": "tool_unavailable"}, 503

    # canonical params 是唯一参数事实源 — default 由 enforce_params_decl
    # 注入。delivery 属 invocation 边界字段, 永不出现在 params。
    import json as _json
    try:
        decl = _json.loads(tool.get("params_json") or "[]")
    except (TypeError, ValueError):
        # stored-contract corruption: a broken declaration must NEVER be
        # silently treated as "no declaration" (free passthrough) — the
        # call is refused before any claim/charge/network
        return None, None, {"ok": False, "error": "tool_unavailable"}, 503

    # tool modes v3 M4: 按 params_json 声明校验+剥离 (来源无关, 单点在此)。
    # params 声明是可选能力: 无声明的工具 params 自由直传 — 但序列化
    # 尺寸仍受同一 TEXT_PARAM_MAX_CHARS 约束 (claim/扣费之前拒绝)。
    if decl:
        from governance.tool_params import enforce_params_decl, ParamsValueError
        try:
            return tool, enforce_params_decl(decl, params), None, None
        except ParamsValueError as exc:
            return None, None, {"ok": False, "error": str(exc)}, 422
    from governance.tool_params import TEXT_PARAM_MAX_CHARS
    try:
        params_len = len(_json.dumps(params, ensure_ascii=False))
    except (TypeError, ValueError):
        return None, None, {"ok": False, "error": "invalid_request"}, 422
    if params_len > TEXT_PARAM_MAX_CHARS:
        return None, None, {"ok": False, "error": "param_too_long"}, 422
    return tool, None, None, None


def source_count_error(tool: dict, n: int) -> str | None:
    """File-count window from the declaration: [files_min, files_max].
    0 is legal for text-only tools."""
    mn = int(tool.get("files_min", 1) or 0)
    mx = int(tool.get("files_max", 1) or 1)
    if not (mn <= n <= mx):
        return "err_file_count"
    return None


def source_accept_error(tool: dict, filenames: list) -> str | None:
    """格式边界闸门 (声明即契约, 执行即强制) — single check for the
    execution boundary and the MCP pre-ingress preflight.

    声明来源: local 工具 resolve() 附 handler.accept; 用户工具在
    accept_json (行 dict 无 accept 键)。None=全部, 不设闸。
    4C-2: canonical AcceptToken — declared tokens (canonical ext)
    → extensions_for_accept 精确扩展名集合, 上传 ext 必须 ∈ 该集合。
    preflight() already validated accept_json: here it can only be SQL
    NULL=any or a valid canonical token list (single stored parser)."""
    declared = tool.get("accept")
    if declared is None and tool.get("accept_json") is not None:
        from governance.formats import parse_stored_accept as _psa
        declared = _psa(tool["accept_json"]) or []
        declared = sorted(declared)
    if declared:
        from governance.formats import extensions_for_accept as _efa
        allowed_exts = _efa(declared)
        if allowed_exts:
            import os as _os
            for fn in filenames:
                ext = _os.path.splitext(str(fn or "").lower())[1]
                if ext not in allowed_exts:
                    return "err_format_mismatch"
    return None


async def dispatch_execute(request, user: dict, raw: dict, process_tool=None, pool=None):
    """返回 (payload_dict | None, error_body | None, status | None)。"""
    # the canonical-invocation unification: 单点 parser — root 非 dict / 旧 shape / 未知字段 / 非法
    # delivery 全部在此受控 422, 不 500。
    try:
        inv = parse_tool_invocation(raw)
    except InvocationError:
        return None, {"ok": False, "error": "invalid_request"}, 422

    tool, declared, err_body, err_status = preflight(
        user, inv.tool_id, inv.delivery, inv.params)
    if err_body is not None:
        return None, err_body, err_status

    params = inv.params
    if declared is not None:
        params.clear()
        params.update(declared)

    request_ctx = {"ip": client_ip(request)}

    async def orchestrate():
        # ── source resolve: 只认 source.files[] ──
        # multi=false: len(files) 必须 == 1; multi=true: parser 已保证 1..20。
        uploads = []
        count_err = source_count_error(tool, len(inv.files))
        if count_err:
            raise ExecutionError(count_err)
        for f in inv.files:
            if not PUBLIC_ID_RE.match(f.public_id):
                raise ExecutionError("invalid_id")
            upload, rerr = await resolve_upload(f.public_id, user, f.source_token)
            if rerr or not upload:
                raise ExecutionError(rerr or "not_found")
            uploads.append(upload)

        # 格式边界闸门 — 单点收口: web/token/MCP 三面共用, 无旁路。
        accept_err = source_accept_error(
            tool, [u.get("file_name", "") for u in uploads])
        if accept_err:
            raise ExecutionError(accept_err)

        from .orchestrator import execute_local, execute_remote
        from tools import ToolError as _TE

        # Stored execution class already validated pre-gate (canonical
        # vocabulary only, no default-else local branch). Here: exactly
        # the two admitted classes.
        _exec = tool.get("execution")
        if _exec == "http":
            try:
                return await execute_remote(request_ctx, tool, uploads,
                                            params, user, inv.delivery)
            except _TE as exc:
                raise ExecutionError(str(exc)) from exc
        # local (validated): worker pool 由调用方注入; 未注入 → 执行域自持池
        if process_tool is None or pool is None:
            from .pool import get_pool, default_process_tool
            _pt, _pl = default_process_tool(), get_pool()
        else:
            _pt, _pl = process_tool, pool
        try:
            return await execute_local(request_ctx, tool, uploads,
                                       params, user, _pt, _pl,
                                       inv.delivery)
        except _TE as exc:
            raise ExecutionError(str(exc)) from exc

    return await gate_run(user, tool, inv, orchestrate)
