"""Tool supply domain (阶段 B: Tool Supply Unification).

三源 (system local / operator remote / user remote) 共享同一个
declarative Tool Contract:

    source adapter  →  canonical ToolDeclaration
                        ↓
    validate_tool_declaration()   (declaration 合法性, 无来源政策)
                        ↓
    create_tool / update_tool / upsert_seed_tool   (供给侧单点)
                        ↓
    repo.catalog 通用 row writer  →  platform_tools

三源只允许 ownership / trust policy / execution binding 不同。
来源政策 (user caps / SSRF / self-test gate / operator public /
first-party seed 语义 / pricing) 留在各 source adapter, 不进 validator。

DB 列映射 (declaration → column) 只存在于本文件的 _serialize_row。
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


class DeclarationError(ValueError):
    """canonical ToolDeclaration 校验失败。str(exc) 为错误码。"""


def _check_localized(value, field: str, max_len: int) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if not (0 < len(value) <= max_len):
            raise DeclarationError("bad_label")
        return
    if isinstance(value, dict) and value:
        for v in value.values():
            if not isinstance(v, str) or not (0 < len(v) <= max_len):
                raise DeclarationError("bad_label")
        return
    raise DeclarationError("bad_label")


def validate_tool_declaration(decl: dict) -> dict:
    """校验一个 canonical ToolDeclaration 本身合法 (与来源无关)。

    只验证 declaration shape/语法; 来源政策 (caps/SSRF/visibility gate/
    pricing/namespace) 一律由 source adapter 负责。
    返回归一化后的 declaration dict (浅拷贝)。
    """
    if not isinstance(decl, dict):
        raise DeclarationError("bad_declaration")

    tool_id = str(decl.get("tool_id") or "")
    if not _TOOL_ID_RE.match(tool_id):
        raise DeclarationError("bad_tool_id")

    if decl.get("execution") not in _EXECUTIONS:
        raise DeclarationError("bad_execution")
    if decl.get("tool_type") not in _TOOL_TYPES:
        raise DeclarationError("bad_tool_type")
    if decl.get("visibility") not in _VISIBILITIES:
        raise DeclarationError("bad_visibility")

    from tools import VALID_ICONS
    if decl.get("icon") not in VALID_ICONS:
        raise DeclarationError("bad_icon")

    _check_localized(decl.get("label"), "label", 40)
    _check_localized(decl.get("desc"), "desc", 200)

    accept = decl.get("accept")
    if accept is not None:
        if (not isinstance(accept, (list, tuple)) or not accept
                or not all(isinstance(x, str) and x.startswith(".")
                           and x == x.lower() for x in accept)):
            raise DeclarationError("bad_accept")

    if not isinstance(decl.get("multi", False), bool):
        raise DeclarationError("bad_multi")
    if not isinstance(decl.get("pass_through", False), bool):
        raise DeclarationError("bad_pass_through")
    if not isinstance(decl.get("enabled", True), bool):
        raise DeclarationError("bad_enabled")

    params = decl.get("params")
    if params not in (None, [], ""):
        try:
            validate_params_decl(params)
        except ParamsDeclError as exc:
            raise DeclarationError(str(exc)) from exc

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
        # local: binding 不持久化 (handler 属 runtime), 允许 None
        pass

    tool_key = str(decl.get("tool_key") or "")
    if not tool_key:
        raise DeclarationError("bad_tool_key")

    return dict(decl)


# ── HTTP binding composition 单点 ──

def build_http_binding(fields: dict, existing: Optional[dict] = None) -> dict:
    """从已校验字段组装 HTTP endpoint binding (PATCH 对 existing merge)。

    字段: url / timeout_s / auth_header / auth_token /
    output_filename / output_content_type (由 source policy 决定是否允许,
    本函数不拒绝)。auth secret 只在这里 seal 一次。
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
        "accept_json": (json.dumps(sorted(decl["accept"]), ensure_ascii=False)
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

def create_tool(decl: dict) -> str:
    """校验 + 持久化一个新工具声明 (user / operator 共用)。

    source adapter 已完成政策检查 (duplicate/caps/SSRF 由调用方自行
    决定先后)。返回 tool_key。冲突时抛 repo 层错误。
    """
    decl = validate_tool_declaration(decl)
    from repo.catalog import insert_tool_row
    from utils import now
    with _db() as con:
        insert_tool_row(con, _serialize_row(decl, now()))
    return decl["tool_key"]


def update_tool(tool_key: str, patch: dict) -> None:
    """对既有工具应用 canonical declaration patch。

    patch 值必须是最终 canonical 形态 (LocalizedText locale patch /
    params locale merge 等由 source adapter 先算完)。
    只映射出现于 patch 的字段到 DB 列; updated_at 单点维护。
    """
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
        sets["accept_json"] = (json.dumps(sorted(patch["accept"]), ensure_ascii=False)
                               if patch["accept"] else None)
    if "presets" in patch:
        sets["presets_json"] = (json.dumps(patch["presets"], ensure_ascii=False)
                                if patch["presets"] else None)
    if "params" in patch:
        sets["params_json"] = (json.dumps(patch["params"], ensure_ascii=False)
                               if patch["params"] else None)
    if "multi" in patch:
        sets["multi"] = bool(patch["multi"])
    if "pass_through" in patch:
        sets["pass_through"] = bool(patch["pass_through"])
    if "visibility" in patch:
        sets["visibility"] = patch["visibility"]
    if "enabled" in patch:
        sets["enabled"] = bool(patch["enabled"])
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
