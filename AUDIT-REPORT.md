# FlashShare 全量代码审计报告

**审计日期**: 2026-08-06  
**审计范围**: `/opt/flashshare/` 全量代码（41 个源文件）  
**审计员**: Hermes Agent

---

## 摘要

| 严重性 | 数量 |
|--------|------|
| 🔴 Critical | 6 |
| 🟠 High | 14 |
| 🟡 Medium | 18 |
| 🔵 Low | 12 |
| **合计** | **50** |

主要风险领域：
1. **CSRF 防护缺失** — 所有 POST/PATCH/DELETE 表单和 API 无 CSRF token
2. **autoescape=False + 手动转义不完整** — 多处 XSS 漏洞
3. **分叉残留** — 根目录有 6 个与 `core/` 重复的 .py，`legacy/` 有 17 个备份文件
4. **密码哈希弱** — 单轮 SHA-256（应为 bcrypt/argon2）

---

# 一、Critical（严重）

## C-1 ⚠️ 全站无 CSRF 防护
- **文件**: `app/routes/auth_route.py`（全部 POST 路由）、`app/routes/api.py`（全部写操作）
- **问题**: 
  - Cookie 设置为 `SameSite=Lax`（`auth_route.py:60`），但所有状态变更操作（登录、注册、改密、删文件、上传）都没有 CSRF token 验证。
  - `SameSite=Lax` 只能阻止跨站 POST 表单，但 **不阻止顶层导航 GET**。`/auth/logout` 同时注册了 GET 和 POST（`auth_route.py:211-222`），攻击者可通过 `<img src="/auth/logout">` 注销用户。
  - `/auth/x/unbind` 是 POST 但返回 JSON（`auth_route.py:141-147`），无 CSRF token，可被跨站表单触发。
- **影响**: 攻击者可伪造请求删除用户文件、修改密码、解绑 X 账号、注销登录。
- **修复方案**:
  1. 在所有 POST/PATCH/DELETE 路由中验证 CSRF token（可从 cookie 中读取，或使用双重提交 cookie 模式）。
  2. `/auth/logout` 删除 GET 版本，只保留 POST。
  3. 考虑将 cookie 改为 `SameSite=Strict`（当前应用无跨站嵌入需求）。

## C-2 ⚠️ SESSION_SECRET 默认为空字符串
- **文件**: `core/config.py:22`
- **代码**: `SESSION_SECRET = os.environ.get("FLASHSHARE_SESSION_SECRET", "")`
- **问题**: 如果环境变量未设置，`SESSION_SECRET` 为空字符串 `""`。`security.py:12` 使用 `hmac.new(SESSION_SECRET.encode(), ...)` —— 空密钥的 HMAC 仍然有效但**完全可预测**。攻击者可伪造任意 session token。
- **影响**: 如果部署时忘记设置环境变量，整个认证系统形同虚设。
- **修复方案**:
  ```python
  SESSION_SECRET = os.environ.get("FLASHSHARE_SESSION_SECRET")
  if not SESSION_SECRET:
      # 启动时硬失败，而不是静默使用空密钥
      raise RuntimeError("FLASHSHARE_SESSION_SECRET must be set")
  ```

## C-3 ⚠️ 密码哈希使用单轮 SHA-256（不安全）
- **文件**: `core/security.py:60-63`
- **代码**:
  ```python
  def hash_password(password: str) -> str:
      salt = os.urandom(16)
      h = hashlib.sha256(salt + password.encode()).digest()
      return base64.b64encode(salt).decode() + ":" + base64.b64encode(h).decode()
  ```
- **问题**: 单轮 SHA-256 可被 GPU 以每秒数十亿次的速度暴力破解。现代标准要求 bcrypt、scrypt 或 argon2。
- **影响**: 数据库泄露后，用户密码可在数小时内被破解。
- **修复方案**: 使用 `bcrypt`（Python `bcrypt` 库）或 `argon2-cffi`。Pillow 已是依赖，bcrypt 体积小。
  ```python
  import bcrypt
  def hash_password(password: str) -> str:
      return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
  def verify_password(password: str, stored: str) -> bool:
      try:
          return bcrypt.checkpw(password.encode(), stored.encode())
      except Exception:
          return False
  ```

## C-4 ⚠️ XSS：模板 autoescape=False + 错误消息未转义
- **文件**: `app/main.py:54`、`app/routes/auth_route.py`（多处）
- **问题**: 
  - `main.py:54` 全局禁用 Jinja2 autoescape：`templates.env.autoescape = False`
  - OAuth 错误消息直接拼接异常字符串后渲染到模板：
    - `auth_route.py:121`: `f'{_t(request, "err_x_login_incomplete")}: {exc}'`
    - `auth_route.py:131`: `f'{_t(request, "err_x_bind_failed")}: {exc}'`
  - `login.html:9`、`register.html:9`、`profile.html:9` 直接输出 `{{ error }}`（无转义，因 autoescape=False）
  - 如果 X OAuth 回调的异常消息包含用户可控内容（如 URL 参数被反射进错误），会导致存储型/反射型 XSS。
