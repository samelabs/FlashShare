"""execution/pool — worker 进程池 (执行域自持资源, 批治理1).

原先 ProcessPoolExecutor 在 app/routes/api.py 模块级创建, core 反向借 app —
分层违例。池归执行域; app 层如需引用(如健康检查)从这里拿。

correctness 语义 (非 capacity tuning): BrokenProcessPool / worker 死亡后
reset_pool 在锁内重建 — 并发 reset 只采纳一个 replacement, 旧池 shutdown
释放; get_pool 惰性兜底已 shutdown 的池。失败 invocation 不自动重跑。
"""

import logging
import threading
from concurrent.futures import ProcessPoolExecutor

logger = logging.getLogger("flashshare")

_pool_lock = threading.Lock()

# Eager init at import (原语义: 避免首请求延迟)
_pool = ProcessPoolExecutor(max_workers=2)
_reset_count = 0


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=2)


def get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None or _pool._broken:
            _pool = _new_pool()
        return _pool


def reset_pool(reason: str = "unspecified") -> ProcessPoolExecutor:
    """Shutdown 旧池 + 锁内新建。并发调用全部完成且池可用; 旧池
    shutdown(wait=False) 不阻塞失败路径。返回新池。"""
    global _pool, _reset_count
    with _pool_lock:
        old = _pool
        _pool = _new_pool()
        _reset_count += 1
    if old is not None:
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.warning("old pool shutdown error during reset")
    logger.warning("execution pool reset #%d (%s)", _reset_count, reason)
    return _pool


def pool_reset_count() -> int:
    return _reset_count


def default_process_tool():
    """worker 入口 (域内 worker 模块)。"""
    from .worker import process_tool
    return process_tool
