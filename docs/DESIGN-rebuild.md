# FlashShare 架构重建方案：FastAPI 全栈

## 一、资产盘点

### 零改动迁移（不动一行）

| 文件 | 行数 | 说明 |
|------|------|------|
| tools.py | 157 | 工具注册框架 |
| tools_registry.py | 193 | 图片净化 + 默认工具 |
| tools_beautify.py | 225 | 截图美化 |
| tools_compress.py | 159 | 图片压缩 |
| icons.py | 88 | 图标注册表 |
| i18n.py | 1380 | 服务端语言包 |
| config.py | 54 | 配置常量 |
| gokapi.py | 123 | gokapi HTTP 客户端 |
| storage.py | 68 | 存储适配器 |
| static/app.js | 769 | 前端逻辑 |
| static/style.css | 524 | 样式 |
| static/sw.js | 33 | Service Worker |
| static/i18n.json | 673 | 客户端语言包 |
| **小计** | **4643** | **核心资产完整保留** |

### 逻辑不变，接口适配（薄封装）

| 文件 | 行数 | 说明 |
|------|------|------|
| database.py | 175 | db() → 不变，去掉 Flask 无关代码 |
| auth.py | 337 | 全部函数不变，FastAPI 直接调用 |
| uploads.py | 103 | N+1 已修，不变 |
| security.py | 73 | sniff_allowed 不变 |
| multipart.py | 237 | 废弃。FastAPI 的 UploadFile 替代 |
| utils.py | 377 | format 函数不变，icon_svg 委托不变 |

### 重写

| 文件 | 行数 | 说明 |
|------|------|------|
| main.py | 877 | BaseHTTPRequestHandler → FastAPI 路由 |
| views.py | 773 | HTML 模板 → Jinja2Templates |

**重写量：~1650 行 → ~800 行**（FastAPI 路由声明式，比手写 handler 短 40%）

---

## 二、目标架构

```
用户 → Cloudflare → Nginx → Uvicorn (FastAPI)
                               │
                               ├── 中间件：认证 / 限流 / CORS
                               ├── Jinja2：首页 / 详情页 / 认证页
                               ├── API 路由：上传 / 列表 / 工具 / 删除 / 重命名
                               ├── StaticFiles：CSS / JS / 图标
                               ├── 流式代理：文件下载 → gokapi
                               └── Worker Pool：工具处理（子进程隔离）
```

### 进程模型

| 进程 | 内存 | 职责 |
|------|------|------|
| Uvicorn | ~50MB | API + 路由 + 认证 + 模板 + 代理 |
| Worker 1 | ~85MB | Pillow 工具处理 |
| Worker 2 | ~85MB | Pillow 工具处理 |
| gokapi | ~20MB | 文件存储 |
| Nginx | ~10MB | 反代 + TLS |
| **总计** | **~250MB** | 1GB VPS 安全 |

### 并发模型

- Uvicorn `--workers 1`：异步单进程，IO（gokapi 调用、文件流）不阻塞
- 工具处理：`ProcessPoolExecutor(max_workers=2)`，CPU 密集在子进程
- 下载代理：FastAPI `StreamingResponse`，分块转发，不占内存
- 限流：FastAPI middleware `Semaphore(8)`，超出返回 429

---

## 三、目录结构

```
/opt/flashshare/
├── app/
│   ├── main.py          # FastAPI 实例 + 中间件 + 启动
│   ├── routes/
│   │   ├── pages.py     # 首页 / 详情页 / 认证页 (Jinja2)
│   │   ├── api.py       # /drop-api/* API 路由
│   │   ├── auth.py      # /auth/* 认证流程
│   │   └── proxy.py     # 文件下载 / 静态文件代理
│   ├── middleware.py    # 认证中间件 / 限流
│   ├── templates/       # Jinja2 HTML 模板
│   │   ├── base.html    # 公共 head/nav/footer
│   │   ├── home.html    # 首页
│   │   ├── detail.html  # 详情页
│   │   └── auth/        # 登录/注册/资料
│   └── worker.py        # 子进程工具执行入口
├── core/                # 零改动的核心逻辑（原样搬过来）
│   ├── tools.py
│   ├── tools_registry.py
│   ├── tools_beautify.py
│   ├── tools_compress.py
│   ├── icons.py
│   ├── i18n.py
│   ├── config.py
│   ├── gokapi.py
│   ├── storage.py
│   ├── database.py
│   ├── auth.py
│   ├── uploads.py
│   ├── security.py
│   └── utils.py
├── static/              # 不动
│   ├── app.js
│   ├── style.css
│   ├── sw.js
│   └── i18n.json
├── docs/                # 不动
└── requirements.txt     # 新增 fastapi + uvicorn + jinja2 + python-multipart
```

---

## 四、路由映射

### GET（页面 + 静态）

| 路径 | 现在 | 之后 | 处理 |
|------|------|------|------|
| `/` | render_home | `GET /` → Jinja2 home.html | 服务端渲染 + __TOOLS__ + __ICONS__ 注入 |
| `/f/{pid}` | render_file_landing | `GET /f/{pid}` → Jinja2 detail.html | 详情页 |
| `/f/{pid}?download=1` | proxy_gokapi_download | `GET /f/{pid}` query param → StreamingResponse | 流式下载 |
| `/f/{pid}/qr.svg` | qr_svg | `GET /f/{pid}/qr.svg` → SVG response | QR 码 |
| `/auth/login` | render_login | `GET /auth/login` → Jinja2 | 登录页 |
| `/auth/register` | render_register | `GET /auth/register` → Jinja2 | 注册页 |
| `/auth/profile` | render_profile | `GET /auth/profile` → Jinja2 | 资料页 |
| `/auth/x/login` | redirect to X | `GET /auth/x/login` → 302 | OAuth 跳转 |
| `/auth/x/callback` | handle callback | `GET /auth/x/callback` → set cookie + 302 | OAuth 回调 |
| `/static/*` | handle_static | `StaticFiles` 自动 | 静态文件 |