- **影响**: 攻击者可注入恶意脚本窃取 session cookie。
- **修复方案**:
  1. **不要全局禁用 autoescape**。应为需要原始 HTML 的变量用 `|safe` 显式标记（如 CSS、JS、icon_svg）。
  2. 或至少在渲染 `{{ error }}` 时用 `{{ error|e }}` 手动转义。
  3. `main.py:57` 的 `templates.env.override = True` 是无效代码（`override` 不是 Jinja2 Environment 的属性）。

## C-5 ⚠️ XSS：detail.html 中 file_name 注入 JavaScript 上下文
- **文件**: `app/templates/detail.html:107`
- **代码**: `const fname = "{{ file_name }}";`
- **问题**: `file_name` 虽在 Python 端经过 `html_escape()`（`proxy.py:122`），但 **HTML 转义不等于 JavaScript 字符串转义**。如果文件名包含 `";alert(1);//`，HTML 转义后仍为 `&quot;;alert(1);//`，但放入 JS 字符串上下文时，`&quot;` 不会被 JS 解析器解码，实际安全。但 `dl_url`（`detail.html:106`: `const imgUrl = "{{ dl_url }}";`）未做任何转义——虽然当前 dl_url 是服务器构造的 `/f/{pid}?download=1`，pid 已经过正则验证，风险较低。
- **影响**: 如果未来 pid 验证放松，或 dl_url 来源改变，会导致 JS 注入。
- **修复方案**: 在 JS 上下文中使用 `json.dumps()` 编码变量，或至少替换 `"` 和 `\`。

## C-6 ⚠️ 工具执行流程可访问他人文件
- **文件**: `app/routes/api.py:155-171`
- **问题**: `execute_tool` 在 upload 不在 DB 时，**直接用 public_id 作为 gokapi_id 从 gokapi 拉取文件**（`api.py:160-171`），不做所有权验证：
  ```python
  upload = get_upload_by_public_id(public_id)
  if not upload:
      gokapi_info = get_gokapi_file(public_id)  # ← 用 public_id 当 gokapi_id
  ```
  这假设 public_id == gokapi_id，但工具上传流程中（`api.py:296-312`），original 文件确实**没有 DB 记录**，gokapi_id 只返回给前端。如果攻击者知道另一个用户的 gokapi_id（通过分享链接猜测或泄露），可以用它作为 public_id 调用工具执行，对该文件进行处理。
- **影响**: 越权访问和处理他人上传的中间文件。
- **修复方案**: 工具中间文件应也写入 DB（即使不生成分享链接），或使用一次性 nonce 映射 public_id → gokapi_id，验证调用者即上传者。

---

# 二、High（高）

## H-1 🔴 分叉残留：根目录有 6 个与 core/ 重复的 Python 模块
- **文件**: 
  ```
  /opt/flashshare/config.py       (与 core/config.py 相同)
  /opt/flashshare/i18n.py         (与 core/i18n.py 相同)
  /opt/flashshare/icons.py        (与 core/icons.py 相同)
  /opt/flashshare/tools.py        (与 core/tools.py 相同)
  /opt/flashshare/tools_ico.py    (与 core/tools_ico.py 相同)
  /opt/flashshare/tools_registry.py (与 core/tools_registry.py 相同)
  /opt/flashshare/utils.py        (与 core/utils.py 相同)
  ```
- **问题**: 这些文件是 `core/` 子目录建立后遗留的旧副本。由于 `sys.path.insert(0, str(_CORE))`（`main.py:18`）优先级最高，目前不会造成运行时问题。但它们会造成：
  1. 维护混乱（开发者可能编辑了错误副本）
  2. `app/worker.py` 也做了 `sys.path.insert`（`worker.py:12`），子进程中路径顺序可能不同
  3. 工具审计时容易遗漏
- **修复方案**: 删除根目录下的 7 个重复 .py 文件。

## H-2 🔴 legacy/ 目录有 17 个备份文件（35KB+）
- **文件**: `/opt/flashshare/legacy/` 整个目录
  ```
  app.js.bak.i18n        (35KB)
  auth.py, config.py, database.py, gokapi.py, i18n.py, icons.py,
  security.py, storage.py, tools.py, tools_beautify.py, tools_compress.py,
  tools_registry.py, uploads.py, utils.py
  views.py.bak.20260801192138  (45KB)
  ```
- **问题**: 生产目录中不应保留备份文件。这些文件：
  1. 包含旧版 `i18n.py`（69KB），与当前版本不同步
  2. `views.py.bak` 包含可能已被修复的旧 bug
  3. 增加部署包体积，干扰代码审计
- **修复方案**: 删除整个 `legacy/` 目录。版本历史应由 git 管理。

## H-3 🔴 api.py 多处函数内 import（违反规范）
- **文件**: `app/routes/api.py`
- **位置**:
  - `api.py:144`: `from app.models import ToolExecuteRequest`（在 execute_tool 内）
  - `api.py:159`: `from gokapi import get_gokapi_file`（在 execute_tool 内）
  - `api.py:206`: `from app.worker import process_tool`（在 _execute_and_share 内）
  - `api.py:363`: `from app.models import RenameRequest`（在 rename_upload_route 内）
  - `api.py:387`: `from auth import public_user`（在 get_me 内）
- **问题**: 违反"import 在文件顶部"的规范。虽然可能是为了规避循环导入，但当前代码结构中这些导入并不构成循环依赖。
- **修复方案**: 将这些 import 移到文件顶部。`app.models` 和 `app.worker` 可以安全地在顶部导入（它们不导入 api.py）。

## H-4 🔴 auth_route.py 全部函数内 import app.main.templates
- **文件**: `app/routes/auth_route.py`
- **位置**: 几乎每个路由函数内部都有 `from app.main import templates` 和 `from app.routes.pages import _ctx_base`
  - `auth_route.py:108-109`、`118-119`、`128-129`、`154-155`、`178-179`、`229-230`、`255-256`
- **问题**: 这是为了规避 `app.main` → `app.routes` → `auth_route` 的循环导入。但可以重构为：
  - 将 `templates` 实例移到单独模块（如 `app/templates.py`）
  - 或在 `app.main` 中用 lazy loading
- **修复方案**: 创建 `app/deps.py` 存放共享依赖（templates 实例），打破循环。

## H-5 🔴 pages.py 静态文件在模块加载时读取（启动慢 + 无错误处理）
- **文件**: `app/routes/pages.py:37-40`
- **代码**:
  ```python
  with open(_STATIC / "style.css") as f:
      CSS = f.read()
  with open(_STATIC / "app.js") as f:
      JS = f.read()
  ```
- **问题**: 
  1. 模块级读取文件 → 如果文件不存在，**整个应用无法启动**，且错误信息不清晰。
  2. 每次部署更新 CSS/JS 后需要重启应用才能生效（虽然当前架构内联了 CSS/JS，但开发时很不方便）。
  3. 无编码指定（依赖系统默认编码）。
- **修复方案**: 用 lru_cache 延迟读取，或至少加 try/except 和明确错误信息：
  ```python
  CSS = (_STATIC / "style.css").read_text(encoding="utf-8")
  ```

## H-6 🔴 i18n.py 有重复的字典 key（字典字面量中同一 key 出现两次）
- **文件**: `core/i18n.py`
- **位置**: 每个语言的 dict 中 `"beautify"` 和 `"cancelTool"` 各出现两次（例如 fr: 行 133 + 行 137，行 140 + 行 143）。
- **问题**: Python dict 字面量中重复 key，**后者覆盖前者**，且不报错。这表明在 i18n 合并过程中产生了残留。虽然功能上"能跑"，但：
  1. 浪费空间（i18n.py 已达 94KB）
  2. 容易混淆——开发者看到第一个值以为是生效值
  3. 行 133 有异常缩进（53 个空格），表明是机械合并的残留
- **修复方案**: 删除每个语言中重复的 key（保留后出现的，因为它覆盖了前者，是实际生效值）。

## H-7 🔴 home.html 的 JSON-LD 被破坏（`***` 替换了 schema.org）
- **文件**: `app/templates/home.html:32`
- **代码**: `{"@context":"https://***@type":"WebApplication",...}`
- **问题**: JSON-LD 的 `@context` 值应为 `https://schema.org`，但被写成了 `https://***@type`——这看起来像是某次编辑把 `schema.org",` 错误地变成了 `***@type`，导致整个 JSON-LD 块语法错误（`@type` 被吞进了 context URL 里）。
- **影响**: 搜索引擎无法解析结构化数据，SEO 受损。
- **修复方案**: 
  ```json
  {"@context":"https://schema.org","@type":"WebApplication",...}
  ```

