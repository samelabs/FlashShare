# FlashShare 生产化方案

## 目标

当前功能完备，需要面向上线的性能保障。
1GB VPS，不引入新框架，最小改动解决三个致命问题。

---

## 问题诊断

| 问题 | 现状 | 风险 |
|------|------|------|
| 无限线程 | ThreadingHTTPServer 每请求一线程，无上限 | 慢客户端耗尽线程 |
| 无内存隔离 | 工具处理在请求线程内，3×85MB=255MB ≈ MemoryMax | OOM 全站宕机 |
| 无背压 | 超载时不排队不拒绝，直接堆积 | 雪崩 |

---

## 方案：有界线程池 + 工具子进程隔离

### 原理

```
Nginx → ThreadingHTTPServer（保持，仅改线程模型）
              │
              ├── 普通 API（认证/列表/静态）→ 线程池内快速处理
              │
              └── 工具处理（Pillow CPU 密集）→ 提交到子进程池
                         │
                         ├── 进程池 worker 1（85MB 上限）
                         └── 进程池 worker 2（85MB 上限）
                         （第 3 个排队，不堆积）
```

### 具体改动

#### 1. 线程池限制（main.py）

ThreadingHTTPServer 本身不限制线程数。在 `App` 类加一个信号量，限制并发处理数：

```python
import threading

# 最多 8 个并发请求在处理（其余在 Nginx 排队）
_concurrency = threading.Semaphore(8)

class App(BaseHTTPRequestHandler):
    def handle_one_request(self):
        with _concurrency:
            super().handle_one_request()
```

Nginx 侧配合 `proxy_connect_timeout 5s`，超时返回 502 而不是无限等待。

**效果**：最多 8 个并发线程，其余在 Nginx 排队。内存上限可预测。

#### 2. 工具处理子进程隔离（main.py + 新文件 worker.py）

工具 handler 的 Pillow 处理放入子进程。主进程只做 IO（读源文件、写结果）。

```python
# main.py 启动时
from multiprocessing import Pool
import pickle, io

# 2 个 worker，每 10 次任务回收（释放内存碎片）
TOOL_POOL = Pool(processes=2, maxtasksperchild=10)

# _execute_and_share 改为
def _execute_and_share(self, tool, upload, params, user):
    # 1. 读源文件到 bytes（主进程 IO）
    status, headers, stream = storage.read(upload_dict["gokapi_id"])
    source_bytes = stream.read()

    # 2. 提交到子进程处理（阻塞等待，但主进程线程不被 Pillow 占内存）
    from worker import process_in_subprocess
    result_bytes, content_type, filename = TOOL_POOL.apply(
        process_in_subprocess,
        args=(tool["id"], source_bytes, upload_dict, params)
    )

    # 3. 写结果到存储（主进程 IO）
    out_stream = io.BytesIO(result_bytes)
    gokapi_result = storage.write(out_stream, filename, content_type, ...)
    # 4. 写 DB
```

```python
# worker.py（新文件，子进程入口）
def process_in_subprocess(tool_id, source_bytes, upload_dict, params):
    """在子进程内执行 handler。"""
    import io
    from tools import get_tool

    tool = get_tool(tool_id)
    handler = tool["handler"]

    tool_input = {
        "stream": io.BytesIO(source_bytes),
        "filename": upload_dict["file_name"],
        "content_type": upload_dict["content_type"],
        "size": len(source_bytes),
    }
    tool_output = handler(tool_input, params)
    output_bytes = tool_output["stream"].read()

    return output_bytes, tool_output["content_type"], tool_output["filename"]
```

**效果**：
- Pillow 的 85MB 内存峰值在子进程内，OOM 只杀子进程
- `maxtasksperchild=10`：自动回收内存碎片
- `Pool(2)`：最多 2 个并发工具，第 3 个排队
- 主进程线程只做 IO（读 bytes + 写 bytes），内存开销极小

#### 3. N+1 已修复（已完成）

`list_uploads` 用批量 gokapi API，1 次 HTTP 替代 20 次。

---

## 内存预算

| 组件 | 内存 |
|------|------|
| Nginx | ~10MB |
| gokapi | ~20MB |
| OS + 其他 | ~100MB |
| Python 主进程（8 线程，纯 IO） | ~50MB |
| 工具子进程 1 | ~85MB |
| 工具子进程 2 | ~85MB |
| **总计** | **~350MB** |

1GB VPS 有 ~600MB 可用，350MB 预算安全。
systemd MemoryMax 从 256M 提到 512M（主进程 + 子进程共享上限）。

---

## 改动清单

| 文件 | 改动 | 工作量 |
|------|------|--------|
| `main.py` | 加信号量 + _execute_and_share 改为子进程调用 | 中 |
| `worker.py` | 新文件，子进程入口函数 | 小 |
| `/etc/systemd/system/flashshare.service` | MemoryMax 提到 512M | 1 行 |
| Nginx | `proxy_connect_timeout 5s` | 1 行 |

**不改**：路由逻辑、认证、上传、gokapi.py、tools.py、handler 代码、前端。

---

## 验收标准

| 指标 | 目标 |
|------|------|
| 3 并发工具处理 | 不 OOM，主服务不挂 |
| 列表加载 | < 100ms |
| 普通上传/下载 | 不受工具处理影响 |
| 单工具处理 | 仍 < 5s |
| 内存峰值 | < 400MB |
