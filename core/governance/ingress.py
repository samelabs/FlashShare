"""governance/ingress — 平台 source-ingress 硬边界单点.

窄职责: files/request、单文件 bytes、batch bytes 三个平台硬边界的
authoritative 校验。不认识 User/Membership/Plan/Credits/rolling quota/
Gokapi — 那些属于 entitlements 与 pipeline 的各自职责。

边界值来自 config (FLASHSHARE_MAX_UPLOAD_MB / FLASHSHARE_MAX_BATCH_MB,
默认 50/100MB) — 只此一份事实源。Web / MCP / pipeline 全部
消费本模块, 禁止任何面自己维护第二份 20/50/100 判定。

错误为结构化 denial dict (轻量, 非错误框架):
  {"error": "err_too_many_files",  "limit": 20,         "incoming": 21}
  {"error": "err_file_too_large",   "limit": 52428800,  "incoming": ...}
  {"error": "err_batch_too_large",  "limit": 104857600, "incoming": ...}
batch 超限绝不冒充 err_file_too_large。合法 → None。
"""

from config import MAX_UPLOAD_BYTES, MAX_BATCH_BYTES

MAX_FILES_PER_REQUEST = 20


def validate_batch(sizes: list[int]) -> dict | None:
    """对一批 source 文件大小做平台硬边界校验。

    sizes: 每个 source 文件的 byte 数。任一边界越界返回结构化
    denial; 全部合法返回 None。检查顺序: files 数 → 单文件 → batch 总量。
    """
    if len(sizes) > MAX_FILES_PER_REQUEST:
        return {"error": "err_too_many_files",
                "limit": MAX_FILES_PER_REQUEST, "incoming": len(sizes)}
    for s in sizes:
        if s > MAX_UPLOAD_BYTES:
            return {"error": "err_file_too_large",
                    "limit": MAX_UPLOAD_BYTES, "incoming": s}
    total = sum(sizes)
    if total > MAX_BATCH_BYTES:
        return {"error": "err_batch_too_large",
                "limit": MAX_BATCH_BYTES, "incoming": total}
    return None
