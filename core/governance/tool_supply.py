"""Tool supply domain (阶段 B: Tool Supply Unification; Stage B-close 收口).

三源 (system local / operator remote / user remote) 共享同一个
declarative Tool Contract:

    source adapter  →  canonical ToolDeclaration
                        ↓
    validate_tool_declaration()   (declaration 合法性 + normalization,
                                   无来源政策)
    validate_tool_patch()         (update patch 字段级同规则校验)
                        ↓
    create_tool / update_tool / upsert_seed_tool   (供给侧单点)
                        ↓
    repo.catalog 通用 row writer  →  platform_tools

三源只允许 ownership / trust policy / execution binding 不同。
来源政策 (user caps / SSRF / self-test gate / operator public /
first-party seed 语义 / pricing) 留在各 source adapter, 不进 validator。

DB 列映射 (declaration → column) 只存在于本文件的 _serialize_row。
HTTP binding composition 与 auth seal 唯一入口 = build_http_binding。
create_tool(update 同理)支持 caller-owned transaction (con=);
policy 事务与 persistence 可同事务, cap 逻辑本身不进 supply domain。
"""

import json
from typing import Optional

from governance.tool_params import validate_params_decl, ParamsDeclError

# ── canonical 词汇 (复用既有单源) ──
import re as _re
_TOOL_ID_RE = _re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_EXECUTIONS = ("local", "http")
_TOOL_TYPES = ("view", "peek", "produce")
_VISIBILITIES = ("private", "unlisted", "public")
_MAX_LABEL = 40
_MAX_DESC = 200


class DeclarationError(ValueError):
    """canonical ToolDeclaration 校验失败。str(exc) 为错误码。"""


def _check_localized(value, max_len: int, required: bool) -> str | dict | None:
    """LocalizedText canonical shape 校验。返回归一化值。

    str: 非空且 <= max_len; dict: 非空且每值为非空 str <= max_len。
    required=False 时 None 通过。错误码沿用 bad_label (API 兼容)。
    """
    if value is None:
        if required:
            raise DeclarationError("bad_label")
        return None
    if isinstance(value, str):
        if not (0 < len(value) <= max_len):
            raise DeclarationError("bad_label")
        return value
    if isinstance(value, dict) and value:
        for v in value.values():
            if not isinstance(v, str) or not (0 < len(v) <= max_len):
                raise DeclarationError("bad_label")
        return value
    raise DeclarationError("bad_label")


def _normalize_accept(accept) -> Optional[list]:
    """canonical accept: None → None; 非 list/tuple/空 → bad_accept;
    每个元素必须是 lowercase ext 且 ∈ formats.VOCAB_EXTS;
    归一化为 sorted unique list。legacy MIME 兼容属 source/read adapter,
    不属于 canonical validator。"""
    from governance.formats import VOCAB_EXTS
    if accept is None:
        return None
    if not isinstance(accept, (list, tuple)) or not accept:
        raise DeclarationError("bad_accept")
    out = set()
    for x in accept:
        if not isinstance(x, str) or x != x.lower() or x not in VOCAB_EXTS:
            raise DeclarationError("bad_accept")
        out.add(x)
    return sorted(out)


def _normalize_params(params):
    """params 经 validate_params_decl 并采用其 canonical 输出。"""
    if params in (None, [], ""):
        return None
    try:
        return validate_params_decl(params) or None
    except ParamsDeclError as exc:
        raise DeclarationError(str(exc)) from exc


