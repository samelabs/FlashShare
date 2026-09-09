"""User-submitted tools (C2C supply side, phase 3).

Self-service manifest submission: a user registers their OWN http
endpoint as a namespaced tool (`{username}/{tool_id}`). No human gate —
visibility is an owner-set field. The platform thesis: new capabilities
enter as DATA rows.

Safety model (no review queue):
  - SSRF pre-check at submission: https + resolved public IPs required
    (same guard the driver applies per-call; fail early, fail typed)
  - Landing identity: output_filename required with whitelisted ext
  - Per-user caps: max 20 tools, price 0..100000
  - Zero-trust output: sniff whitelist at every landing, no exemptions
  - Economics: provider earns (price - commission) per successful call;
    rate lives in settings (commission_rate, default 30%)

Ownership boundaries:
  - tool_key namespace = owner username at creation time (renames do not
    re-key; the row's owner_id is the authority, tool_key is addressing)
  - pricing rows are written with updated_by = owner; only the owner (or
    /samelabs) may change them — set_pricing stays the single writer.
"""

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.web_auth import require_session_or_bearer_auth as _require_auth  # 单点 (治理)

from database import db
from i18n import SERVER_I18N as _SUPPORTED_LANGS  # 发布端多语言键白名单
from governance.user_tools import (
    MAX_TOOLS_PER_USER, _owner_rows, _public_item, _call_stats,
)
from governance.formats import FAMILIES, FAMILY_ORDER, validate_family_key
from governance.tool_params import (patch_locale, patch_locale_guard,
                                     resolve_label)
from utils import now

router = APIRouter()

MAX_PRICE = 100000
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
ACCEPT_MIME_RE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