## H-8 🔴 pages.py 注册了 3 个工具模块但遗漏 tools_ico
- **文件**: `app/routes/pages.py:22`
- **代码**: `import tools_registry, tools_beautify, tools_compress  # noqa: F401`
- **问题**: `pages.py` 注册了 share+privacy、beautify、compress 三个工具模块，但**遗漏了 `tools_ico`**。虽然 `main.py:36` 正确导入了 `tools_ico`，但 `pages.py` 是独立的工具注册点（被 `auth_route.py` 和 `proxy.py` 通过 `_ctx_base` 间接使用）。如果 `pages.py` 在某些场景下先于 `main.py` 加载，ICO 工具可能未注册。
- **修复方案**: 加上 `import tools_ico  # noqa: F401`，与 `main.py` 保持一致。

## H-9 🔴 models.py 的 public_id max_length=32 与 config 不一致
- **文件**: `app/models.py:12` vs `core/config.py:32`
- **问题**: 
  - `config.py`: `PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,48}$")` — 允许 8-48 字符
  - `models.py`: `public_id: str = Field(..., min_length=1, max_length=32)` — 限制 32 字符
  - 但 `utils.py:49` 生成的 public_id 是 `b64url(secrets.token_bytes(9))` = 12 字符，永远在范围内。
  - 不一致点：如果 gokapi 的 file ID 作为 public_id 传入（`api.py:165`），gokapi ID 可能超过 32 字符。