def validate_tool_declaration(decl: dict) -> dict:
    """校验并归一化一个 canonical ToolDeclaration (与来源无关)。

    返回值即**可持久化** canonical declaration (accept sorted unique /
    params canonical / label+desc 校验归一)。来源政策一律由
    source adapter 负责。
    """
    if not isinstance(decl, dict):
        raise DeclarationError("bad_declaration")

    tool_id = str(decl.get("tool_id") or "")
    if not _TOOL_ID_RE.match(tool_id):
        raise DeclarationError("bad_tool_id")
    tool_key = str(decl.get("tool_key") or "")
    if not tool_key:
        raise DeclarationError("bad_tool_key")

    if decl.get("execution") not in _EXECUTIONS:
        raise DeclarationError("bad_execution")
    if decl.get("tool_type") not in _TOOL_TYPES:
        raise DeclarationError("bad_tool_type")
    if decl.get("visibility") not in _VISIBILITIES:
        raise DeclarationError("bad_visibility")

    from tools import VALID_ICONS
    icon = decl.get("icon")
    if icon not in VALID_ICONS:
        raise DeclarationError("bad_icon")

    label = _check_localized(decl.get("label"), _MAX_LABEL, required=True)
    desc = _check_localized(decl.get("desc"), _MAX_DESC, required=False)

    accept = _normalize_accept(decl.get("accept"))

    for key in ("multi", "pass_through"):
        if not isinstance(decl.get(key, False), bool):
            raise DeclarationError(f"bad_{key}")
    if not isinstance(decl.get("enabled", True), bool):
        raise DeclarationError("bad_enabled")

    params = _normalize_params(decl.get("params"))

    presets = decl.get("presets")
    if presets is not None and not isinstance(presets, list):
        raise DeclarationError("bad_presets")

    owner_id = decl.get("owner_id")
    if owner_id is not None and not isinstance(owner_id, int):
        raise DeclarationError("bad_owner")

    binding = decl.get("binding")
    if decl["execution"] == "http":
        if not isinstance(binding, dict) or not binding.get("url"):
            raise DeclarationError("bad_binding")
        url = str(binding["url"])
        if not url.startswith(("http://", "https://")):
            raise DeclarationError("bad_binding")
        ts = binding.get("timeout_s", 60)
        if not isinstance(ts, int) or not (5 <= ts <= 300):
            raise DeclarationError("bad_binding")
    else:
        binding = None  # local: handler 属 runtime, binding 不持久化

    return {
        "tool_key": tool_key,
        "tool_id": tool_id,
        "owner_id": owner_id,
        "execution": decl["execution"],
        "tool_type": decl["tool_type"],
        "label": label,
        "desc": desc,
        "icon": icon,
        "accept": accept,
        "multi": bool(decl.get("multi", False)),
        "pass_through": bool(decl.get("pass_through", False)),
        "params": params,
        "presets": presets,
        "visibility": decl["visibility"],
        "enabled": bool(decl.get("enabled", True)),
        "binding": binding,
    }


def validate_tool_patch(patch: dict) -> dict:
    """update patch 字段级 canonical 校验/归一化 (规则复用 full
    declaration 的 field validators, 不复制第二份 contract 表)。

    patch 不要求完整 declaration, 但出现的字段必须 canonical。
    禁止静默 bool()/类型强转 — 非法值必须抛 DeclarationError。
    """
    if not isinstance(patch, dict):
        raise DeclarationError("bad_patch")
    out = {}
    if "label" in patch:
        out["label"] = _check_localized(patch["label"], _MAX_LABEL, required=True)
    if "desc" in patch:
        out["desc"] = _check_localized(patch["desc"], _MAX_DESC, required=False)
    if "icon" in patch:
        from tools import VALID_ICONS
        if patch["icon"] not in VALID_ICONS:
            raise DeclarationError("bad_icon")
        out["icon"] = patch["icon"]
    if "accept" in patch:
        out["accept"] = _normalize_accept(patch["accept"])
    if "params" in patch:
        out["params"] = _normalize_params(patch["params"])
    if "presets" in patch:
        if patch["presets"] is not None and not isinstance(patch["presets"], list):
            raise DeclarationError("bad_presets")
        out["presets"] = patch["presets"]
    for key in ("multi", "pass_through"):
        if key in patch:
            if not isinstance(patch[key], bool):
                raise DeclarationError(f"bad_{key}")
            out[key] = patch[key]
    if "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            raise DeclarationError("bad_enabled")
        out["enabled"] = patch["enabled"]
    if "visibility" in patch:
        if patch["visibility"] not in _VISIBILITIES:
            raise DeclarationError("bad_visibility")
        out["visibility"] = patch["visibility"]
    if "binding" in patch:
        b = patch["binding"]
        if b is not None:
            if not isinstance(b, dict) or not b.get("url"):
                raise DeclarationError("bad_binding")
            if not str(b["url"]).startswith(("http://", "https://")):
                raise DeclarationError("bad_binding")
            ts = b.get("timeout_s", 60)
            if not isinstance(ts, int) or not (5 <= ts <= 300):
                raise DeclarationError("bad_binding")
        out["binding"] = b
    return out


# ── HTTP binding composition 单点 ──

def build_http_binding(fields: dict, existing: Optional[dict] = None) -> dict:
    """从已校验字段组装 HTTP endpoint binding (PATCH 对 existing merge)。

    字段: url / timeout_s / auth_header / auth_token /
    output_filename / output_content_type (由 source policy 决定是否允许,
    本函数不拒绝)。auth secret 只在这里 seal 一次 (全仓 supply 路径
    唯一 seal 入口)。
    """
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
    if "output_filename" in fields:
        decl["output_filename"] = fields["output_filename"]
    if "output_content_type" in fields:
        decl["output_content_type"] = fields["output_content_type"]
    return decl


