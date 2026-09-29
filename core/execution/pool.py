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

# worker 数是 capacity 事实, 初始池与每个 replacement 池一致。
WORKERS = 2

# Eager init at import (原语义: 避免首请求延迟)
_pool = ProcessPoolExecutor(max_workers=WORKERS)
_reset_count = 0

# 本地执行背压: 进程内在途计数上限。worker 池只限制并行执行数,
# 排队中的 invocation 仍会占住 claim/扣费后的等待 — 在途上限
# (4×workers) 让超载在 claim 之前就被拒 (err_busy), 不扣费。
LOCAL_MAX_INFLIGHT = 4 * WORKERS

_inflight_lock = threading.Lock()
_inflight = 0


def try_acquire() -> bool:
    """占一个本地执行在途名额; 满额返回 False (调用方拒绝, 不排队)。"""
    global _inflight
    with _inflight_lock:
        if _inflight >= LOCAL_MAX_INFLIGHT:
            return False
        _inflight += 1
        return True


def release() -> None:
    """释放一个在途名额 (与 try_acquire 成对; 下限 0 防御性钳位)。"""
    global _inflight
    with _inflight_lock:
        if _inflight > 0:
            _inflight -= 1


def inflight_count() -> int:
    """当前在途数 (观测/日志用)。"""
    with _inflight_lock:
        return _inflight


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=WORKERS)


def get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None or _pool._broken:
            _pool = _new_pool()
        return _pool


def reset_pool(expected_pool, reason: str = "unspecified") -> ProcessPoolExecutor:
    """CAS-style 重建: 以调用者观察到的 broken pool identity 为准。
    同一个 broken pool 被 N 个 caller 同时观察到时, 只有第一个完成
    replacement (reset_count +1); 后续 caller 发现 _pool 已不是其
    observed identity → 直接复用既有 replacement。旧池 shutdown 释放。
    不创建 generation table / worker state machine。"""
    global _pool, _reset_count
    with _pool_lock:
        if _pool is not expected_pool:
            return _pool
        old = _pool
        replacement = _new_pool()
        _pool = replacement
        _reset_count += 1
    if old is not None:
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.warning("old pool shutdown error during reset")
    logger.warning("execution pool reset #%d (%s)", _reset_count, reason)
    return replacement


def default_process_tool():
    """worker 入口 (域内 worker 模块)。"""
    from .worker import process_tool
    return process_tool
