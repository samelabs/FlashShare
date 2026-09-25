"""User-submitted tools (C2C supply side, phase 3).

Self-service manifest submission: a user registers their OWN http
endpoint as a namespaced tool (`{username}/{tool_id}`). No human gate —
visibility is an owner-set field. The platform thesis: new capabilities
enter as DATA rows.

Safety model (no review queue):
  - SSRF pre-check at submission: https + resolved public IPs required
    (same guard the driver applies per-call; fail early, fail typed)
  - Landing identity: endpoint-product-owned — output_filename /
    output_content_type are NOT declaration fields (explicit submission
    → typed 422 bad_output_filename / bad_output_content_type)
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

from app.web_auth import require_session_auth as _require_auth  # 单点 (治理)

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
        # 存原文; seal 只在 tool_supply.build_http_binding 组装时做一次
        fields["auth_header"] = str(raw["auth_header"]).strip()[:64]
        fields["auth_token"] = str(raw["auth_token"]).strip()[:512]
        # Credential-header authority boundary (user tools only): the
        # transport owns Host/framing — a developer credential may never
        # claim those names. Trusted operator rows keep header freedom.
        from remote import auth_header_allowed
        if not auth_header_allowed(fields["auth_header"]):
            return None, "bad_auth_header"

    if "accept" in raw or "format_families" in raw or not partial:
        # 4C-2 Tool Input Contract: format_families 保留为 authoring taxonomy;
        # accept 是 exact executable contract — 存库永远写 sorted canonical
        # extension tokens。accept 必须显式声明 (见下)。
        fam, ferr = validate_family_key(raw.get("format_families"))
        if ferr:
            return None, ferr
        fam_exts = set(FAMILIES[fam].exts)
        # accept 是唯一 executable input contract, 必须显式声明:
        #   缺失 / None / 非 list / 空 list / 空 token 结果 → bad_accept
        #   (不猜测, 不从 family 扩面)。format_families 只是 authoring
        #   taxonomy, 不再替 accept 生成 contract。
        raw_accept = raw.get("accept")
        if "accept" not in raw or raw_accept is None:
            return None, "bad_accept"
        if not isinstance(raw_accept, list):
            return None, "bad_accept"
        from governance.formats import extensions_for_accept
        try:
            canon = extensions_for_accept(raw_accept)
        except (KeyError, TypeError, ValueError):
            return None, "bad_accept"
        # 声明的 accept token 必须落在所选 family 的 canonical ext 内
        if not canon <= fam_exts:
            return None, "bad_accept"
        fields["accept"] = sorted(canon)

    # IO-1 input/output contract: files count window + output kinds.
    # create defaults to 1..1 / ["file"]; PATCH updates only on presence.
    if "files_min" in raw or "files_max" in raw or not partial:
        mn = raw.get("files_min", 1)
        mx = raw.get("files_max", 1)
        if isinstance(mn, bool) or not isinstance(mn, int) or mn < 0:
            return None, "bad_files_min"
        if isinstance(mx, bool) or not isinstance(mx, int) \
                or not (1 <= mx <= 20):
            return None, "bad_files_max"
        if mn > mx:
            return None, "bad_files_range"
        fields["files_min"] = mn
        fields["files_max"] = mx
    if "outputs" in raw or not partial:
        outputs = raw.get("outputs", ["file"])
        if not isinstance(outputs, list) or not outputs \
                or any(k not in ("text", "json", "file") for k in outputs):
            return None, "bad_outputs"
        fields["outputs"] = [k for k in ("text", "json", "file")
                             if k in set(outputs)]

    if "output_filename" in raw:
        # 文件名是开发者 API 自定义机制, 平台不限定。
        # 字段不再写入 (文件名由端点 items 声明, 平台不限定)。
        return None, "bad_output_filename"

    if "output_content_type" in raw:
        # 输出由端点决定, 平台不声明 — 提交该字段即拒绝。
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


# 阶段 B: HTTP binding 组装单点在 governance.tool_supply.build_http_binding
# (_endpoint_decl 已删除, seal 只在 supply 层发生一次)。


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

    # Credential boundary: a NEW non-empty secret cannot be persisted
    # when the vault has no working key — refuse before any write.
    if str(raw.get("auth_token") or "").strip():
        from secrets_vault import vault_enabled
        if not vault_enabled():
            return JSONResponse(
                {"ok": False, "error": "credential_vault_unavailable"}, 503)
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

    # 阶段 B-close: params 不再提前 validate — raw 直入 declaration,
    # 由 create_tool → validate_tool_declaration 单点校验/normalize。
    # (PATCH 的 edit-locale params merge 仍可调 validate_params_decl:
    #  transformation prerequisite, 非 declaration validator。)
    params_decl = raw.get("params") or None

    # user policy → canonical declaration → tool_supply 单点 (阶段 B)
    from repo.catalog import tool_key_exists, count_by_owner, lock_user_row
    from governance.tool_supply import create_tool, build_http_binding, DeclarationError
    tool_key = f"{user['username']}/{tool_id}"
    label = fields["label"]
    declaration = {
        "tool_key": tool_key,
        "tool_id": tool_id,
        "owner_id": user["id"],
        "execution": execution,
        "tool_type": "produce",
        "label": label if isinstance(label, dict) else {"en": label},
        "desc": desc if isinstance(desc, dict) else ({"en": desc} if desc else None),
        "icon": "wand",
        "inputs": {"files": {
            "min": int(fields.get("files_min", 1)),
            "max": int(fields.get("files_max", 1)),
            "accept": fields.get("accept")}},
        "outputs": fields.get("outputs") or ["file"],
        "params": params_decl or None,
        "visibility": fields["visibility"],
        "enabled": True,
        "binding": build_http_binding(
            {k: fields[k] for k in
             ("url", "timeout_s", "auth_header", "auth_token") if k in fields}),
    }
    try:
        # Stage B-close: policy check + declaration + commercial contract
        # 同一事务; 对 owner 行 SELECT ... FOR UPDATE 串行化同 owner 并发
        # create, 保证 MAX_TOOLS_PER_USER 在并发下不被突破 (READ COMMITTED
        # 下裸 COUNT 会双读 19)。cap 是 user policy, 不进 supply domain。
        from governance.pricing import create_tool_with_pricing
        with db() as con:
            lock_user_row(con, user["id"])
            if tool_key_exists(con, tool_key):
                return JSONResponse({"ok": False, "error": "duplicate_tool"}, 409)
            if count_by_owner(con, user["id"]) >= MAX_TOOLS_PER_USER:
                return JSONResponse({"ok": False, "error": "too_many_tools"}, 429)
            create_tool_with_pricing(declaration, fields["cost"],
                                     admin_user_id=user["id"], con=con)
    except DeclarationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad_cost"}, 422)
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
    # Explicit response shape (whitelist — the failure text is NOT
    # exposed: it can carry caller-side detail such as their balance):
    #   at            epoch seconds (tool_calls.started_at); the client
    #                 renders it in the viewer's local time
    #   status        running | ok | failed | insufficient
    #   duration_s    finished_at - started_at (both epoch seconds);
    #                 None while running
    #   cost_snapshot claim-time price of the call (Credits)
    out = []
    for cl in calls:
        f, s = cl.get("finished_at"), cl.get("started_at")
        out.append({
            "at": int(s) if s is not None else None,
            "status": cl.get("status"),
            "duration_s": (int(f) - int(s))
            if (f is not None and s is not None) else None,
            "cost_snapshot": cl.get("cost_snapshot"),
        })
    return {"ok": True, "calls": out}


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

    from repo.catalog import owner_row_for_update
    tool_key = f"{user['username']}/{tool_id}"
    # Snapshot + declaration + pricing mutations share ONE transaction:
    # the row lock keeps the snapshot the mutation actually applies to.
    # (declaration update 与 pricing 同 commit / 同 rollback。)
    with db() as con:
        row = owner_row_for_update(con, tool_key, user["id"])
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        if not TOOL_ID_RE.match(tool_id or ""):
            return JSONResponse({"ok": False, "error": "bad_tool_id"}, 422)

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

        # Credential boundary: a NEW non-empty secret cannot be persisted
        # when the vault has no working key — refuse before any write
        # (endpoint_json/pricing untouched).
        if str(fields.get("auth_token") or "").strip():
            from secrets_vault import vault_enabled
            if not vault_enabled():
                return JSONResponse(
                    {"ok": False, "error": "credential_vault_unavailable"}, 503)

        # endpoint change → SSRF re-check (批4 #14: 对 MERGED 后的最终 decl 复检,
        # 与运行时驱动看到的完全一致, 而非裸入参)
        from governance.tool_supply import build_http_binding
        merged_decl = build_http_binding(
            {k: fields[k] for k in
             ("url", "timeout_s", "auth_header", "auth_token") if k in fields},
            existing_decl)
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
                        sets["label"] = merged
                else:
                    # fallback 物化防护: 提交值 == 旧值在当前 locale 的 resolver
                    # 结果 → 发布者未修改 (表单显示的就是 fallback), 不写。
                    cur = resolve_label(old_label, edit_locale)
                    if cur and str(new_label or "").strip() == cur:
                        pass  # 未修改 — 保留原 dict, 不创建/覆写 locale 键
                    else:
                        merged = patch_locale(old_label, edit_locale, str(new_label or ""))
                        if isinstance(merged, dict) and merged:
                            sets["label"] = merged
                        elif isinstance(merged, str) and merged:
                            sets["label"] = {"en": merged}
                        # merged 为 "" (删光了所有语言) → 不写 label, 保留原值
            else:
                sets["label"] = (fields["label"] if isinstance(fields["label"], dict)
                    else {"en": fields["label"]})
        if "visibility" in fields:
            sets["visibility"] = fields["visibility"]
        if "accept" in fields:
            sets["accept"] = fields["accept"]
        if "files_min" in fields or "files_max" in fields:
            sets["files_min"] = fields["files_min"]
            sets["files_max"] = fields["files_max"]
        if "outputs" in fields:
            sets["outputs"] = fields["outputs"]
        if "enabled" in raw:
            # 不做 bool(raw) 强转 — 非 bool 交 supply patch validator 拒
            # (bad_enabled), 与 declaration 语义一致。
            sets["enabled"] = raw.get("enabled")
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
                    sets["desc"] = merged or None
                else:
                    new_txt = str(d or "").strip()[:200]
                    if cur and new_txt == cur:
                        pass  # 未修改 — 保留原 dict
                    else:
                        merged = patch_locale(old_dict, edit_locale, new_txt)
                        sets["desc"] = merged or None
            elif isinstance(d, dict):
                clean = {k: str(v).strip()[:200] for k, v in d.items()
                         if k in _SUPPORTED_LANGS and str(v).strip()}
                sets["desc"] = clean or None
            else:
                d = str(d or "").strip()[:200]
                sets["desc"] = {"en": d} if d else None
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
            sets["params"] = decl or None
        # 阶段 B: canonical patch → tool_supply 单点序列化/写库
        # (merged_decl = build_http_binding 产物, binding 结构与 operator 一致)
        if sets or fields:
            if row["execution"] == "http":
                sets["binding"] = merged_decl
            from governance.tool_supply import update_tool
            update_tool(tool_key, sets, con=con)

        if "cost" in fields:
            from governance.pricing import set_pricing
            set_pricing(tool_key, fields["cost"], True,
                        admin_user_id=user["id"], con=con)
    return {"ok": True}


@router.delete("/api/usertools/{tool_id}")
async def delete_user_tool(tool_id: str, request: Request):
    """Remove a tool row. pricing flips disabled (history interpretable)."""
    user, err = _require_auth(request)
    if err:
        return err
    from repo.catalog import owner_row_for_update, delete_tool_row
    tool_key = f"{user['username']}/{tool_id}"
    # Tool row deletion and pricing disable share ONE transaction —
    # a pricing write failure rolls the deletion back with it.
    with db() as con:
        row = owner_row_for_update(con, tool_key, user["id"])
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        delete_tool_row(con, row["id"])
        from governance.pricing import set_pricing
        set_pricing(tool_key, 0, False, admin_user_id=user["id"], con=con)
    return {"ok": True}
