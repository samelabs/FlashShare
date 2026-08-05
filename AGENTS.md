# AGENTS.md — 518.plus FlashShare AI 开发规范

> 所有 AI agent 开发前必须完整阅读本文件。违反规范导致的 bug 由 agent 全责。

## 项目概况

518.plus 是临时文件分享服务，部署在 Oracle Cloud 1GB VPS（东京）。

- 域名：518.plus（Cloudflare 代理 → Nginx → Uvicorn → Gokapi）
- 技术栈：Python 3.12 + FastAPI + Uvicorn + Jinja2 + Pillow + Gokapi（Go）+ Nginx + SQLite + Cloudflare
- 生产路径：`/opt/flashshare/`
- 运行用户：`ubuntu`
- 数据库：`/var/lib/flashshare/flashshare.sqlite`（WAL 模式）
- systemd 服务：`flashshare-api.service`（端口 8000）

## 架构链路

```
用户浏览器 → Cloudflare（代理+CDN）→ Nginx（SSL+反代）→ Uvicorn:8000（FastAPI 应用）
                                                            ↓ HTTP（localhost）
                                                      Gokapi:53842（Go 文件存储）
                                                            ↓
                                                 ProcessPoolExecutor（Pillow 工具处理）
```

### 请求处理流程

```
flashshare (main.py)
├── BaseHTTPRequestHandler + ThreadingHTTPServer
├── 路由：do_GET / do_POST / do_HEAD
├── 中间件：CSRF（Origin 校验）→ Auth（Cookie → Session → User）
├── 认证：本地注册/登录 + X OAuth2 PKCE
└── 上传：multipart.py 解析 → gokapi.py 转发 → SQLite 记录
```

### 文件上传完整链路

```
浏览器 FormData(XHR)
  → Nginx proxy_request_buffering off
  → flashshare multipart.py 流式解析（SpooledTemporaryFile，8MB 阈值落盘）
  → security.py sniff_allowed（扩展名 + magic bytes 校验）
  → gokapi.py upload_to_gokapi（localhost HTTP 流式转发）
  → Gokapi 存盘
  → SQLite 写 uploads 表
  → 返回 publicId
```

## 文件清单与职责

| 文件 | 职责 | 改动风险 |
|------|------|----------|
| `main.py` | HTTP 路由 + 请求处理 + 上传 handler | 高 — 影响 ALL 请求 |
| `config.py` | 环境变量读取 + 文件类型白名单/黑名单 | 中 |
| `database.py` | SQLite 连接 + 建表 + 迁移 | 高 — Schema 变更需迁移 |
| `auth.py` | 本地账户 + X OAuth2 PKCE 全流程 | 高 |
| `gokapi.py` | Gokapi API 客户端（上传/下载/删除/列表） | 中 — 改了上传就挂 |
| `uploads.py` | 上传配额 + CRUD | 低 |
| `views.py` | 所有 HTML 页面渲染（7+ 页面） | 高 — 改一处影响多页 |
| `i18n.py` | 10 种语言翻译（106 keys × 10） | 低 |
| `security.py` | 密码 hash + 文件校验 + CSRF + Session | 高 |
| `multipart.py` | 流式 multipart/form-data 解析 | 极高 — 死循环 bug 已修复，勿动核心逻辑 |
| `tools.py` | 文件工具框架（view/peek/produce） | 低 |
| `tools_registry.py` | 工具注册 | 低 |
| `utils.py` | 格式化/转义/QR/ID 生成/图标 SVG 函数 | 中 |
| `static/app.js` | 前端全部逻辑 + i18n 客户端翻译表 | 高 |
| `static/style.css` | 全局样式（Design Token 体系） | 中 |
| `static/sw.js` | Service Worker（缓存+离线） | 高 — 版本不匹配会崩 |

## 页面清单

| 函数 | 页面 | 共享组件 |
|------|------|----------|
| `render_home` | 首页（上传+历史列表） | shared_header + shared_footer |
| `render_file_landing` | 文件详情/下载页 | shared_header + shared_footer |
| `render_login` | 登录页 | shared_header + shared_footer |
| `render_register` | 注册页 | shared_header + shared_footer |
| `render_profile` | 用户资料页 | shared_header + shared_footer |
| `render_not_found` | 404 页 | shared_header + shared_footer |
| `render_server_error` | 500 页 | shared_header + shared_footer |
| `render_offline` | 离线页 | shared_header（无 footer） |
| `render_doc` | 条款/隐私页 | shared_header + shared_footer |
| `render_file_missing` | 文件不存在页 | shared_header + shared_footer |

## 共享组件

### shared_header（全局头部）
- 所有页面**必须使用** `shared_header(user, lang, texts, share_url="")`
- 包含：品牌 logo + 用户头像下拉菜单 / 登录注册按钮 + 语言切换按钮
- **禁止任何页面自定义 header**

