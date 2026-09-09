"""execution/invocation — canonical ToolInvocation domain (Stage C).

唯一 invocation schema:

    {
      "tool_id": "compress",
      "source": {"files": [{"public_id": "...", "source_token": "..."}]},
      "params": {"preset": "2mb"},
      "delivery": {"expiry_days": 7, "allowed_downloads": 200},
      "client_ref": null
    }

硬不变量:
- 只有一种 schema; 单文件/多文件不改变 shape (只是 source.files 长度)。
- params 只含 tool params; delivery 属平台产物交付, handler/remote
  endpoint 永远看不到。
- client_ref: null = fresh invocation; 非空 string = user-scoped
  idempotency/replay key (gate 语义不变)。
- 旧协议 (flat source 字段 / delivery 混入 params) 出现即
  invalid_request — 无 compatibility adapter, 无双读。
"""

from governance.delivery import validate_delivery

_ALLOWED_KEYS = {"tool_id", "source", "params", "delivery", "client_ref"}
_MAX_FILES = 20
_MAX_CLIENT_REF = 128


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
    __slots__ = ("tool_id", "files", "params", "delivery", "client_ref")

    def __init__(self, tool_id, files, params, delivery, client_ref):
        self.tool_id = tool_id
        self.files = files          # list[SourceFile], 1..20
        self.params = params        # dict (只含 tool params)
        self.delivery = delivery    # canonical {"expiry_days","allowed_downloads"}
        self.client_ref = client_ref  # str | None

    def to_json(self) -> dict:
        """canonical JSON shape (外部/内部一致)。"""
        return {
            "tool_id": self.tool_id,
            "source": {"files": [
                {"public_id": f.public_id, "source_token": f.source_token}
                for f in self.files]},
            "params": dict(self.params),
            "delivery": dict(self.delivery),
            "client_ref": self.client_ref,
        }


def parse_tool_invocation(raw) -> ToolInvocation:
    """严格 parser/validator。失败抛 InvocationError("invalid_request")。

    dispatch 在此单点解析; 不接受旧 request shape, 未知 top-level
    字段不 fail-open。
    """
    if not isinstance(raw, dict):
        raise _bad()
    unknown = set(raw.keys()) - _ALLOWED_KEYS
    if unknown:
        raise _bad()

    tool_id = raw.get("tool_id")
    if not isinstance(tool_id, str) or not tool_id.strip():
        raise _bad()
    # tool_id 身份 grammar / 存在性 / visibility 由 resolve() 单点处理,
    # parser 不复制。

    source = raw.get("source")
    if not isinstance(source, dict) or set(source.keys()) - {"files"}:
        raise _bad()
    files = source.get("files")
    if not isinstance(files, list) or not (1 <= len(files) <= _MAX_FILES):
        raise _bad()
    parsed_files = []
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

    params = raw.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _bad()
    # 旧协议: delivery 曾随 params 传入 — 显式拒绝, 不剥离不忽略。
    if "expiry_days" in params or "allowed_downloads" in params:
        raise _bad()

    delivery_raw = raw.get("delivery")
    if delivery_raw is None:
        delivery_raw = {}
    try:
        delivery = validate_delivery(delivery_raw) if delivery_raw else None
    except ValueError:
        raise _bad() from None
    if delivery is None:
        raise _bad()

    client_ref = raw.get("client_ref")
    if client_ref is not None:
        if not isinstance(client_ref, str):
            raise _bad()
        client_ref = client_ref.strip()
        if not client_ref or len(client_ref) > _MAX_CLIENT_REF:
            raise _bad()

    return ToolInvocation(tool_id, parsed_files, params, delivery, client_ref)