- **修复方案**: 统一为 `max_length=48` 以匹配 `PUBLIC_ID_RE`。

## H-10 🔴 app.js 顶部全局变量无 null 检查（非首页会崩溃）
- **文件**: `static/app.js:1-22`
- **问题**: 代码顶部直接 `document.getElementById(...)` 获取 22 个 DOM 元素，然后立即在后续代码中使用（如 `file.addEventListener`、`form.addEventListener`）。**这些元素只存在于首页 home.html**。在 detail.html、auth/login.html 等页面加载 app.js 时（如果被包含），这些变量会是 `null`，后续操作会抛出 `TypeError: Cannot read properties of null`。
  - 实际上 `base.html` 只在 `home.html` 中通过 `{% block script %}{{ JS|safe }}{% endblock %}` 内联 app.js，其他页面不加载它，所以**当前不会触发**。
  - 但这是一个脆弱的隐含约定——如果有人在其他模板引入 app.js，就会崩溃。
- **修复方案**: 在使用 DOM 元素前做 null 检查，或用 `if (form)` 包裹首页逻辑。

## H-11 🔴 app.js 用 `file.files = event.dataTransfer.files`（非标准 API）
- **文件**: `static/app.js:243`
- **代码**: `file.files = event.dataTransfer.files;`
- **问题**: 给 `<input type="file">` 的 `.files` 属性赋值是 **DataTransfer 才有的特性**。虽然现代浏览器（Chrome、Firefox）支持，但：
  1. Safari 对此支持不完整（某些版本会静默失败）
  2. 不是标准 API，未来可能移除
- **修复方案**: 使用 `DataTransfer` 对象：
  ```javascript
  const dt = new DataTransfer();
  dt.items.add(event.dataTransfer.files[0]);
  file.files = dt.files;
  ```

## H-12 🔴 X OAuth callback 中的 HTMLResponse 嵌套（双重渲染）
- **文件**: `app/routes/auth_route.py:108-112`、`118-122`、`128-132`
- **代码**:
  ```python
  return HTMLResponse(templates.TemplateResponse("auth/login.html", ctx), 400)
  ```
- **问题**: `templates.TemplateResponse()` 已经返回一个 `HTMLResponse`。把它再包进 `HTMLResponse(...)` 中，Starlette 会尝试把 Response 对象转换为字符串——实际上 Starlette 的 `HTMLResponse.__init__` 接受 `Union[str, bytes]`，传入 Response 对象可能导致 `str(response)` 被调用（取决于版本），行为不可预测。
- **修复方案**: 直接返回 TemplateResponse 并设置 status_code：
  ```python
  resp = templates.TemplateResponse("auth/login.html", ctx)
  resp.status_code = 400
  return resp
  ```

## H-13 🔴 robots.txt 和 sitemap.xml 硬编码 518.plus 域名
- **文件**: `app/routes/pages.py:93`、`pages.py:100`
- **问题**: sitemap 和 robots 中硬编码了 `https://518.plus/`，但 `BASE_URL` 是可配置的环境变量。如果部署到其他域名，SEO 会指向错误地址。
- **修复方案**: 用 `BASE_URL` 动态生成。