# ── serialization mapping (declaration → DB row) — 全仓唯一一份 ──

def _serialize_row(decl: dict, ts: int) -> dict:
    """canonical declaration → platform_tools 列值。唯一 DB 列映射点。"""
    binding = decl.get("binding")
    return {
        "tool_key": decl["tool_key"],
        "owner_id": decl.get("owner_id"),
        "tool_id": decl["tool_id"],
        "execution": decl["execution"],
        "tool_type": decl["tool_type"],
        "label_json": json.dumps(decl["label"], ensure_ascii=False),
        "desc_json": (json.dumps(decl["desc"], ensure_ascii=False)
                      if decl.get("desc") else None),
        "icon": decl["icon"],
        "accept_json": (json.dumps(decl["accept"], ensure_ascii=False)
                        if decl.get("accept") else None),
        "presets_json": (json.dumps(decl["presets"], ensure_ascii=False)
                         if decl.get("presets") else None),
        "params_json": (json.dumps(decl["params"], ensure_ascii=False)
                        if decl.get("params") else None),
        "multi": bool(decl.get("multi", False)),
        "pass_through": bool(decl.get("pass_through", False)),
        "visibility": decl["visibility"],
        "endpoint_json": json.dumps(binding) if decl["execution"] == "http" else None,
        "enabled": bool(decl.get("enabled", True)),
        "ts": ts,
    }


# ── persistence service (供给侧单点) ──

def create_tool(decl: dict, con=None) -> str:
    """校验 + 持久化一个新工具声明 (user / operator 共用)。

    con 未传: 自开事务; con 已传: 参与 caller 的事务 (policy check 与
    persistence 同事务)。返回 tool_key。
    """
    decl = validate_tool_declaration(decl)
    from repo.catalog import insert_tool_row
    from utils import now
    if con is not None:
        insert_tool_row(con, _serialize_row(decl, now()))
        return decl["tool_key"]
    with _db() as own:
        insert_tool_row(own, _serialize_row(decl, now()))
    return decl["tool_key"]


def update_tool(tool_key: str, patch: dict) -> None:
    """对既有工具应用 canonical declaration patch。

    patch 先经 validate_tool_patch (字段级 canonical, 不静默强转);
    LocalizedText locale patch / params locale merge 等由 source
    adapter 先算完 final canonical value。DB 列序列化/updated_at 单点。
    """
    patch = validate_tool_patch(patch)
    from repo.catalog import update_tool_row
    from utils import now
    sets = {}
    if "label" in patch:
        sets["label_json"] = json.dumps(patch["label"], ensure_ascii=False)
    if "desc" in patch:
        sets["desc_json"] = (json.dumps(patch["desc"], ensure_ascii=False)
                             if patch["desc"] else None)
    if "icon" in patch:
        sets["icon"] = patch["icon"]
    if "accept" in patch:
        sets["accept_json"] = (json.dumps(patch["accept"], ensure_ascii=False)
                               if patch["accept"] else None)
    if "presets" in patch:
        sets["presets_json"] = (json.dumps(patch["presets"], ensure_ascii=False)
                                if patch["presets"] else None)
    if "params" in patch:
        sets["params_json"] = (json.dumps(patch["params"], ensure_ascii=False)
                               if patch["params"] else None)
    if "multi" in patch:
        sets["multi"] = patch["multi"]
    if "pass_through" in patch:
        sets["pass_through"] = patch["pass_through"]
    if "visibility" in patch:
        sets["visibility"] = patch["visibility"]
    if "enabled" in patch:
        sets["enabled"] = patch["enabled"]
    if "binding" in patch:
        sets["endpoint_json"] = (json.dumps(patch["binding"])
                                 if patch["binding"] else None)
    if not sets:
        return
    sets["updated_at"] = now()
    with _db() as con:
        update_tool_row(con, tool_key, sets)


def upsert_seed_tool(decl: dict) -> bool:
    """first-party seed upsert (保留行身份/可见性语义)。

    返回 True=inserted / False=updated。
    """
    decl = validate_tool_declaration(decl)
    from repo.catalog import upsert_seed_tool_row
    from utils import now
    with _db() as con:
        row = upsert_seed_tool_row(con, _serialize_row(decl, now()))
    return bool(row["inserted"])


def _db():
    from database import db
    return db()