def _validate_submission(raw: dict, partial: bool = False, trusted_ok: bool = False,
                         edit_locale: str = "") -> tuple:
    """Shared validation for create (full) and patch (partial).
    Returns (fields, error_code). fields keys only for provided values."""
    fields = {}

    if "tool_id" in raw and not partial:
        tid = str(raw.get("tool_id") or "").strip()
        if not TOOL_ID_RE.match(tid):
            return None, "bad_tool_id"

    if "label" in raw or not partial:
        label = raw.get("label")
        # 双形态 (tool modes v3 语言模型): str=任意语言原文 | dict=多语言集合
        if isinstance(label, str):
            label = label.strip()
            # Web locale patch 下空串 = 显式删除当前 locale (批 2), 放行;
            # 其余路径 (POST / 无头 API PATCH) 工具名必填维持 bad_label
            if not label and not (partial and edit_locale):
                return None, "bad_label"
            if len(label) > 40:
                return None, "bad_label"
            fields["label"] = label
        elif isinstance(label, dict):
            clean = {}
            for lang, v in label.items():
                if lang not in _SUPPORTED_LANGS or not isinstance(v, str):
                    return None, "bad_label"
                v = v.strip()
                # Web locale patch: 成员空串 = 显式删除该 locale (批 2c),
                # 放行空串由 handler patch_locale 删键; 其余路径维持非空约束
                if not v and not (partial and edit_locale):
                    return None, "bad_label"
                if len(v) > 40:
                    return None, "bad_label"
                clean[lang] = v
            if not clean:
                return None, "bad_label"
            fields["label"] = clean
        else:
            return None, "bad_label"

    if "url" in raw or not partial:
        url = str(raw.get("url") or "").strip()
        # https+public-IP is the user-row contract; trusted (operator-adopted)
        # rows are exempt at runtime and on edit — flag set by the caller.
        if not trusted_ok and not url.startswith("https://"):
            return None, "https_required"
        if not url.startswith(("http://", "https://")):
            return None, "bad_url"
        fields["url"] = url

    if "timeout_s" in raw or not partial:
        try:
            timeout_s = int(raw.get("timeout_s") or 60)
        except (ValueError, TypeError):
            return None, "bad_timeout"
        if not (5 <= timeout_s <= 300):
            return None, "bad_timeout"
        fields["timeout_s"] = timeout_s

    if raw.get("auth_header") and raw.get("auth_token"):
        # 存原文; seal 只在 _endpoint_decl 组装时做一次 (🔴4 修复: 原双重加密)
        fields["auth_header"] = str(raw["auth_header"]).strip()[:64]
        fields["auth_token"] = str(raw["auth_token"]).strip()[:512]

    if "accept" in raw or "format_families" in raw or not partial:
        # 4C-2 Tool Input Contract: format_families 保留为 authoring taxonomy;
        # accept 是 exact executable contract — 存库永远写 sorted canonical
        # extension tokens。旧客户端只发 family (无 accept) → 解释为整族。
        fam, ferr = validate_family_key(raw.get("format_families"))
        if ferr:
            return None, ferr
        fam_exts = set(FAMILIES[fam].exts)
        # accept 缺失 vs 显式空数组语义 (4C-2-close):
        #   raw 无 accept 键 / None → legacy client: 整族 canonical ext
        #   显式 [] / 非 list / 空 token 结果 → bad_accept (显式空选择
        #   不得静默扩大成整族)
        raw_accept = raw.get("accept")
        if "accept" not in raw or raw_accept is None:
            canon = set(fam_exts)
        else:
            if not isinstance(raw_accept, list):
                return None, "bad_accept"
            from governance.formats import extensions_for_accept
            try:
                canon = extensions_for_accept(raw_accept)
            except (KeyError, TypeError):
                return None, "bad_accept"
            if not canon:
                return None, "bad_accept"
            # exact tokens 必须全部属于所选 family (operator remote 才允许跨族)
            if not canon <= fam_exts:
                return None, "bad_accept"
        fields["accept"] = sorted(canon)

    # 4C-2: multi 是标准 ToolDeclaration capability (不是 params)。
    # create 缺失 → false; 显式值必须是真 JSON boolean; PATCH 仅出现时更新。
    if "multi" in raw or not partial:
        multi = raw.get("multi", False)
        if not isinstance(multi, bool):
            return None, "bad_multi"
        fields["multi"] = multi

    if "output_filename" in raw:
        # 文件名是开发者 API 自定义机制, 平台不限定。
        # 字段不再写入 (raw-bytes 兜底名由执行层按产物嗅探生成)。
        return None, "bad_output_filename"

    if "output_content_type" in raw:
        # 输出由端点决定, 平台不声明。字段不再写入 (存量 decl 原样保留)。
        return None, "bad_output_content_type"

    if "visibility" in raw or not partial:
        vis = str(raw.get("visibility") or "private").strip()
        if vis not in ("private", "unlisted", "public"):
            return None, "bad_visibility"
        fields["visibility"] = vis

    if "cost" in raw or not partial:
        try:
            cost = int(raw.get("cost") or 0)
        except (ValueError, TypeError):
            return None, "bad_cost"
        if not (0 <= cost <= MAX_PRICE):
            return None, "bad_cost"
        fields["cost"] = cost

    return fields, None


def _endpoint_decl(fields: dict, execution: str, existing: dict | None = None) -> dict:
    """Compose endpoint_json from validated fields (merge for patch)."""
    decl = dict(existing or {})
    if "url" in fields:
        decl["url"] = fields["url"]
    if "timeout_s" in fields:
        decl["timeout_s"] = fields["timeout_s"]
    if "auth_header" in fields:
        decl["auth_header"] = fields["auth_header"]
    if "auth_token" in fields:
        from secrets_vault import seal
        decl["auth_token"] = seal(fields["auth_token"])
    # output_filename/output_content_type 由端点决定, 不入库
    # (存量 decl 中已有值原样保留在 existing_decl 里)。
    return decl


@router.get("/api/tools/search")
async def search_tools(request: Request, q: str = ""):
    """Home tool search — one list, self-operated first, community next.
    Anonymous: public rows only. Signed-in: own private/unlisted included."""
    from governance.catalog import search_catalog
    user = request.state.user
    viewer = user["id"] if user else None
    return {"ok": True, "tools": search_catalog(q, viewer)}  # noqa: E501 (search_catalog caps at 30)


