"""Params declaration validation (tool modes spec v3, 2026-09-02).

One validator for the params_json declaration — the ONLY place that knows
the contract. 提交时全量校验, 存库即合法, 读取面永不二次校验 (单点原则)。
来源无关: 协议只看数据形态 (label: str | dict), 不看发布者/执行类。
"""
import re

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TYPES = ("enum", "text", "number", "boolean")
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
            if (item.get("min") is not None and item.get("max") is not None
                    and item["min"] > item["max"]):
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
    """LocalizedText 编辑保护 (patch semantics, 批 2).

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


def merge_params_decl(old_decl: list, new_decl: list, lang: str) -> list:
    """params 结构全量替换 + LocalizedText 叶子 locale merge (批 2).

    结构 (参数/选项的增删改) 以 new_decl 为准 — 删除 parameter/option
    表示结构删除, 其旧翻译随结构一起删除。
    三个 LocalizedText 叶子 (param.label / param.placeholder /
    options[].label) 按身份对齐合并: parameter 按 key, option 按 value,
    禁止按数组 index。旧值在该 locale 下缺失时, new_decl 的 str 直接落
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
        # param.label
        if "label" in p:
            old_label = old.get("label")
            if isinstance(p["label"], str):
                cur = resolve_label(old_label, lang)
                if cur and p["label"] == cur and isinstance(old_label, dict):
                    p["label"] = old_label            # 未修改, 不物化
                elif cur and p["label"] == cur:
                    p["label"] = old_label            # str 旧值: 原样保留
                else:
                    p["label"] = patch_locale(old_label, lang, p["label"])
        # param.placeholder
        if p.get("placeholder") is not None and isinstance(p["placeholder"], str):
            old_ph = old.get("placeholder")
            if old_ph is not None:
                cur = resolve_label(old_ph, lang)
                if cur and p["placeholder"] == cur:
                    p["placeholder"] = old_ph
                else:
                    p["placeholder"] = patch_locale(old_ph, lang, p["placeholder"])
        # options[].label (按 value 对齐)
        old_opts = {o.get("value"): o for o in (old.get("options") or [])
                    if isinstance(o, dict) and o.get("value") is not None}
        if p.get("options"):
            new_opts = []
            for o in p["options"]:
                old_o = old_opts.get(o.get("value"))
                if old_o and isinstance(o.get("label"), str):
                    old_label = old_o.get("label")
                    cur = resolve_label(old_label, lang)
                    if cur and o["label"] == cur:
                        o["label"] = old_label
                    else:
                        o["label"] = patch_locale(old_label, lang, o["label"])
                new_opts.append(o)
            p["options"] = new_opts
        merged.append(p)
    return merged




def patch_locale_guard(value, lang: str, new_text: str):
    """patch_locale + fallback 物化防护: new_text 等于旧值在该 locale 的
    resolver 结果时视为未修改, 原样返回 (不创建/覆写键)。"""
    cur = resolve_label(value, lang) if value else ""
    if cur and (new_text or "").strip() == cur:
        return value if isinstance(value, dict) else value
    return patch_locale(value, lang, new_text)


class ParamsValueError(ValueError):
    """运行时取值违规 (→ 422)。与声明校验的 ParamsDeclError 分离。"""


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
                    raise ParamsValueError("param_required")
            elif p.get("default") is not None:
                out[key] = p["default"]
            continue
        v = values[key]
        t = p.get("type")
        if t == "enum":
            sv = str(v)
            if sv not in [o["value"] for o in p.get("options", [])]:
                raise ParamsValueError("param_invalid")
            out[key] = sv
        elif t == "number":
            try:
                nv = float(v)
            except (TypeError, ValueError):
                raise ParamsValueError("param_invalid")
            if p.get("min") is not None and nv < p["min"]:
                raise ParamsValueError("param_out_of_range")
            if p.get("max") is not None and nv > p["max"]:
                raise ParamsValueError("param_out_of_range")
            out[key] = nv
        elif t == "boolean":
            out[key] = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
        else:  # text
            out[key] = str(v)
    return out
