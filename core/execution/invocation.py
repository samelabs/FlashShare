"""execution/invocation — canonical ToolInvocation domain (the canonical-invocation unification).

唯一 invocation schema:

    {
      "tool_id": "compress",
      "source": {"files": [{"public_id": "...", "source_token": "..."}]}
             | {"body": <取值>},
      "params": {"preset": "2mb"},
      "delivery": {"expiry_days": 7, "allowed_downloads": 200},
      "client_ref": "web:9f2c..."
    }

硬不变量:
- 只有一种 schema; 单文件/多文件不改变 shape (只是 source.files 长度)。
- source 是两个正交输入方式之一, 键集精确二选一: {files:[...]} (文件
  工具, 0..N) 或 {body: <取值>} (正文工具, 取值口径见 governance.body,
  合法 null 也算正文 — 以键是否存在判定, 禁止按真假值判断)。
- params 只含 tool params; delivery 属平台产物交付, handler/remote
  endpoint 永远看不到。
- client_ref: 必填 non-empty string (<=128), client-generated stable
  operation identity。same user+tool+ref 永远代表同一个 canonical
  invocation fingerprint: same fingerprint → running=progress /
  ok=replay / failed=可重试; different fingerprint → 409
  idempotency_conflict (zero charge, zero execution)。
- 旧协议 (flat source 字段 / delivery 混入 params) 出现即
  invalid_request — 无 compatibility adapter, 无双读。
"""

from governance.delivery import validate_delivery

_ALLOWED_KEYS = {"tool_id", "source", "params", "delivery", "client_ref"}
from governance.ingress import MAX_FILES_PER_REQUEST as _MAX_FILES
_MAX_CLIENT_REF = 128

# 正文缺席哨兵: body 合法取值包含 null/0/"" — 缺席与否只能用哨兵判定
BODY_ABSENT = object()


class InvocationError(ValueError):
    """canonical ToolInvocation 解析失败。str(exc) = invalid_request。"""


def _bad():
    return InvocationError("invalid_request")


class SourceFile:
    __slots__ = ("public_id", "source_token")

    def __init__(self, public_id: str, source_token: str):
        self.public_id = public_id
        self.source_token = source_token


class ToolInvocation:
    __slots__ = ("tool_id", "files", "body", "params", "delivery",
                 "client_ref", "body_format")

    def __init__(self, tool_id, files, params, delivery, client_ref,
                 body=BODY_ABSENT, body_format=None):
        self.tool_id = tool_id
        self.files = files          # list[SourceFile], 0..20 (文件输入)
        self.body = body            # 正文取值 (BODY_ABSENT = 文件输入)
        self.body_format = body_format  # 正文口径 ('text'|'json'), dispatch 注入
        self.params = params        # dict (只含 tool params)
        self.delivery = delivery    # canonical {"expiry_days","allowed_downloads"}
        self.client_ref = client_ref  # str (required, non-empty, <=128)

    def has_body(self) -> bool:
        return self.body is not BODY_ABSENT

    def to_json(self) -> dict:
        """canonical JSON shape (外部/内部一致)。"""
        source = ({"body": self.body} if self.has_body()
                  else {"files": [
                      {"public_id": f.public_id, "source_token": f.source_token}
                      for f in self.files]})
        return {
            "tool_id": self.tool_id,
            "source": source,
            "params": dict(self.params),
            "delivery": dict(self.delivery),
            "client_ref": self.client_ref,
        }