### shared_footer（全局底部）
- 所有页面使用 `shared_footer(lang)`
- 包含：版权 + LANG_MENU_JS（语言切换的内联脚本）

### 语言切换
- 两处渲染：`LANG_MENU_JS`（views.py 内联脚本，footer 用）+ `renderLanguageMenu()`（app.js）
- 这是历史分叉，修改语言按钮时**两处都要同步改**
- 当前状态：文字按钮（语言名称），不是图标

## 渲染链路

```
Python views.py → f-string 模板 → 注入 CSS/JS/i18n → HTML 输出
                   ↓
              app.js 在 <script> 标签内执行
              读取 window.__USER__ / window.__LIMITS__ / window.__I18N__
```

### 资源加载顺序
```
HTML → <style>CSS（内联）</style> → <script>window.__I18N__（JSON）</script> → <script>app.js（内联）</script>
```

### Service Worker
- `sw.js` 缓存 app.js + style.css + 核心页面
- 改 CSS/JS 后必须升级 SW 版本号（CACHE_NAME + views.py 中的 v= 参数）
- **不升级版本号 = 用户加载旧缓存 = 改动不生效**

## Design Token 体系（style.css）

```css
:root {
  /* 间距：4px 基数 */
  --s1: 4px;  --s2: 8px;  --s3: 12px;  --s4: 16px;  --s5: 24px;  --s6: 32px;

  /* 圆角 */
  --r-sm: 8px;  --r: 12px;  --r-lg: 16px;  --r-full: 9999px;

  /* 字号 */
  --text-xs: 12px;  --text-sm: 13px;  --text-base: 14px;
  --text-lg: 16px;  --text-xl: 20px;

  /* 阴影 */
  --sh-sm / --sh / --sh-lg / --sh-xl / --sh-accent

  /* 层级 */
  --z-dropdown: 30;  --z-modal: 50;

  /* 颜色（全部走变量，禁止硬编码 hex） */
  --bg / --ink / --muted / --soft / --line / --line-strong / --accent / --danger / --green
}
```

**规则：所有间距/圆角/字号/阴影必须使用 token 变量。格式图标颜色例外（固定分类色）。**

## 已知陷阱（必读）

### 1. Service Worker 缓存
- 改 `app.js` 或 `style.css` 后，必须同时：
  - `sw.js` 的 `CACHE_NAME` 版本号 +1
  - `views.py` 中 `sw.js?v=N` 的 N +1
  - 重启 flashshare
- **不这么做 = 改动对用户不可见**

### 2. Python views.py f-string 转义
- views.py 用 f-string 渲染 HTML，`{{ }}` 是字面 `{ }`，`{ }` 是变量插值
- 在 f-string 中嵌入 JS 的引号会冲突——**不要在 views.py 的 f-string 内写复杂的 JS innerHTML**
- LANG_MENU_JS 是预构建的字符串，用 `.format(lang=lang)` 而非 f-string

### 3. 语言切换两处定义
- `views.py` 的 `LANG_MENU_JS`（未登录/所有页面 footer）
- `app.js` 的 `renderLanguageMenu()`（首页加载后覆盖）
- **改一处不改另一处 = 不一致**

### 4. multipart.py 已修复死循环
- `_read_part` 函数的 `safe_end == 0` 分支已修复
- **不要重构这个文件**，除非完整理解 boundary 匹配逻辑

### 5. Gokapi 加密已关闭
- config.json Encryption Level = 0
- 不要重新开启，分块上传 API 不支持加密

### 6. 控件区域 .controls
- 有效期/下载下拉框用 `display: inline-flex` 水平排列
- 不要改回 `display: block` 或加 `min-width`
- 移动端 `@media` 断点内只覆盖 font-size

### 7. Gokapi chunk API
- `/api/chunk/add` 需要 header 形式参数（uuid/filename/filesize/index/total/offset）
- `/uploadChunk` 是 web UI 路径，需要 session cookie
- chunk 最小 5MB
- **未验证通过，不要在生产环境调用**

### 8. 语言包修改规范（i18n.py / i18n.json）— ⚠️ 高危

**背景**：i18n.py 是 58KB、1128 行的大文件，包含 10 语言 × 106 keys。一次全量重写极易出错（已导致事故）。

**事故记录**：曾用 `terminal` cat 读取 i18n.py（>50KB），输出被 50KB stdout 上限截断，写回后文件损坏（丢失 ~2500 字符），又导致 `language_from_header`/`language_from_code` 函数定义丢失，服务崩溃。

**强制规则**：

1. **禁止用 `terminal` cat / execute_code 读取大文件（>10KB）后全量写回**
   - `terminal` 有 50KB stdout 上限，大文件会被静默截断
   - `read_file` 有行数限制，`write_file` 会覆盖整个文件