### POST（API）

| 路径 | 现在 | 之后 | 处理 |
|------|------|------|------|
| `/drop-api/upload` | handle_upload | `POST /drop-api/upload` → UploadFile | 上传 |
| `/drop-api/tool` | handle_execute_tool | `POST /drop-api/tool` → JSON | 工具执行（子进程） |
| `/drop-api/uploads` | handle_uploads | `GET /drop-api/uploads` → JSON | 列表 |
| `/drop-api/delete` | handle_delete_upload | `POST /drop-api/delete` → JSON | 删除 |
| `/drop-api/rename` | handle_rename_upload | `POST /drop-api/rename` → JSON | 重命名 |
| `/drop-api/tools` | handle_list_all_tools | `GET /drop-api/tools` → JSON | 工具清单 |
| `/drop-api/tools/{pid}` | handle_get_tools | `GET /drop-api/tools/{pid}` → JSON | 文件工具 |
| `/auth/login` | handle_local_login | `POST /auth/login` → form | 本地登录 |
| `/auth/register` | handle_register | `POST /auth/register` → form | 注册 |

---

## 五、关键设计

### 5.1 认证中间件

```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # 从 cookie 读 session → 查 DB → 挂到 request.state
    token = request.cookies.get(COOKIE_NAME)
    request.state.user = get_user_from_session(token) if token else None
    response = await call_next(request)
    return response
```

所有路由通过 `request.state.user` 获取当前用户，不需要每个 handler 重复写。

### 5.2 工具处理子进程

```python
from concurrent.futures import ProcessPoolExecutor

process_pool = ProcessPoolExecutor(max_workers=2)

@app.post("/drop-api/tool")
async def execute_tool(request: Request):
    user = request.state.user
    # ... 校验 ...
    # 读源文件 → bytes
    source_bytes = await storage.read_bytes(upload.gokapi_id)
    # 提交到子进程
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        process_pool,
        worker.process_tool, tool_id, source_bytes, upload_dict, params
    )
    # 写结果 → 返回新分享链
```

### 5.3 下载代理（流式，零内存）

```python
@app.get("/f/{pid}")
async def file_page(pid: str, download: int = 0, request: Request):
    upload = get_upload_by_public_id(pid)
    if download:
        # 流式代理 gokapi — 不把文件读进内存
        status, headers, stream = storage.read(upload["gokapi_id"])
        return StreamingResponse(
            stream,
            media_type=upload["content_type"],
            headers={"Content-Disposition": f"attachment; filename={upload['file_name']}"}
        )
    # 否则渲染详情页
    return templates.TemplateResponse("detail.html", {...})
```

### 5.4 模板渲染

Jinja2 模板替代 views.py 的 f-string 拼接：

```html
<!-- base.html -->
<!doctype html>
<html lang="{{ locale }}" dir="{{ direction }}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {% block head %}{% endblock %}
</head>
<body>
  {{ shared_header(user, lang, texts) }}
  {% block content %}{% endblock %}
  <script>
    window.__USER__ = {{ user_json }};
    window.__LIMITS__ = {{ limits_json }};
    window.__I18N__ = {{ i18n_json }};
    window.__TOOLS__ = {{ tools_json }};
    window.__ICONS__ = {{ icons_json }};
  </script>
</body>
</html>
```

### 5.5 Nginx 配置

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;  # 从 300s 降到 60s
    proxy_connect_timeout 5s;
}
```

### 5.6 systemd

```ini
[Service]
ExecStart=/usr/local/bin/uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips=127.0.0.1
MemoryMax=512M
```

---

## 六、迁移步骤（不中断服务）

| 步骤 | 内容 | 可验证 | 回滚 |
|------|------|--------|------|
| 1 | 装 FastAPI + Uvicorn + Jinja2 | import 成功 | pip uninstall |
| 2 | 建 app/ + core/ 目录，搬核心文件 | 单元测试通过 | 删目录 |
| 3 | 实现 app/main.py + 中间件 + 基础路由 | curl 测试 | Nginx 不切 |
| 4 | 实现 API 路由（上传/列表/工具/删除） | API 测试 | Nginx 不切 |
| 5 | 实现 Jinja2 模板（首页/详情/认证） | 页面渲染 | Nginx 不切 |
| 6 | 实现下载代理 + 静态文件 | 下载测试 | Nginx 不切 |
| 7 | 全量回归测试（功能 + 性能） | 通过 | Nginx 不切 |
| 8 | Nginx upstream 切到 8000 | 线上验证 | 切回 53843 |

步骤 1-7 在新端口（8000）跑，旧服务（53843）不中断。步骤 8 切换是秒级。

---

## 七、依赖

```
# requirements.txt
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
jinja2>=3.1,<4.0
python-multipart>=0.0.9
Pillow>=10.0,<11.0
```

---

## 八、废弃

| 文件 | 原因 |
|------|------|
| main.py | BaseHTTPRequestHandler → FastAPI |
| views.py | f-string 拼接 → Jinja2 |
| multipart.py | FastAPI UploadFile 替代 |

---

## 九、验收标准

| 指标 | 目标 |
|------|------|
| 全部功能 | 和当前一致（工具/上传/下载/认证/列表/语言/图标） |
| 3 并发工具 | 不 OOM，主服务不挂 |
| 列表加载 | < 100ms |
| 下载 | 流式代理，内存 < 10MB |
| 单工具处理 | < 5s |
| 内存峰值 | < 400MB |
| SW / PWA | 正常工作 |
