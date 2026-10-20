"""Body input contract (正文输入口径) — single point.

两个正交输入方式之一 (IO-1): 文件工具 (files_max>0) 的输入走
source.files[]; 正文工具 (files_min=files_max=0) 的输入走 source.body。
本模块只管**取值口径**; 声明互斥 (body_format ↔ accept/tag) 在
tool_supply, 调用形态在 execution.invocation。

取值规则:
  text: 非空 UTF-8 字符串, <= BODY_MAX_CHARS 字符。
  json: 任意合法 JSON 值 (含 null/字符串/数字/布尔/数组/对象),
        禁止 NaN/Infinity; 规范序列化 (键排序、紧凑分隔、
        ensure_ascii=False) 后 <= BODY_MAX_CHARS 字符。
按 JSON 语义透传, 不保证原始字节不变; 指纹与南向传输都使用
canonical_json 的结果。
"""

import json
import math

BODY_FORMATS = ("text", "json")
BODY_MAX_CHARS = 100_000
# body_placeholder 与 params 的文本叶子同限 (LocalizedText, 每语言)
PLACEHOLDER_MAX_CHARS = 64


class BodyValueError(ValueError):
    """正文取值违规。str(exc) = 错误码 (err_body_required |
    err_body_too_long | err_body_invalid)。"""


def _reject_nonfinite(value) -> None:
    """json.loads 默认接受 NaN/Infinity 字面量 — 正文值里它们不是
    数字, 规范序列化前递归拒绝。"""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BodyValueError("err_body_invalid")
        return
    if isinstance(value, int):
        return
    if isinstance(value, str):
        return
    if isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)
        return
    if isinstance(value, dict):
        for k in value:
            _reject_nonfinite(k)
            _reject_nonfinite(value[k])
        return
    raise BodyValueError("err_body_invalid")


def canonical_json(value) -> str:
    """规范序列化 (键排序、紧凑分隔、ensure_ascii=False)。
    非有限数 / 非 JSON 值 → err_body_invalid。"""
    _reject_nonfinite(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BodyValueError("err_body_invalid") from exc


def validate_body_value(body_format: str, value) -> str:
    """校验一个正文取值, 返回其规范串 (指纹/南向传输的同一事实)。

    text: 返回原字符串; json: 返回 canonical_json 结果。"""
    if body_format == "text":
        if not isinstance(value, str):
            # 类型错位是非法取值, 不是缺失 — 两个码语义分开
            raise BodyValueError("err_body_invalid")
        if not value:
            raise BodyValueError("err_body_required")
        if len(value) > BODY_MAX_CHARS:
            raise BodyValueError("err_body_too_long")
        return value
    if body_format == "json":
        blob = canonical_json(value)
        if len(blob) > BODY_MAX_CHARS:
            raise BodyValueError("err_body_too_long")
        return blob
    raise BodyValueError("err_body_invalid")