2. **修改语言包用 `patch`（精准替换），不要用全量重写**
   ```python
   # ✅ 正确：用 patch 精准替换每个 subtitle 行
   patch(path="i18n.py", old_string='"subtitle": "旧文本"', new_string='"subtitle": "新文本"')

   # ❌ 错误：读取整个文件 → 正则替换 → 写回
   content = cat("i18n.py")  # 50KB 截断！
   write_file("i18n.py", new_content)  # 损坏！
   ```

3. **i18n.py 底部有三个函数，全量重建时必须全部保留**：
   - `server_text(lang)` — 返回语言字典
   - `language_from_code(code)` — 规范化语言代码（`zh-CN` → `zh`），不支持返回 None
   - `language_from_header(accept_language)` — 解析 Accept-Language，按出现顺序匹配，fallback `"en"`

4. **i18n.py（10 语言）和 i18n.json（7 语言）是两套独立数据**
   - i18n.py: 服务端渲染用，106 keys × 10 语言（en/zh/ja/ko/es/ar/de/ru/pt/fr）
   - i18n.json: 客户端 JS 用，59 keys × 7 语言（en/zh/ja/ko/es/ar/de）
   - 共享的 key（如 `subtitle`）在两个文件中**必须值一致**
   - 改了一个文件的某个 key，必须同步改另一个文件的同名 key

5. **修改后必须验证**：
   - `python3 -c "import ast; ast.parse(open('i18n.py').read())"` — 语法检查
   - `python3 -c "import sys; sys.path.insert(0,'.'); from i18n import SERVER_I18N, language_from_header, language_from_code"` — 导入检查
   - 比对 pyc 缓存（`__pycache__/i18n.cpython-*.pyc`）确认未丢失数据

### 9. .user-menu 需要 position: relative
- `.user-dropdown` 用 `position: absolute`，必须有一个 `position: relative` 的父元素
- `.user-menu` 必须设置 `position: relative`，否则下拉菜单会相对 viewport 定位（消失在屏幕外）
- `.lang-switch` 已有 `position: relative`，两者结构平行

### 10. LANG_MENU_JS 与 app.js 的 const 冲突
- `LANG_MENU_JS`（footer 内联脚本）和 `app.js` 都声明了 `SUPPORTED_LANGS`、`LANG_LABELS`、`langButton`、`langMenu`
- 两个 `<script>` 共享全局作用域，重复 `const` 声明会抛 `SyntaxError`
- LANG_MENU_JS 已改用 `var` + `typeof` 守卫，**不要改回 `const`**

### 11. 移动端 .auth-page 不要覆盖 max-width
- `.auth-page` 桌面端 `max-width: 380px` 已正确收口
- 移动端 `@media (max-width: 820px)` 内**不要写 `max-width: 100%`**，这会拆掉收口导致表单撑满
- 小屏只需调 padding，max-width 继承桌面端值即可

## 改动流程（强制）

```
1. 阅读本文件，理解目标文件的职责和依赖
2. 在本地修改
3. 本地验证：python3 main.py → curl 测试 → node --check app.js
4. 确认无误后部署
5. 升级 SW 版本号
6. 重启 flashshare
7. 线上验证完整流程：首页 → 登录 → 上传 → 下载 → 列表 → 删除
8. 更新本文件（如果改动影响了架构或链路）
```

**禁止：边改边上线、跳过验证、回滚时丢失正常改动。**

## 服务管理

```bash
sudo systemctl restart flashshare   # 改代码后重启
sudo systemctl restart gokapi       # 极少需要
sudo systemctl reload nginx         # 改 Nginx 配置后
sudo journalctl -u flashshare -n 20 # 看日志
```

## 环境变量

配置文件：`/etc/flashshare.env`

```
FLASHSHARE_ADDR=127.0.0.1:53843
FLASHSHARE_BASE_URL=https://518.plus
FLASHSHARE_DB=/var/lib/flashshare/flashshare.sqlite
FLASHSHARE_MAX_UPLOAD_MB=50
FLASHSHARE_DAILY_UPLOADS=100
FLASHSHARE_DAILY_MB=1024
FLASHSHARE_X_CLIENT_ID=（已配置）
FLASHSHARE_X_CLIENT_SECRET=（已配置）
FLASHSHARE_GOKAPI_URL=http://127.0.0.1:53842
FLASHSHARE_GOKAPI_KEY=（已配置）
FLASHSHARE_SESSION_SECRET=（已配置）
```

## Nginx 配置

- 路径：`/etc/nginx/sites-available/518.plus`
- `proxy_request_buffering off`（上传流式）
- `client_max_body_size 60m`
- Gokapi 下载路由：`/d/` `/dh/` `= /d`

## 网络优化（已配置）

```bash
# /etc/sysctl.d/99-tcp-optimization.conf
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
```

## 第二台机器（备用）

- IP：141.147.185.123（公网）/ 10.0.0.246（内网）
- 同子网 10.0.0.0/24，内网 <1ms 延迟
- SSH 可达，HTTP 80/443 运行中
- 用途：未定（上传节点 / 备用 / 负载均衡）