@router.get("/api/usertools")
async def list_user_tools(request: Request):
    """Owner's own tools with live stats."""
    user, err = _require_auth(request)
    if err:
        return err
    return {"ok": True, "tools": [_public_item(r) for r in _owner_rows(user["id"])]}


@router.post("/api/usertools")
async def create_user_tool(request: Request):
    """Submit a tool manifest. New tools land as visibility=private by
    default — the owner flips to unlisted/public after self-testing.
    SSRF pre-check runs here so misconfiguration fails at submission,
    not at first customer call."""
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)

    tool_id = str(raw.get("tool_id") or "").strip()
    if not TOOL_ID_RE.match(tool_id):
        return JSONResponse({"ok": False, "error": "bad_tool_id"}, 422)
    # Supply-side contract: ONE execution class — plain HTTP API.
    # (mcp forwarding removed 2026-08-26: agent-native MCP servers don't
    # need a broker; 518 IS the MCP surface northbound.)
    execution = "http"

    fields, ferr = _validate_submission(raw)
    if ferr:
        return JSONResponse({"ok": False, "error": ferr}, 422)

    # SSRF pre-check (typed failure at submission time)
    from remote import guard_endpoint, RemoteError
    try:
        guard_endpoint(fields["url"], trusted=False)
    except RemoteError as exc:
        code = "https_required" if exc.code == "ssrf_blocked" else "bad_url"
        return JSONResponse({"ok": False, "error": code}, 422)

    # New tools ALWAYS land private (self-test before exposure)
    fields["visibility"] = "private"

    # params 声明 (tool modes spec v3): 可选, 提交时全量校验, 存库即合法
    # 简介 (tool modes v3: 名称/简介/模式/使用): 可选, str|dict 双形态, ≤200/项
    desc = raw.get("desc")
    if desc is not None:
        if isinstance(desc, dict):
            desc = {k: str(v).strip()[:200] for k, v in desc.items()
                    if k in _SUPPORTED_LANGS and str(v).strip()} or None
        else:
            desc = str(desc).strip()[:200] or None

    params_decl = []
    if raw.get("params") not in (None, "", []):
        from governance.tool_params import validate_params_decl, ParamsDeclError
        try:
            params_decl = validate_params_decl(raw.get("params"))
        except ParamsDeclError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)

    from repo.catalog import tool_key_exists, count_by_owner, insert_user_tool
    tool_key = f"{user['username']}/{tool_id}"
    with db() as con:
        if tool_key_exists(con, tool_key):
            return JSONResponse({"ok": False, "error": "duplicate_tool"}, 409)
        if count_by_owner(con, user["id"]) >= MAX_TOOLS_PER_USER:
            return JSONResponse({"ok": False, "error": "too_many_tools"}, 429)

        ts = now()
        label = fields["label"]
        insert_user_tool(
            con, tool_key, user["id"], tool_id, execution,
            json.dumps(fields["label"] if isinstance(fields["label"], dict)
                       else {"en": fields["label"]}, ensure_ascii=False),
            json.dumps(fields.get("accept")) if fields.get("accept") else None,
            fields["visibility"],
            json.dumps(_endpoint_decl(fields, execution)), ts,
            params_json=json.dumps(params_decl, ensure_ascii=False) if params_decl else None,
            desc_json=json.dumps(desc if isinstance(desc, dict) else {"en": desc},
                                 ensure_ascii=False) if desc else None,
            multi=bool(fields.get("multi", False)))
    # pricing row: owner-priced from day one
    from governance.pricing import set_pricing
    set_pricing(tool_key, fields["cost"], True, admin_user_id=user["id"])
    return {"ok": True, "tool_key": tool_key}


