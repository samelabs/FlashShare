"""Anti-abuse: client_ref throttle (gate 前置).

防作弊基线: 消费/发放的账目实时性由 billing 单点保证
(行锁 + 幂等键 + balance_after 逐行快照); 此处补调用面滥用防线 —

client_ref 洪泛: 同一 user 对未知/重试 ref 的高频提交会打满
tool_calls 表 (replay 锚点查询也随之变慢)。滑动窗口计数, 超限 429。
窗口内存态 (进程级), 重启归零 — 防线是削峰不是记账, 记账永远落库。
"""

import threading
import time
from collections import defaultdict, deque

from utils import now

# 每 user 每 60s 最多 N 次闸门提交 (含重试); 正常工具调用远低于此。
GATE_REF_LIMIT = 30
GATE_REF_WINDOW = 60

_lock = threading.Lock()
_hits: dict[int, deque] = defaultdict(deque)


def gate_ref_throttled(user_id: int) -> bool:
    """True = 本次提交超限 (调用方返回 429)。滑动窗口, O(1) 均摊。"""
    ts = now()
    cutoff = ts - GATE_REF_WINDOW
    with _lock:
        q = _hits[user_id]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= GATE_REF_LIMIT:
            return True
        q.append(ts)
        return False


# MCP tools/call 入口统一限流: 覆盖 meta 工具与 URL 源拉取等一切分支
# (任何单分支自身的防护都不替代入口计数)。同一 user 每 60s 最多 60 次。
MCP_CALL_LIMIT = 60
MCP_CALL_WINDOW = 60

_call_lock = threading.Lock()
_call_hits: dict[int, deque] = defaultdict(deque)


def mcp_call_throttled(user_id: int) -> bool:
    """True = 本次 tools/call 超限 (调用方返回 isError err_rate_limited)。
    滑动窗口, O(1) 均摊; 与 gate_ref_throttled 同一写法, 独立计数。"""
    ts = now()
    cutoff = ts - MCP_CALL_WINDOW
    with _call_lock:
        q = _call_hits[user_id]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= MCP_CALL_LIMIT:
            return True
        q.append(ts)
        return False
