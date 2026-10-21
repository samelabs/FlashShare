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


# 远程执行背压: http 工具的进程内在途计数上限 (全局 + 每用户)。全局上限
# 必须小于全局重路径闸门并发 (app/main.py Semaphore(8)) — 慢的第三方端点
# 永远占不满全局闸门, 其余表面始终留有名额; 每用户上限防止单账号独占。
REMOTE_MAX_INFLIGHT = 4
REMOTE_PER_USER_MAX = 2

_remote_lock = threading.Lock()
_remote_inflight = 0
_remote_per_user: dict = {}


def remote_try_acquire(user_id) -> str | None:
    """占一个远程执行在途名额 (全局 + 每用户); 成功返回 None, 满额返回
    "user" 或 "global" (调用方在 claim 之前拒绝, 不排队)。"""
    global _remote_inflight
    uid = int(user_id)
    with _remote_lock:
        if _remote_per_user.get(uid, 0) >= REMOTE_PER_USER_MAX:
            return "user"
        if _remote_inflight >= REMOTE_MAX_INFLIGHT:
            return "global"
        _remote_inflight += 1
        _remote_per_user[uid] = _remote_per_user.get(uid, 0) + 1
        return None


def remote_release(user_id) -> None:
    """释放一个远程在途名额 (与 remote_try_acquire 成对; 计数下限 0,
    每用户计数归零即删除键, 防御性钳位 + 防字典膨胀)。"""
    global _remote_inflight
    uid = int(user_id)
    with _remote_lock:
        if _remote_inflight > 0:
            _remote_inflight -= 1
        held = _remote_per_user.get(uid, 0)
        if held > 1:
            _remote_per_user[uid] = held - 1
        else:
            _remote_per_user.pop(uid, None)


def remote_inflight_count() -> int:
    """当前远程在途数 (观测/日志用)。"""
    with _remote_lock:
        return _remote_inflight


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=WORKERS)


def get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None or _pool._broken:
            _pool = _new_pool()
        return _pool


def reset_pool(expected_pool, reason: str = "unspecified",
               terminate_workers: bool = False) -> ProcessPoolExecutor:
    """CAS-style 重建: 以调用者观察到的 broken pool identity 为准。
    同一个 broken pool 被 N 个 caller 同时观察到时, 只有第一个完成
    replacement (reset_count +1); 后续 caller 发现 _pool 已不是其
    observed identity → 直接复用既有 replacement。旧池 shutdown 释放。
    terminate_workers=True (超时回收): shutdown 之后对旧池仍存活的
    worker 进程 terminate → join(2) → 仍存活 kill + join(1) —
    shutdown(wait=False) 不杀在跑进程, 卡死 worker 必须显式回收。
    不创建 generation table / worker state machine。"""
    global _pool, _reset_count
    procs = []
    with _pool_lock:
        if _pool is not expected_pool:
            return _pool
        old = _pool
        # snapshot BEFORE shutdown: shutdown clears _processes
        procs = list((getattr(old, "_processes", None) or {}).values())
        replacement = _new_pool()
        _pool = replacement
        _reset_count += 1
    if old is not None:
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.warning("old pool shutdown error during reset")
    if terminate_workers:
        for proc in procs:
            try:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=2)
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=1)
            except Exception:
                logger.warning("worker termination error during pool reset")
    logger.warning("execution pool reset #%d (%s)", _reset_count, reason)
    return replacement


def default_process_tool():
    """worker 入口 (域内 worker 模块)。"""
    from .worker import process_tool
    return process_tool