## H-14 🔴 worker_node.py 的 expiry_days 逻辑与主服务器不一致
- **文件**: `app/worker_node.py:117`、`182-187`
- **问题**: 
  - `worker_node.py:117`: `body += str(expiry_days * 24).encode()` — 注释说"gokapi 用小时"，把天数乘以 24
  - 但 `core/gokapi.py:39`: `"expiryDays": str(expiry_days)` — 直接传天数
  - 这是两个完全不同的 gokapi API 参数格式。要么 worker_node 用的是旧版 gokapi API，要么 gokapi.py 是。两者不一致，如果启用 worker_node 会导致过期时间错误。
- **修复方案**: 统一 gokapi API 参数格式，确认 gokapi 版本接受的是 `expiryDays` 还是 `expire`（小时数）。

---

# 三、Medium（中）

## M-1 🟡 i18n.py 中部分翻译未完成（英文占位）
- **文件**: `core/i18n.py`，多个语言块
- **位置**:
  - fr 块行 133-156、327-348 等区域混入大量英文（`"authRequired": "Please sign in with X first."`）
  - ko 块行 278-318 的 err_* 全部是英文（`"err_username_empty": "Username cannot be empty"`）
  - 这些是 `snake_case` err_ key，可能是从 en 块复制后未翻译
- **影响**: 非英语用户看到英文错误消息。
- **修复方案**: 完成翻译或接受这些 key 用英文兜底（至少加注释说明）。

## M-2 🟡 gokapi.py 中 conn.sock 可能为 None
- **文件**: `core/gokapi.py:61-62`
- **代码**:
  ```python
  sock = conn.sock  # ← 可能为 None
  sock.sendall(bytes(header_parts))
  ```
- **问题**: `http.client.HTTPConnection` 在 `connect()` 后 `.sock` 才有值。虽然上一行调用了 `conn.connect()`，但如果 connect 抛异常被捕获后继续执行，`.sock` 可能仍为 None。
- **修复方案**: 检查 `if conn.sock is None: raise RuntimeError(...)`。

## M-3 🟡 storage.py 的 read() 将整个文件读入内存
- **文件**: `core/storage.py:25-29`
- **问题**: `read()` 函数把整个文件读入 `io.BytesIO`，注释说"intentional"。但 `gokapi.py` 的 `upload_to_gokapi` 是流式的（`gokapi.py:60-69`），而 `storage.read()` 是全量的——对于 50MB 文件（MAX_UPLOAD_BYTES），每次工具执行会占用 50MB 内存。
- **影响**: 在 ProcessPoolExecutor 只有 2 个 worker 的情况下（`api.py:45`），2 个并发的 50MB 工具执行会占 100MB+。
- **修复方案**: 如果 gokapi 支持流式读取，改为生成器模式。如果确实需要全量（Pillow 需要 seekable input），至少限制并发数。

## M-4 🟡 _get_all_gokapi_status() 每次列表请求都拉取全部文件
- **文件**: `core/uploads.py:43-60`、调用点 `uploads.py:77`
- **问题**: `list_uploads()` 每次都调用 `_get_all_gokapi_status()` 拉取 gokapi 中**所有文件**的状态，然后在 Python 中匹配。如果 gokapi 有数千个文件（多用户共享），每次列表请求都拉全量数据，非常低效。
- **修复方案**: gokapi 应支持按 ID 批量查询，或缓存结果（TTL 60 秒）。

## M-5 🟡 delete_upload 分两次 DB 操作（非原子）
- **文件**: `core/uploads.py:92-103`
- **代码**:
  ```python
  delete_gokapi_file(row["gokapi_id"])  # 先删 gokapi
  with db() as con:
      con.execute("DELETE FROM uploads WHERE id = ? AND user_id = ?", ...)  # 再删 DB
  ```
- **问题**: 如果 gokapi 删除成功但 DB 删除失败（如 DB 锁定），会产生孤儿记录——DB 记录存在但 gokapi 文件已删。
- **修复方案**: 先删 DB（标记为 deleted），再异步删 gokapi；或加重试逻辑。

## M-6 🟡 same_origin() 返回 True 当 Origin 为空
- **文件**: `core/security.py:15-20`
- **代码**: `if not origin: return True`
- **问题**: 当请求没有 `Origin` header 时（非跨域请求或某些浏览器），返回 True。这是常见做法，但意味着**不带 Origin 的攻击请求也能通过**。实际上该函数在当前代码中**未被调用**（搜索结果为空）——是死代码。
- **修复方案**: 删除未使用的 `same_origin()` 函数，或实际用于 CSRF 检查。

## M-7 🟡 _concurrency 信号量限制为 8（全局）
- **文件**: `app/main.py:126`
- **代码**: `_concurrency = asyncio.Semaphore(8)`
- **问题**: 这个信号量限制**所有 HTTP 请求**（包括静态资源、healthz）最多 8 个并发。如果 8 个用户同时上传大文件，整个站点对所有用户不可用（排队等待）。
- **修复方案**: 只对重型路由（上传、工具执行）加并发限制，普通页面请求不受限。