@router.get("/api/usertools/{tool_id}/calls")
async def tool_call_log(tool_id: str, request: Request):
    """B3: provider-visible call log for THEIR tool (last 20).
    Owner-scoped; no caller identity beyond status/latency — privacy."""
    user, err = _require_auth(request)
    if err:
        return err
    from repo.catalog import owner_row_by_key
    from repo.ledger import calls_by_tool_excluding_owner
    tool_key = f"{user['username']}/{tool_id}"
    with db() as con:
        row = owner_row_by_key(con, tool_key, user["id"])
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        calls = [dict(r) for r in calls_by_tool_excluding_owner(
            con, tool_key, user["id"], limit=20)]
    for cl in calls:
        f, s = cl.pop("finished_at", None), cl.pop("started_at", None)
        cl["ms"] = (f - s) if (f is not None and s is not None) else None
    return {"ok": True, "calls": calls}


@router.post("/api/usertools/{tool_id}/selftest")
async def selftest_tool(tool_id: str, request: Request):
    """4A-fix4: compatibility thin adapter over the REAL tool consumption path.

    UI 自测已收口到真实工具页 (/tool/{owner}/{tool_id} → canonical params →
    runToolFlow → /api/tools/execute → dispatch → gate → real endpoint)。
    本 endpoint 仅为兼容保留: 同一 dispatch/gate, 无免费测试侧路。

    params:
      - 缺失/null → {} (canonical validator 注入声明 default)
      - dict      → 原样交给 dispatch, canonical validator 校验
      - 非 dict   → 422 invalid_request (沿用现有错误协议, 不发明新协议)
    client_ref:
      - 显式传   → 原样使用, 支持幂等重试 (gate replay)
      - 未传     → 留空 (gate 自动生成本次唯一 ref) — 每次 selftest 真实执行,
                   不会因固定 ref 第二次起全部被 replay 吞掉
    """
    user, err = _require_auth(request)
    if err:
        return err
    tool_key = f"{user['username']}/{tool_id}"
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    public_id = str(raw.get("public_id") or "").strip()
    source_token = str(raw.get("source_token") or "").strip()
    if not public_id:
        return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
    params = raw.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    import time as _t
    t0 = _t.time()
    # Same single gate as any call (批3a: 直调 execution 域 dispatch)
    from execution.dispatch import dispatch_execute
    body_raw = {
        "tool_id": tool_key,
        "params": params,
        "public_id": public_id,
        "source_token": source_token,
    }
    client_ref = raw.get("client_ref")
    if client_ref:
        body_raw["client_ref"] = str(client_ref)
    payload, error_body, status = await dispatch_execute(request, user, body_raw)
    ms = int((_t.time() - t0) * 1000)
    body = payload if (payload is not None) else (error_body or {"ok": False, "error": "internal_error"})
    if isinstance(body, dict):
        body["ms"] = ms
        if error_body is not None:
            return JSONResponse(body, status_code=status)
    return body


