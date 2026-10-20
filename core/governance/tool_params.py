"""Params declaration validation (tool modes spec v3, 2026-09-02).

One validator for the params_json declaration — the ONLY place that knows
the contract. 提交时全量校验, 存库即合法, 读取面永不二次校验 (单点原则)。
来源无关: 协议只看数据形态 (label: str | dict), 不看发布者/执行类。
"""
import math
import re

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TYPES = ("enum", "text", "number", "boolean")

# text parameters accept long prose input (constant upper bound)
TEXT_PARAM_MAX_CHARS = 100_000
MAX_PARAMS = 8
MAX_OPTIONS = 32
MAX_LABEL = 64


class ParamsDeclError(ValueError):
    """Carries a machine-readable error code (→ 422 body)."""


def _check_label(label, field: str) -> None:
    if isinstance(label, str):
        if not (0 < len(label) <= MAX_LABEL):
            raise ParamsDeclError("label_too_long")
        return
    if isinstance(label, dict):
        if not label:
            raise ParamsDeclError("bad_params_json")
        for v in label.values():
            if not isinstance(v, str) or not (0 < len(v) <= MAX_LABEL):
                raise ParamsDeclError("label_too_long")
        return
    raise ParamsDeclError("bad_params_json")


def validate_params_decl(raw) -> list:
    """Validate a params declaration (list of dicts). Returns the normalized
    decl (same shape). Raises ParamsDeclError with a machine code on any
    contract violation."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ParamsDeclError("bad_params_json")
    if len(raw) > MAX_PARAMS:
        raise ParamsDeclError("params_too_many")

    seen_keys = set()
    out = []
    for p in raw:
        if not isinstance(p, dict):
            raise ParamsDeclError("bad_params_json")
        key = str(p.get("key") or "").strip()
        if not KEY_RE.match(key):
            raise ParamsDeclError("bad_param_key")
        if key in seen_keys:
            raise ParamsDeclError("bad_param_key")
        seen_keys.add(key)
        ptype = str(p.get("type") or "").strip()
        if ptype not in _TYPES:
            raise ParamsDeclError("bad_param_type")
        _check_label(p.get("label", key), "label")

        item = {"key": key, "type": ptype, "label": p.get("label", key),
                "required": bool(p.get("required", False))}

        if ptype == "enum":
            options = p.get("options")
            if (not isinstance(options, list) or not options
                    or len(options) > MAX_OPTIONS):
                raise ParamsDeclError("bad_param_options")
            values = []
            norm_opts = []
            for o in options:
                if not isinstance(o, dict) or "value" not in o:
                    raise ParamsDeclError("bad_param_options")
                v = str(o["value"])
                if not v or v in values:
                    raise ParamsDeclError("bad_param_options")
                values.append(v)
                norm_opts.append({"value": v, "label": o.get("label", v)})
                _check_label(o.get("label", v), "options.label")
            item["options"] = norm_opts
            default = p.get("default")
            if default is not None and str(default) not in values:
                raise ParamsDeclError("bad_param_default")
            if default is not None:
                item["default"] = str(default)
        elif ptype == "number":
            try:
                if p.get("min") is not None:
                    item["min"] = float(p["min"])
                if p.get("max") is not None:
                    item["max"] = float(p["max"])
                if p.get("default") is not None:
                    item["default"] = float(p["default"])
            except (TypeError, ValueError):
                raise ParamsDeclError("bad_param_default")
            # float() happily accepts inf/nan — those are not numbers a
            # schema can carry: JSON serialization of the declaration
            # would fail downstream (catalog projections, MCP tools/list)
            if not all(math.isfinite(item[k]) for k in ("min", "max", "default")
                       if item.get(k) is not None):
                raise ParamsDeclError("bad_param_default")
            if (item.get("min") is not None and item.get("max") is not None
                    and item["min"] > item["max"]):
                raise ParamsDeclError("bad_param_default")
            # default must satisfy its own bounds: execution injects the
            # default verbatim (no re-validation there), so an
            # out-of-range default would sail straight into the handler
            if item.get("default") is not None:
                if item.get("min") is not None and item["default"] < item["min"]:
                    raise ParamsDeclError("bad_param_default")
                if item.get("max") is not None and item["default"] > item["max"]:
                    raise ParamsDeclError("bad_param_default")
        elif ptype == "boolean":
            if p.get("default") is not None:
                item["default"] = bool(p["default"])
        else:  # text
            if p.get("placeholder") is not None:
                _check_label(p["placeholder"], "placeholder")
                item["placeholder"] = p["placeholder"]
            if p.get("default") is not None:
                item["default"] = str(p["default"])
        out.append(item)
    return out


def resolve_label(label, lang: str) -> str:
    """LocalizedText protocol resolver.

    Accepted shapes:
        str
        dict[str, str]   (locale -> text)

    Resolution order (the canonical platform fallback chain):
        requested locale -> "en" -> first non-empty value -> ""

    Callers MAY apply domain-specific fallback after resolution
    (e.g. `or tool_id` for tool names), but MUST NOT implement
    another LocalizedText resolution chain — Jinja templates use the
    `lt` filter (app/main.py), JS uses window.resolveLabel (static/app.js);
    both are semantically identical twins of this function and are kept
    in sync by the protocol regression test.

    Search indexing is exempt: it intentionally indexes all locale values.
    """
    if isinstance(label, str):
        return label
    if isinstance(label, dict):
        if label.get(lang):
            return str(label[lang])
        if label.get("en"):
            return str(label["en"])
        for v in label.values():
            if isinstance(v, str) and v:
                return v
    return ""


def to_input_schema_properties(decl: list, lang: str = "en") -> tuple[dict, list]:
    """decl → MCP inputSchema (properties, required). Agent 面恒稳定单串
    (dict["en"] 或原文), 不做语言协商。空声明返回 ({}, [])。"""
    props: dict = {}
    required: list = []
    for p in decl or []:
        t = p.get("type")
        if t == "enum":
            schema = {"type": "string",
                      "enum": [o["value"] for o in p.get("options", [])]}
        elif t == "number":
            schema = {"type": "number"}
            if p.get("min") is not None:
                schema["minimum"] = p["min"]
            if p.get("max") is not None:
                schema["maximum"] = p["max"]
        elif t == "boolean":
            schema = {"type": "boolean"}
        else:
            schema = {"type": "string"}
        desc = resolve_label(p.get("label"), lang) or p["key"]
        default = p.get("default")
        if default is not None:
            schema["default"] = default
            desc = f"{desc} (default: {default})"
        schema["description"] = desc
        props[p["key"]] = schema
        if p.get("required"):
            required.append(p["key"])
    return props, required


def patch_locale(value, lang: str, new_text: str):
    """LocalizedText 编辑保护 (patch semantics, the billing fix).

    Web 表单只编辑一个 locale; 既有翻译必须原样保留:
      旧值 str      → {lang: new_text}        (不保留旧 str — 单串无其它语言可保)
      旧值 dict     → 旧 dict 浅拷贝后覆写 lang 键
      new_text 空   → 删除 lang 键 (显式删除语义); dict 删空后返回 ""
    Fallback 物化防护由调用方处理 (merge_params_decl 的 no-change 检测)。
    """
    new_text = (new_text or "").strip()
    if isinstance(value, dict):
        out = {k: v for k, v in value.items() if isinstance(v, str)}
        if new_text:
            out[lang] = new_text
        else:
            out.pop(lang, None)
        return out if out else ""
    # str 旧值
    if new_text:
        return {lang: new_text}
    return ""


def _leaf_merge(old_value, new_value, lang: str):
    """params 叶子 (label/placeholder/options[].label) 的 locale merge:
    no-change 判定基于 old_value (该参数的原始声明, 不可变), 真写走 patch_locale。
    new 为 str = 该 locale 单值提交; 为 dict = 逐 locale patch (web 主语言
    {主语言: 文本} 形态), 未提及语言一律保留。
    旧值 str → 基底 {lang: 旧 str} (locale 机制: dict 第一键 = 原文语言)。
    返回合并后的叶子值 (str | dict)。"""
    if isinstance(new_value, str):
        return patch_locale_guard(old_value, old_value, lang, new_value)
    if isinstance(new_value, dict):
        base = old_value if isinstance(old_value, dict) else (
            {lang: old_value} if isinstance(old_value, str) and old_value else {})
        merged = dict(base)
        for lg, txt in new_value.items():
            merged = patch_locale_guard(old_value, merged, lg, str(txt or ""))
        return merged
    return new_value


def merge_params_decl(old_decl: list, new_decl: list, lang: str) -> list:
    """params 结构全量替换 + LocalizedText 叶子 locale merge (the billing fix).

    结构 (参数/选项的增删改) 以 new_decl 为准 — 删除 parameter/option
    表示结构删除, 其旧翻译随结构一起删除。
    三个 LocalizedText 叶子 (param.label / param.placeholder /
    options[].label) 按身份对齐合并: parameter 按 key, option 按 value,
    禁止按数组 index。str 与 dict ({主语言: 文本}) 提交形态均为 locale
    patch — 未提及语言保留。旧值在该 locale 下缺失时, new_decl 的 str 直接落
    {lang: str} (首次翻译); 若 str 等于旧值的 resolver fallback (发布者
    没改 fallback 文本), 视为未修改, 不物化新 locale 键。
    """
    old_by_key = {p["key"]: p for p in (old_decl or []) if isinstance(p, dict) and p.get("key")}
    merged = []
    for p in new_decl or []:
        key = p.get("key")
        old = old_by_key.get(key)
        if not old:
            merged.append(p)
            continue
        # param.label (str 与 dict 提交形态都走 locale merge — dict 直替会丢翻译)
        if "label" in p:
            old_label = old.get("label")
            if isinstance(p["label"], (str, dict)):
                p["label"] = _leaf_merge(old_label, p["label"], lang)
        # param.placeholder
        if p.get("placeholder") is not None and isinstance(p["placeholder"], (str, dict)):
            old_ph = old.get("placeholder")
            if old_ph is not None:
                p["placeholder"] = _leaf_merge(old_ph, p["placeholder"], lang)
        # options[].label (按 value 对齐)
        old_opts = {o.get("value"): o for o in (old.get("options") or [])
                    if isinstance(o, dict) and o.get("value") is not None}
        if p.get("options"):
            new_opts = []
            for o in p["options"]:
                old_o = old_opts.get(o.get("value"))
                if old_o and isinstance(o.get("label"), (str, dict)):
                    o["label"] = _leaf_merge(old_o.get("label"), o["label"], lang)
                new_opts.append(o)
            p["options"] = new_opts
        merged.append(p)
    return merged




def patch_locale_guard(old_snapshot, merged, lang: str, new_text: str):
    """patch_locale + fallback 物化防护 (the billing fixc: 顺序无关)。

    no-change 判定永远基于 old_snapshot (merge 前的原始旧 dict) 在该
    locale 的 resolver 结果, 与 merged 的累积状态无关 — 前一个 locale
    的修改不得影响后一个 locale 是否被判为 fallback/no-op。
      new_text == resolve_label(old_snapshot, lang) → 未修改, 返回 merged 原样
      否则 → 对 merged 执行该 locale 的 patch/delete
    """
    cur = resolve_label(old_snapshot, lang) if old_snapshot else ""
    if cur and (new_text or "").strip() == cur:
        return merged
    return patch_locale(merged, lang, new_text)


class ParamsValueError(ValueError):
    """运行时取值违规 (→ 422)。与声明校验的 ParamsDeclError 分离。
    str(exc) = 错误码; field = 违规参数的 key (denial 的结构化字段)。"""

    def __init__(self, code: str, field: str):
        super().__init__(code)
        self.field = field


def enforce_params_decl(decl: list, values: dict) -> dict:
    """执行入口取值校验 (spec v3 M4): required/enum/number 越界 → ParamsValueError;
    boolean 强转; 声明外的键剥离 (不透传 handler)。返回清洗后的 dict。"""
    if values is None:
        values = {}
    out: dict = {}
    for p in decl or []:
        key = p["key"]
        if key not in values or values[key] is None or values[key] == "":
            if p.get("required"):
                if p.get("default") is not None:
                    out[key] = p["default"]
                else:
                    raise ParamsValueError("param_required", key)
            elif p.get("default") is not None:
                out[key] = p["default"]
            continue
        v = values[key]
        t = p.get("type")
        if t == "enum":
            sv = str(v)
            if sv not in [o["value"] for o in p.get("options", [])]:
                raise ParamsValueError("param_invalid", key)
            out[key] = sv
        elif t == "number":
            try:
                nv = float(v)
            except (TypeError, ValueError):
                raise ParamsValueError("param_invalid", key)
            if not math.isfinite(nv):
                raise ParamsValueError("param_invalid", key)
            if p.get("min") is not None and nv < p["min"]:
                raise ParamsValueError("param_out_of_range", key)
            if p.get("max") is not None and nv > p["max"]:
                raise ParamsValueError("param_out_of_range", key)
            out[key] = nv
        elif t == "boolean":
            out[key] = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
        else:  # text
            tv = str(v)
            if len(tv) > TEXT_PARAM_MAX_CHARS:
                raise ParamsValueError("param_too_long", key)
            out[key] = tv
    return out