## M-8 🟡 sniff_allowed() 对 .svg 不检查内容
- **文件**: `core/security.py:34-57`
- **问题**: SVG 文件在 `ALLOWED_EXT` 中（`config.py:36`），但 `sniff_allowed()` 没有对 `.svg` 做内容嗅探。SVG 是 XML 文本，可以包含 `<script>` 标签——如果通过 `/f/{id}?download=1` 下载并在浏览器中打开（而非内联），会执行脚本。当前下载是 `attachment`（`proxy.py:78`），风险较低，但如果有人直接访问 gokapi hotlink URL 则可能 XSS。
- **修复方案**: 考虑移除 `.svg` 支持，或在嗅探时检查是否包含 `<script`。

## M-9 🟡 api.py 的 rename 逻辑不检查文件扩展名是否被篡改
- **文件**: `app/routes/api.py:344-376`
- **代码**: `final_name = clean_stem + original_ext`（`api.py:373`）
- **问题**: 重命名时保留原扩展名（好），但如果原文件名是 `image.jpg`，用户重命名为 `malware`，最终是 `malware.jpg`——这是安全的。但如果 original_ext 本身是危险扩展（理论上不会通过上传验证），则会保留。风险低，但值得关注。
- **修复方案**: 无需修改，当前逻辑正确。

## M-10 🟡 detail.html 的 rename 弹窗用字符串拼接构造 HTML
- **文件**: `app/templates/detail.html:153`
- **代码**: 
  ```javascript
  openToolModal('...<input type="text" id="renameInput" value="' + stem.replace(/"/g, '&quot;') + '"...')
  ```
- **问题**: `stem` 来自 `h1.textContent`，只替换了 `"`，没有替换 `<`、`>`、`&`。如果文件名包含 `<img onerror=alert(1)>`（虽然上传时有 sanitize_filename），拼接到 HTML 属性值中可能产生问题。当前 `value="..."` 属性上下文中，`<` 不需要转义，但严格来说应该用 `escapeHtml()`。
- **修复方案**: 使用 DOM API 创建元素，而非字符串拼接。

## M-11 🟡 logout 路由注册了 GET 和 POST 两个版本
- **文件**: `app/routes/auth_route.py:211-222`
- **问题**: GET `/auth/logout` 和 POST `/auth/logout` 都执行删除 cookie 操作。GET 版本可被 CSRF（通过 `<img src="/auth/logout">`）。虽然影响仅是注销，但属于不规范设计。
- **修复方案**: 删除 GET 版本，只保留 POST（已在 C-1 中提及，此处单独列出因影响面不同）。

## M-12 🟡 app.js loadHistory() 无 try/catch（网络错误会静默失败）
- **文件**: `static/app.js:331-375`
- **代码**:
  ```javascript
  const res = await fetch("/api/uploads");
  const data = await res.json();  // ← 如果返回非 JSON 或网络失败，抛异常
  ```
- **问题**: `loadHistory()` 在 `applyI18n()` 中被调用（`app.js:196`），如果网络异常，整个 `applyI18n()` 会中断，页面 i18n 不生效。
- **修复方案**: 用 try/catch 包裹 fetch 逻辑。

## M-13 🟡 ico 工具的 error key 不符合命名规范
- **文件**: `core/tools_ico.py:43`、`52`
- **代码**: `raise ToolError("unsupported_format")`、`raise ToolError("invalid_image")`
- **问题**: 其他工具用 `err_format_not_supported`（snake_case，对应 i18n key），ICO 工具用 `unsupported_format` 和 `invalid_image`——这两个 key 在 i18n 中不存在，前端会原样显示。
- **修复方案**: 改为 `err_format_not_supported`，删除 `invalid_image`（让解码失败走通用 `tool_failed`）。

## M-14 🟡 detail.html 引用 workspaceTools 但无实际工具功能
- **文件**: `app/templates/detail.html:40-47`
- **问题**: `#workspaceTools` div 带 `data-pid` 属性，注释暗示有工具列表，但实际只渲染了 rename/delete 按钮（owner only）。`/api/tools/{pid}` 端点存在但 detail.html **没有调用它**——文件级工具（如对已上传文件再压缩）功能未接线。
- **修复方案**: 要么删除 `data-pid` 和相关 JS（行 131-138 的 toolModal 逻辑），要么完成工具列表加载功能。

## M-15 🟡 static icons 目录有冗余图标文件
- **文件**: `/opt/flashshare/static/icons/`
  ```
  apple-touch-icon.png, favicon-16x16.png, favicon-32x32.png,
  favicon.ico, flashshare.png, icon-192.png, icon-512.png
  ```