@router.patch("/api/usertools/{tool_id}")
async def update_user_tool(tool_id: str, request: Request):
    """Partial update: label/url/timeout/auth/accept/output identity/
    visibility/enabled/cost. Endpoint changes re-run the SSRF pre-check.

    LocalizedText 写入语义 (批 2 协议, 二选一无灰色地带):
      携 X-518-Edit-Locale (Web 表单):
        str 与 dict 均为 locale patch — 只写提及的 locale, 未提及语言不动;
        空值 = 显式删除该 locale; 提交值 == resolver fallback → 未修改不写。
      无头 (API/manifest):
        dict = 显式全集替换 (未提及语言删除); str 归一为 {"en": str}。
    params 机器结构 (key/type/default/min/max/options.value/required):
      两种语义下均为全量 authoritative — 结构增删以提交为准。
    params 的三文本叶子 (label/placeholder/options[].label):
      Web Form (携 X-518-Edit-Locale): 对旧声明做 locale merge —
        parameter 按 key 对齐, option 按 value 对齐, 禁止 index。
      API / manifest (无头): 显式全集替换, 不启用 locale merge。
    """
    user, err = _require_auth(request)
    if err:
        return err
    from repo.catalog import owner_row_by_key, update_fields
    tool_key = f"{user['username']}/{tool_id}"
    with db() as con:
        row = owner_row_by_key(con, tool_key, user["id"])
    if not row:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if not TOOL_ID_RE.match(tool_id or ""):
        return JSONResponse({"ok": False, "error": "bad_tool_id"}, 422)

    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)

    # LocalizedText 编辑保护 (批 2): Web 表单显式携带当前编辑 locale 才启用
    # patch 语义 (str 只更新该 locale, 保留其余语言)。无此头的 API PATCH
    # 保持既有替换语义 — Web 语义不扩散到 API manifest。
    edit_locale = (request.headers.get("X-518-Edit-Locale") or "").strip()
    if edit_locale and edit_locale not in _SUPPORTED_LANGS:
        return JSONResponse({"ok": False, "error": "bad_edit_locale"}, 422)

    fields, ferr = _validate_submission(raw, partial=True,
                                        trusted_ok=bool(row["trusted"]),
                                        edit_locale=edit_locale)
    if ferr:
        return JSONResponse({"ok": False, "error": ferr}, 422)

    existing_decl = {}
    try:
        existing_decl = json.loads(row["endpoint_json"] or "{}")
    except Exception:
        pass

    # visibility cannot jump private→public directly (self-test gate:
    # at least one successful self-call recorded first)
    new_vis = fields.get("visibility")
    if new_vis == "public" and row["visibility"] != "public":
        stats = _call_stats(tool_key, row["owner_id"])
        if stats["ok"] < 1:
            return JSONResponse({"ok": False, "error": "self_test_required"}, 422)

    # endpoint change → SSRF re-check (批4 #14: 对 MERGED 后的最终 decl 复检,
    # 与运行时驱动看到的完全一致, 而非裸入参)
    merged_decl = _endpoint_decl(fields, row["execution"], existing_decl)
    if "url" in fields and not row["trusted"]:
        from remote import guard_endpoint, RemoteError
        try:
            guard_endpoint(merged_decl["url"], trusted=False)
        except RemoteError:
            return JSONResponse({"ok": False, "error": "https_required"}, 422)

    sets = {}
    if "label" in fields:
        if edit_locale:
            # Web locale patch 语义 (批 2 协议, 唯一):
            #   携 X-518-Edit-Locale 的 PATCH 上, str 与 dict 都是 locale patch —
            #   str 只写该 locale; dict 逐 locale 覆写; 未提及的语言一律不动。
            #   空值 = 显式删除该 locale。
            # 无此头的 API PATCH = 替换语义 (dict 即全集, 见下方 else 分支)。
            try:
                old_label = json.loads(row["label_json"] or "{}")
            except Exception:
                old_label = {}
            new_label = fields["label"]
            if isinstance(new_label, dict):
                # fallback no-change 判定永远基于 old_snapshot (原始旧 dict),
                # 与 merged 累积状态无关 — locale 间互不顺序依赖 (批 2c)
                merged = dict(old_label) if isinstance(old_label, dict) else {}
                for lg, txt in new_label.items():
                    if lg not in _SUPPORTED_LANGS:
                        continue
                    merged = patch_locale_guard(old_label, merged, lg, str(txt or ""))
                # dict 全空提交 (全部清空) → 不写, 保留原值
                if isinstance(merged, dict) and merged:
                    sets["label_json"] = json.dumps(merged, ensure_ascii=False)
            else:
                # fallback 物化防护: 提交值 == 旧值在当前 locale 的 resolver
                # 结果 → 发布者未修改 (表单显示的就是 fallback), 不写。
                cur = resolve_label(old_label, edit_locale)
                if cur and str(new_label or "").strip() == cur:
                    pass  # 未修改 — 保留原 dict, 不创建/覆写 locale 键
                else:
                    merged = patch_locale(old_label, edit_locale, str(new_label or ""))
                    if isinstance(merged, dict) and merged:
                        sets["label_json"] = json.dumps(merged, ensure_ascii=False)
                    elif isinstance(merged, str) and merged:
                        sets["label_json"] = json.dumps({"en": merged}, ensure_ascii=False)
                    # merged 为 "" (删光了所有语言) → 不写 label, 保留原值
        else:
            sets["label_json"] = json.dumps(
                fields["label"] if isinstance(fields["label"], dict)
                else {"en": fields["label"]}, ensure_ascii=False)
    if "visibility" in fields:
        sets["visibility"] = fields["visibility"]
    if "accept" in fields:
        sets["accept_json"] = json.dumps(fields["accept"]) if fields["accept"] else None
    if "multi" in fields:  # 4C-2: PATCH 仅在提交 multi 时更新
        sets["multi"] = bool(fields["multi"])
    if "enabled" in raw:
        sets["enabled"] = bool(raw.get("enabled"))
    if "desc" in raw:
        d = raw.get("desc")
        if edit_locale:
            # Web locale patch: str 与 dict 同协议 (见 label 分支注释) —
            # str 只写该 locale + fallback 物化防护; dict 逐 locale merge。
            try:
                old_desc = json.loads(row["desc_json"] or "{}")
            except Exception:
                old_desc = {}
            old_dict = old_desc if isinstance(old_desc, dict) else {}
            cur = resolve_label(old_desc, edit_locale) if old_desc else ""
            if isinstance(d, dict):
                # 同 label: no-change 基于 old_snapshot, 顺序无关
                merged = dict(old_dict)
                for lg, txt in d.items():
                    if lg not in _SUPPORTED_LANGS:
                        continue
                    merged = patch_locale_guard(old_dict, merged, lg, str(txt or "").strip()[:200])
                sets["desc_json"] = json.dumps(merged, ensure_ascii=False) if merged else None
            else:
                new_txt = str(d or "").strip()[:200]
                if cur and new_txt == cur:
                    pass  # 未修改 — 保留原 dict
                else:
                    merged = patch_locale(old_dict, edit_locale, new_txt)
                    sets["desc_json"] = json.dumps(merged, ensure_ascii=False) if merged else None
        elif isinstance(d, dict):
            clean = {k: str(v).strip()[:200] for k, v in d.items()
                     if k in _SUPPORTED_LANGS and str(v).strip()}
            sets["desc_json"] = json.dumps(clean, ensure_ascii=False) if clean else None
        else:
            d = str(d or "").strip()[:200]
            sets["desc_json"] = json.dumps({"en": d}, ensure_ascii=False) if d else None
    if "params" in raw:
        # params 声明 (spec v3): 结构全量替换; Web 编辑时三个 LocalizedText
        # 叶子 (label/placeholder/options[].label) 对旧声明做 locale merge —
        # parameter 按 key 对齐, option 按 value 对齐, 禁止按 index。
        from governance.tool_params import (validate_params_decl, ParamsDeclError,
                                            merge_params_decl)
        try:
            decl = validate_params_decl(raw.get("params"))
        except ParamsDeclError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)
        if edit_locale and decl:
            try:
                old_decl = json.loads(row["params_json"] or "[]")
            except Exception:
                old_decl = []
            decl = merge_params_decl(old_decl, decl, edit_locale)
        sets["params_json"] = json.dumps(decl, ensure_ascii=False) if decl else None
    if sets or fields:
        sets["endpoint_json"] = json.dumps(merged_decl)
        sets["updated_at"] = now()
        with db() as con:
            update_fields(con, tool_key, sets)
    if "cost" in fields:
        from governance.pricing import set_pricing
        set_pricing(tool_key, fields["cost"], True, admin_user_id=user["id"])
    return {"ok": True}


@router.delete("/api/usertools/{tool_id}")
async def delete_user_tool(tool_id: str, request: Request):
    """Remove a tool row. pricing flips disabled (history interpretable)."""
    user, err = _require_auth(request)
    if err:
        return err
    from repo.catalog import owner_row_by_key, delete_tool_row
    tool_key = f"{user['username']}/{tool_id}"
    with db() as con:
        row = owner_row_by_key(con, tool_key, user["id"])
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        delete_tool_row(con, row["id"])
    from governance.pricing import set_pricing
    set_pricing(tool_key, 0, False, admin_user_id=user["id"])
    return {"ok": True}
