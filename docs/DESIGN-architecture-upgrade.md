# FlashShare 架构升级方案

## 一、当前问题

Python stdlib `ThreadingHTTPServer` 是开发服务器，当前已暴露出结构性风险：

- 无线程池上限：每请求一线程，无背压
- 无内存隔离：3 个并发工具处理（3×85MB=255MB）即触达 MemoryMax=256M → OOM kill 全站宕机
- N+1 gokapi 调用：列表请求串行 20 次 HTTP，阻塞线程
- 无请求超时控制：慢客户端可占用线程 5 分钟（Nginx proxy_read_timeout=300s）

目标：1GB VPS 上稳定支撑 10-20 并发用户，工具处理不拖垮分享链。

---

## 二、方案对比

### 方案 A：Nginx 反代 + Gunicorn（推荐）

将 Python 进程管理和请求调度交给成熟的生产服务器。

```
用户 → Cloudflare → Nginx → Gunicorn (同步 worker) → Flask/Bottle App
```

| 维度 | 效果 |
|------|------|
| 线程池 | Gunicorn `--workers 2 --threads 2`：固定 4 个并发槽，超出排队 |
| 内存隔离 | 每个 worker 独立进程，单 worker OOM 不影响另一个 |
| 背压 | 队列满时返回 502，Nginx 层可缓存重试 |
| N+1 | Worker 内部并发仍需优化，但不会因 N+1 打崩 |

**改动量**：中。需要：
1. 引入轻量框架（Bottle ~4000 行，零依赖；或直接用 WSGI handler）
2. Gunicorn 配置（`pip install gunicorn`）
3. systemd service 改为 Gunicorn 启动
4. Nginx upstream 改为 Gunicorn socket

**风险**：需要适配现有 `BaseHTTPRequestHandler` 代码到 WSGI 接口。当前 `main.py` ~870 行，核心路由逻辑约 200 行，改动可控。

---

### 方案 B：保留 stdlib + 进程级隔离（最小改动）

不引入框架，用多进程 + 队列实现工具处理的内存隔离。

```
用户 → Nginx → ThreadingHTTPServer（仅路由）
                    ↓ 工具请求
              子进程池（2 个 worker，队列分发）
```

| 维度 | 效果 |
|------|------|
| 内存隔离 | 工具处理在子进程，OOM 只杀子进程，主服务不挂 |
| 背压 | 队列长度限制，超出返回 503 |
| 改动量 | 小。仅改工具执行路径，加 `multiprocessing.Pool` |
| 稳定性 | 主服务仍是无限制线程模型，但工具是主要内存杀手 |

**风险**：stdlib HTTP server 仍无连接数限制。非工具请求（上传/下载/列表）仍可能被慢客户端耗尽线程。

---

### 方案 C：Nginx 直接代理 gokapi + 瘦 Python API（重构）

把文件下载完全交给 Nginx → gokapi 直连，Python 只管 API 逻辑。

```
用户 → Nginx → /d/*  → gokapi（文件下载，零 Python）
              → /api/* → Python（认证/上传/工具/列表）
```

| 维度 | 效果 |
|------|------|
| 下载零开销 | 文件下载不过 Python，Nginx 直连 gokapi |
| Python 减负 | 只处理 API 逻辑，下载流量完全旁路 |
| 改动量 | 大。需要重构路由和认证流程 |

---

## 三、推荐方案：B → A 渐进

### 第一阶段（B）：立即解决 OOM 炸弹

**改动**：`_execute_and_share` 的工具处理部分放入子进程池。

```python
# main.py 启动时
from multiprocessing import Pool
TOOL_POOL = Pool(processes=2, maxtasksperchild=10)

# _execute_and_share 改为
def _execute_and_share(self, tool, upload, params, user):
    # 读源文件 → 序列化 → 提交到子进程 → 等结果 → 写存储
    result = TOOL_POOL.apply(_worker_process, args=(tool_id, file_bytes, params))
    # ...
```

**效果**：
- 工具处理在子进程，OOM 只杀子进程
- `maxtasksperchild=10`：每 10 次任务回收子进程，释放内存碎片
- `Pool(2)`：最多 2 个并发工具处理，第 3 个排队
- 主服务线程不被 Pillow 阻塞

**时间**：1-2 小时。改动集中在 `_execute_and_share`。

### 第二阶段（A）：生产级稳定性

**前提**：用户量增长到 10+ 并发时执行。

引入 Gunicorn + Bottle：

```python
# app.py
from bottle import Bottle, request, response
app = Bottle()

@app.post('/drop-api/upload')
def upload():
    user = auth(request)
    # ...
```

```ini
# /etc/systemd/system/flashshare.service
ExecStart=/usr/local/bin/gunicorn \
    --workers 2 \
    --threads 2 \
    --max-requests 50 \
    --max-requests-jitter 10 \
    --timeout 30 \
    --bind 127.0.0.1:53843 \
    app:app
```

**时间**：半天。主要工作是 `BaseHTTPRequestHandler` → WSGI 适配。

### N+1 修复（独立于方案，立即执行）

gokapi 有 `/api/files/list` 批量接口（无 file_id = 返回全部），一次请求拿全部文件的实时状态，Python 内 dict 查找。

```python
def list_uploads(user_id):
    rows = con.execute("SELECT ... FROM uploads WHERE user_id=? ...").fetchall()
    # 一次请求拿全部 gokapi 状态
    all_gokapi = get_all_gokapi_files()  # 1 次 HTTP
    gokapi_map = {f["Id"]: f for f in all_gokapi}
    for row in rows:
        live = gokapi_map.get(row["gokapi_id"])  # dict 查找，O(1)
        # ...
```

**效果**：20 次 HTTP → 1 次 HTTP。列表加载从 140ms → 5ms。

---

## 四、实施优先级

| 优先级 | 任务 | 紧急度 | 效果 |
|--------|------|--------|------|
| P0 | N+1 修复（1 次批量替代 20 次串行） | 立即 | 列表 140ms → 5ms |
| P0 | 子进程池隔离工具处理 | 立即 | 防 OOM 全站宕机 |
| P1 | 上传请求加并发限制 | 本周 | 防慢客户端耗尽线程 |
| P2 | Gunicorn + WSGI 适配 | 用户增长时 | 生产级稳定性 |
| P3 | Nginx 直连 gokapi 下载 | 长期 | 下载零 Python 开销 |

---

## 五、约束

- 1GB VPS：总内存 ~954MB，可用 ~230MB（gokapi + Nginx + OS）
- MemoryMax=256M：单进程内存上限
- Pillow 10.2.0：唯一外部依赖
- 不引入 Redis/RabbitMQ/Celery（重，1GB 扛不住）
- 纯 Python stdlib 优先