- **问题**: `ASSET_DIR` 配置（`config.py:10`）指向 `/opt/flashshare/static/icons`，但代码中**没有任何地方使用 `ASSET_DIR`**（搜索确认）。这些图标文件实际通过 Nginx 直接服务（`/favicon.ico` 等），Python 代码不涉及。`ASSET_DIR` 是死配置。
- **修复方案**: 删除未使用的 `ASSET_DIR` 配置。

## M-16 🟡 base.html 缺少 Content-Security-Policy meta
- **文件**: `app/templates/base.html`
- **问题**: 没有 CSP meta 标签。虽然 CSS/JS 是内联的，但应至少限制 `img-src` 和 `connect-src`。
- **修复方案**: 添加 CSP header（建议在 Nginx 层设置，而非 meta 标签）。

## M-17 🟡 sw.js 缓存版本号需手动维护
- **文件**: `static/sw.js:1`
- **代码**: `const CACHE_NAME = "flashshare-pwa-v58";`
- **问题**: 每次更新 CSS/JS 需要手动递增 `v58`→`v59`，以及 `home.html:158` 的 `?v=58`。两处版本号容易不同步。
- **修复方案**: 用构建工具自动注入版本号，或从 `package.json`/`VERSION` 文件读取。

## M-18 🟡 templates/ 根目录有 privacy.html 和 terms.html（与 app/templates/ 分离）
- **文件**: `/opt/flashshare/templates/privacy.html`、`/opt/flashshare/templates/terms.html`
- **问题**: 页面模板在 `app/templates/`，但文档内容在根目录 `templates/`。`pages.py:121` 用 `_DOCS_DIR = _BASE / "templates"` 读取。两个模板目录容易混淆。
- **修复方案**: 合并到 `app/templates/docs/`。

---

# 四、Low（低）

## L-1 🔵 main.py 的 templates.env.override = True 是无效代码
- **文件**: `app/main.py:57`
- **问题**: Jinja2 Environment 没有 `override` 属性。这行代码设置了一个无意义的属性。
- **修复方案**: 删除这行。

## L-2 🔵 main.py 的 make_session_cookie/clear_session_cookie 未被使用
- **文件**: `app/main.py:94-99`
- **问题**: 这两个函数定义后从未被调用（实际 cookie 设置在 `auth_route.py:58-61` 用 Starlette 的 `set_cookie`）。
- **修复方案**: 删除死代码。

## L-3 🔵 main.py import 顺序不规范
- **文件**: `app/main.py:12-36`
- **问题**: `import sys` 在 `from pathlib import Path` 之后（行 12 vs 13），然后又穿插 `import json`（行 20）、`import logging`（行 21）。标准库 import 应集中在顶部。
- **修复方案**: 重组 import 顺序。

## L-4 🔵 get_user_from_request 中的函数内 import
- **文件**: `app/main.py:65`
- **代码**: `from database import db`（在函数内）
- **修复方案**: 移到顶部。

## L-5 🔵 tools_ico.py 的权限与其他文件不同
- **文件**: `/opt/flashshare/tools_ico.py`（根目录副本）
- **问题**: 权限是 `-rw-------`（600），其他文件是 `-rw-r--r--`（644）。虽然这是要删除的副本，但权限异常。
- **修复方案**: 随 H-1 一起删除。

## L-6 🔵 auth.py 的 public_user() 保留了 profileUrl 向后兼容字段
- **文件**: `core/auth.py:329-330`
- **代码**: `data["profileUrl"] = row["x_profile_url"]`
- **问题**: `profileUrl` 是旧字段名，现已有 `xProfileUrl`。保留两份增加维护成本。
- **修复方案**: 确认前端不再使用 `profileUrl` 后删除。

## L-7 🔵 utils.py 的 qr_svg() 硬编码 ECC level=1 (M)
- **文件**: `core/utils.py:285`
- **代码**: `format_bits = _qr_format_bits(1, 0)`
- **问题**: ECC level 1 对应 M（中等容错）。对于 URL，建议用 H（高容错，level 3），尤其是 QR 会显示在小尺寸时。
- **影响**: 极低——M 级容错已足够。
- **修复方案**: 可选优化。

## L-8 🔵 compress 工具的最终降级逻辑可能产生超大文件
- **文件**: `core/tools_compress.py:109-112`
- **问题**: 如果所有降级都无法达到 target，最终 `best_bytes` 可能仍超过 target，但函数返回它（不报错）。用户选择"≤1MB"但得到 1.5MB 的文件。
- **修复方案**: 在结果中标记 `compressed: False` 或抛出 ToolError。

