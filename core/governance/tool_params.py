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
    """The single label resolver (str | dict → display string).
    str → 原样; dict[lang] → dict["en"] → 首值。三面共用, 勿在他处重写。"""
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