def parse_tool_invocation(raw) -> ToolInvocation:
    """严格 parser/validator。失败抛 InvocationError("invalid_request")。

    dispatch 在此单点解析; 不接受旧 request shape, 未知 top-level
    字段不 fail-open。取值级正文口径 (长度/JSON 语义) 在 dispatch
    preflight 解析出工具后校验 — parser 只认形态。
    """
    if not isinstance(raw, dict):
        raise _bad()
    # canonical root 必须精确五键 — 多余键与缺失键同样拒绝
    # (字段缺失不得解释为 null/默认)。
    if set(raw.keys()) != _ALLOWED_KEYS:
        raise _bad()

    tool_id = raw.get("tool_id")
    if not isinstance(tool_id, str) or not tool_id.strip():
        raise _bad()
    # tool_id 身份 grammar / 存在性 / visibility 由 resolve() 单点处理,
    # parser 不复制。

    source = raw.get("source")
    if not isinstance(source, dict):
        raise _bad()
    keys = set(source.keys())
    body = BODY_ABSENT
    parsed_files = []
    if keys == {"files"}:
        files = source["files"]
        if not isinstance(files, list) or not (0 <= len(files) <= _MAX_FILES):
            raise _bad()
        for item in files:
            if not isinstance(item, dict):
                raise _bad()
            if set(item.keys()) - {"public_id", "source_token"}:
                raise _bad()
            pid = item.get("public_id")
            token = item.get("source_token")
            if not isinstance(pid, str) or not pid:
                raise _bad()
            # source_token key 固定存在且为 str (允许空串 — DB-owned upload
            # 可仅靠 owner 校验); 缺 key 即旧协议混入, 拒绝。
            if not isinstance(token, str):
                raise _bad()
            parsed_files.append(SourceFile(pid, token))
    elif keys == {"body"}:
        # 取值任意 JSON (含 null) — 键存在即正文输入; 真假值不参与判定
        body = source["body"]
    else:
        # 空对象 / files+body 并存 / 未知键 → 旧协议或双输入, 拒绝
        raise _bad()

    params = raw.get("params")
    # canonical params 必须显式是 dict; null = 缺失语义, 拒绝
    # (无参数工具必须显式 "params": {})。
    if not isinstance(params, dict):
        raise _bad()
    # 旧协议: delivery 曾随 params 传入 — 显式拒绝, 不剥离不忽略。
    if "expiry_days" in params or "allowed_downloads" in params:
        raise _bad()

    # delivery: key 必须存在 (五键 root 已保证), 严格两键校验在
    # governance.delivery.validate_delivery。
    try:
        delivery = validate_delivery(raw.get("delivery"))
    except ValueError:
        raise _bad() from None

    client_ref = raw.get("client_ref")
    if not isinstance(client_ref, str):
        raise _bad()
    client_ref = client_ref.strip()
    if not client_ref or len(client_ref) > _MAX_CLIENT_REF:
        raise _bad()

    return ToolInvocation(tool_id, parsed_files, params, delivery,
                          client_ref, body=body)


def canonical_body_payload(invocation: ToolInvocation) -> str:
    """正文取值的规范串 (指纹身份)。text = 原字符串; json = 规范序列化。
    dispatch 在 preflight 后注入 body_format; 未注入 (纯 parser 单测)
    时按值形态兜底 — 同一工具口径恒定, 不影响线上判定。"""
    from governance.body import canonical_json, BodyValueError
    value = invocation.body
    if invocation.body_format == "text" or (
            invocation.body_format is None and isinstance(value, str)):
        return str(value)
    try:
        return canonical_json(value)
    except BodyValueError:
        # 未过 preflight 的非规范值 (仅可达于单测直调): 确定性回落
        return repr(value)


def invocation_fingerprint(invocation: ToolInvocation) -> str:
    """SHA-256 hex of the canonical semantic payload of one operation.

    Identity inputs (in this exact order-sensitive shape):
      tool_id, source identity (files: public_ids ORDER PRESERVED;
      body: 规范序列化串), canonicalized params (dispatch
      enforce_params_decl defaults already applied), delivery.

    Deliberately EXCLUDED: client_ref (the identity KEY, not content),
    source_token (authorization credential, not operation semantics —
    swapping an equivalent token keeps the same fingerprint), user_id,
    IP, pricing, commission. Same public_id under a different but
    equivalent token is the SAME logical operation.

    One helper only — Web/MCP all call this; nobody re-hashes
    the raw HTTP JSON (a defaulted-omitted param and an explicitly
    sent default must be the same operation).
    """
    import hashlib
    import json as _json
    payload = {
        "tool_id": invocation.tool_id,
        "params": dict(invocation.params),
        "delivery": dict(invocation.delivery),
    }
    if invocation.has_body():
        payload["source_body"] = canonical_body_payload(invocation)
    else:
        payload["source_public_ids"] = [f.public_id
                                        for f in invocation.files]
    blob = _json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