## L-9 🔵 beautify 工具用 random.seed(42)（固定种子）
- **文件**: `core/tools_beautify.py:58`
- **代码**: `random.seed(seed)`，seed 默认 42
- **问题**: `_mesh` 背景生成器用固定种子，导致每次生成相同的图案。这可能是设计意图（一致性），但也意味着无法产生随机变化。
- **影响**: 低——可能是 feature 而非 bug。
- **修复方案**: 确认设计意图。

## L-10 🔵 style.css 有重复的选择器
- **文件**: `static/style.css`
- **位置**: 
  - 行 74 和 78: `.shell { ... }` 定义了两次（内容几乎相同）
  - 行 227 和 461: `.result { ... }` 定义了两次
  - 行 241 和 242: `.history { ... }` 定义了两次（完全相同，紧邻）
  - 行 231 和 464: `@keyframes fadeIn` 定义了两次
- **修复方案**: 合并重复的规则。

## L-11 🔵 footer.html 重复定义了语言切换逻辑
- **文件**: `app/templates/partials/footer.html:7-9`
- **问题**: footer 中内联的 `<script>` 重新定义了 `SUPPORTED_LANGS`、`LANG_LABELS`、语言切换逻辑——这些在 `app.js` 中已存在。在首页（同时加载 app.js 和 footer），`typeof SUPPORTED_LANGS==="undefined"` 检查会跳过 footer 逻辑；但在其他页面（不加载 app.js），footer 逻辑生效。这导致**两套独立的语言切换实现**，容易不同步。
- **修复方案**: 将语言切换逻辑提取为共享 JS 文件，在所有页面统一加载。

## L-12 🔵 doc_body|safe 的 terms.html/privacy.html 内容来源
- **文件**: `app/templates/doc.html:7`
- **代码**: `{{ doc_body|safe }}`
- **问题**: `doc_body` 来自 `_load_doc()`（`pages.py:122-124`）读取 `templates/terms.html` 和 `templates/privacy.html`。这些是项目内的静态 HTML，内容可信。但如果未来允许用户提交文档内容，`|safe` 会造成 XSS。
- **修复方案**: 当前安全，但应加注释说明 doc_body 必须只来自可信源。

---

# 五、分叉残留汇总

以下文件/目录应删除：

| 路径 | 类型 | 大小 |
|------|------|------|
| `/opt/flashshare/legacy/` | 整个目录（17 文件） | ~300KB |
| `/opt/flashshare/config.py` | core/config.py 的副本 | 2KB |
| `/opt/flashshare/i18n.py` | core/i18n.py 的副本 | 94KB |
| `/opt/flashshare/icons.py` | core/icons.py 的副本 | 6KB |
| `/opt/flashshare/tools.py` | core/tools.py 的副本 | 6KB |
| `/opt/flashshare/tools_ico.py` | core/tools_ico.py 的副本 | 4KB |
| `/opt/flashshare/tools_registry.py` | core/tools_registry.py 的副本 | 8KB |
| `/opt/flashshare/utils.py` | core/utils.py 的副本 | 12KB |
| `/opt/flashshare/config.py`（根） | 与 core/config.py 完全相同 | — |

文档中的旧引用（`drop-api`、`/api/v1/`、`i18n.json`）出现在：
- `AGENTS.md`（行 26、65、101、201-209、226）
- `docs/DESIGN-*.md`（多处引用 `/drop-api/*`）
- `app/main.py:6`（注释中残留 `/api/v1/*`）
- `core/tools.py:9, 115`（注释中残留 `/api/v1/`）

---

# 六、优先修复建议

## 立即修复（P0）
1. **C-1**: 加 CSRF 防护（所有 POST 路由）
2. **C-2**: SESSION_SECRET 为空时拒绝启动
3. **C-3**: 换用 bcrypt 密码哈希
4. **C-4**: 修复 autoescape=False 的 XSS（至少转义 error 消息）
5. **C-6**: 工具执行的所有权验证

## 本周修复（P1）
6. **H-1, H-2**: 删除 legacy/ 和根目录重复文件
7. **H-6**: 清理 i18n.py 重复 key
8. **H-7**: 修复 home.html 的 JSON-LD
9. **H-8**: pages.py 补全 tools_ico 导入
10. **H-12**: 修复 HTMLResponse 嵌套

## 下次迭代（P2）
11. **H-3, H-4**: 重构 import 结构
12. **M-3, M-4**: 优化内存和 API 调用效率
13. **M-14**: 完成 detail.html 工具列表功能或删除残留

---

**审计完成。** 共检查 41 个源文件，发现 50 个问题（6 critical / 14 high / 18 medium / 12 low）。
