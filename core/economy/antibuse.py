"""Anti-abuse: client_ref throttle (gate 前置).

防作弊基线 (1004): 消费/发放的账目实时性由 billing 单点保证
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


def _sweep_dead():
    """可选维护: 清空长期无命中的 user 桶 (内存回收)。无人调用也不出错。"""
    cutoff = time.time() - 24 * 3600
    with _lock:
        for uid in [u for u, q in _hits.items() if not q or q[-1] < cutoff]:
            _hits.pop(uid, None)
