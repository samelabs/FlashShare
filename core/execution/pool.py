"""execution/pool — worker 进程池 (执行域自持资源, 批治理1).

原先 ProcessPoolExecutor 在 app/routes/api.py 模块级创建, core 反向借 app —
分层违例。池归执行域; app 层如需引用(如健康检查)从这里拿。
"""

from concurrent.futures import ProcessPoolExecutor

# Eager init at import (原语义: 避免首请求延迟)
_pool = ProcessPoolExecutor(max_workers=2)


def get_pool() -> ProcessPoolExecutor:
    return _pool


def default_process_tool():
    """worker 入口 (域内 worker 模块)。"""
    from .worker import process_tool
    return process_tool
