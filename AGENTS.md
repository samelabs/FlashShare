# AGENTS.md — 518.PLUS FlashShare AI 开发规范

> 所有 AI agent 开发前**必须完整阅读本文件**。违反规范导致的 bug 由 agent 全责。
> 最后更新：2026-08-05

---

## 项目概况

FlashShare 是临时文件分享 + 工具处理平台，部署在 Oracle Cloud 1GB VPS（东京）。

- **域名**：518.plus（Cloudflare → Nginx → Uvicorn:8000）
- **技术栈**：FastAPI + Jinja2 + Uvicorn + Pillow 10.2 + gokapi + SQLite + Nginx
- **Python**：系统 `/usr/bin/python3`（3.12.3），**不用 venv**
- **服务**：`flashshare-api.service`（systemd，`sudo systemctl restart flashshare-api.service`）
- **用户**：`ubuntu`（不是 vibazzdrop）
- **Git**：`/opt/flashshare/.git`，分支 `main`

---

## 架构

```
用户 → Cloudflare → Nginx → Uvicorn:8000 (FastAPI)
                              ├── app/routes/pages.py     页面渲染（Jinja2）
                              ├── app/routes/api.py       /api/v1/* JSON API
                              ├── app/routes/auth_route.py /auth/* 认证
                              ├── app/routes/proxy.py     /f/{pid} 详情+下载
                              └── app/worker.py           ProcessPoolExecutor 工具处理
                                    ↓
                              gokapi:53842（文件存储）
```

### 目录结构

```
/opt/flashshare/
├── app/                    FastAPI 应用（唯一运行入口）
│   ├── main.py             FastAPI 实例 + 中间件 + Jinja2(autoescape=False)
│   ├── worker.py           子进程工具执行
│   ├── worker_node.py      远程 Worker Node（未部署）
│   ├── models.py           Pydantic 请求模型
│   ├── routes/             api.py, auth_route.py, pages.py, proxy.py
│   └── templates/          Jinja2 模板（base, home, detail, auth/*, partials/*）
├── core/                   核心模块（14 个，零 I/O 依赖）
│   ├── config.py           配置常量（APP_NAME, ALLOWED_EXT, ...）
│   ├── tools.py            工具注册框架 + VALID_ICONS
│   ├── tools_registry.py   share + privacy 工具注册
│   ├── tools_beautify.py   截图美化（9 档预设）
│   ├── tools_compress.py   图片压缩（4 档目标大小）
│   ├── tools_ico.py        ICO 图标生成（4 档尺寸）
│   ├── icons.py            SVG 图标单一数据源（35 个图标）
│   ├── i18n.py             服务端语言包（10 语言）
│   ├── database.py         SQLite 连接
│   ├── auth.py             认证逻辑
│   ├── gokapi.py           gokapi API 封装
│   ├── storage.py          文件存储适配
│   ├── uploads.py          上传记录管理
│   ├── security.py         安全检查（magic bytes 嗅探）
│   └── utils.py            工具函数（format_ts, file_format_icon, ...）
├── static/                 前端资源
│   ├── app.js              前端逻辑（~800 行）
│   ├── style.css           样式（~530 行）
│   ├── sw.js               Service Worker
│   ├── i18n.json           客户端语言包（7 语言）
│   └── manifest.json       PWA Manifest
├── docs/                   设计文档
├── run.sh                  systemd 启动脚本
└── AGENTS.md               本文件
```

**禁止**修改根目录的 `.py` 副本（`config.py`, `i18n.py` 等）——它们是 `core/` 的同步副本，实际运行加载的是 `core/`。

---

## ⛔ 反越界红线（RECURRING ISSUES）

以下是开发过程中反复出现的越界问题。**每次修改前必须检查这些红线**。

### 1. CSS 选择器作用域

**问题**：改首页样式时用了全局选择器（如 `h1 {}`），影响登录/注册/详情页。

**规则**：
- 首页专用样式用 `.intro h1 {}`、`.workspace .xxx {}` 限定作用域
- 认证页用 `.auth-page .xxx {}`
- 详情页用 `.file-page .xxx {}`
- **禁止**用裸 `h1 {}`、`section {}` 等全局选择器做页面特有样式

### 2. data-i18n 遗漏

**问题**：服务端渲染的文案（`{{ texts.xxx }}`）没有加 `data-i18n` 属性，切换语言时不刷新。

**规则**：
- **所有**动态文案的 HTML 元素必须加 `data-i18n="key"` 属性
- `applyI18n()` 遍历 `[data-i18n]` 更新文本——没有这个属性的元素被跳过
- JS 动态生成的文案用 `t("key")` 并在 `applyI18n()` 中调用重建函数

### 3. i18n 双系统同步

**问题**：服务端 `i18n.py`（10 语言，snake_case key）和客户端 `i18n.json`（7 语言，camelCase key）不同步。

**规则**：
- 新增文案时**两个文件都要改**
- 服务端 key：`download_file`（下划线）
- 客户端 key：`downloadFile`（驼峰）
- **禁止**在客户端 `t()` 里用服务端 key

### 4. preset ID 全局唯一

**问题**：ICO 工具的 `standard` preset 覆盖了 privacy 工具的 `standard` preset。

**规则**：
- preset ID **必须全局唯一**——加工具前缀（`ico_standard`, `ico_web`）
- 新增 preset 前检查所有已有工具的 preset ID 列表

### 5. icons.py 单一数据源

**问题**：在 app.js、views.py、utils.py 里手绘 SVG path，和 icons.py 不一致。

**规则**：
- 所有 SVG path **只存在于 `icons.py` 的 `_ICONS` 字典**
- Python 端：`icon_svg("name")` / `icon_path("name")`
- JS 端：`window.__ICONS__["name"]` / `svgIcon("name")`
- `utils.py` 的 `FORMAT_ICONS` 从 `icons.py` 取 path（`from icons import _ICONS`）
- **禁止**在其他任何文件里出现 `<path d="...">` 字面量

### 6. CSS/JS 内联加载机制

**问题**：修改了 style.css 或 app.js 但页面不更新。

**规则**：
- CSS 和 JS 在 `pages.py` 启动时读入内存（`CSS = open(...).read()`）
- **修改 style.css / app.js 后必须 `sudo systemctl restart flashshare-api.service`**
- SW 版本号同步更新：`sw.js` 的 `CACHE_NAME` + `home.html` 的 `sw.js?v=N`

### 7. Jinja2 autoescape

**问题**：Starlette 的 `Jinja2Templates` 强制 `autoescape=True`，导致 SVG/JSON 被转义。

**规则**：
- `app/main.py` 中 `templates.env.autoescape = False`（在 `Jinja2Templates()` 之后覆盖）
- 模板中用 `{{ xxx|safe }}` 输出 HTML/SVG
- **修改 main.py 后必须重启 uvicorn**

### 8. sqlite3.Row 没有 .get()

**问题**：`upload.get("url_hotlink")` 报错——sqlite3.Row 没有 `.get()` 方法。

**规则**：
- 用 `upload["key"] if "key" in upload.keys() else ""` 替代 `upload.get("key")`
- **禁止**对 `sqlite3.Row` 使用 `.get()`

### 9. gokapi 下载 URL 格式

**问题**：gokapi 的 `/d?id=xxx` 返回 HTML 页面不是文件流；hotlink URL 有两种格式。

**规则**：
- `/h/{id}` → 旧格式 hotlink（Nginx `/dh/` 代理到 gokapi `/h/`）
- `/downloadFile?id={id}` → 新格式（Nginx `/downloadFile` 代理）
- 302 重定向时**保留 query string**（`urlparse` 的 `.path` + `.query`）
- **禁止**把 gokapi 的 HTML 下载页当文件 blob 使用

### 10. Web Share API AbortError

**问题**：用户取消 `navigator.share()` 触发下载 fallback。

**规则**：
- `AbortError`（用户取消）返回 `true`（不触发 fallback）
- 只有 fetch 失败或 `navigator.share` 不支持才返回 `false`
- **不要用 `navigator.canShare` 做前置检查**——iOS Safari 支持 `share({files})` 但不支持 `canShare`，会导致误判 fallback 导航阻塞 PWA

### 11. 语言包精简原则

**问题**：每次加功能就新增 i18n key，导致语言包膨胀、翻译维护负担加重。

**规则**：
- 新功能优先用**视觉和交互**表达，而非文案——图标、数字、动画、布局变化
- 避免增加**描述性文案**（如"正在处理"、"请稍候"），用 spinner/进度条/视觉反馈替代
- 能复用已有 key 的不新增——`uploading`、`failed`、`delete` 等通用 key 跨场景复用
- 新增 key 前**必须确认无法用视觉手段表达**
- 纯数字/符号的 preset label（如 `16x16`、`≤ 2MB`）**不占 i18n key**——从注册 label 字典取

### 12. 全局事件绑定唯一性

**问题**：同一 DOM 元素在多个 `<script>` 块里注册了相同事件的监听器（如用户下拉菜单、语言切换），导致行为互相抵消。

**规则**：
- 共享交互（用户下拉菜单、语言切换）统一在 `footer.html` 的 `<script>` 里绑定——所有页面都加载 footer
- 用 `window.__FLAG` 布尔标记防止重复绑定（如 `window.__LANG_INIT__`）
- **不要**在 app.js 和 footer.html 里**各绑一次**同一事件
- **不要**用 `typeof VAR !== "undefined"` 防重复——`var` 提升会使检查失效

### 13. 脚本块独立性

**问题**：一个 `<script>` 块里的 SyntaxError 或运行时错误会杀死该块后续代码，但开发者误以为后续独立 `<script>` 块也不执行。

**规则**：
- 多个 `<script>` 块是**独立执行**的——A 块报错不影响 B 块
- 但 A 块里的错误会杀死 A 块内**该错误之后的所有代码**
- 共享交互代码放在**可靠的独立 `<script>` 块**（footer.html）里，不依赖 app.js 是否成功执行
- 排查"某功能在某些页面不工作"时，首先检查是否有 `const`/`var` 重复声明导致的 SyntaxError

---

## 工具系统规范

详见 `docs/DESIGN-tool-flow.md`（387 行完整规范）。关键要点：

### 新增工具 checklist

1. `core/tools_{name}.py`：实现 handler（纯 Pillow，零 I/O）
2. `core/tools.py` `VALID_ICONS`：加图标名（如已存在则跳过）
3. `core/icons.py` `_ICONS`：加 SVG path（唯一数据源）
4. `app/main.py`：`import tools_{name}`
5. `core/i18n.py`（10 语言）+ `static/i18n.json`（7 语言）：加 `tool_id`、`tool_id_desc`、各 preset_id
6. `static/app.js` `ACCEPT_TAGS`：加新 MIME 映射（如需要）
7. `static/sw.js` `CACHE_NAME` +1，`home.html` `sw.js?v=N` +1
8. 重启服务：`sudo systemctl restart flashshare-api.service`

### 工具属性表

| tool_id | icon | presets | accept | 特殊 |
|---|---|---|---|---|
| share | upload | 无 | None（全部） | 兜底默认，点击不弹浮层 |
| privacy | shield | light/standard/strong/max | 6 种图片 | — |
| beautify | wand | 9 档背景预设 | 6 种图片 | — |
| compress | compress | 1mb/2mb/5mb/10mb | 6 种图片 | — |
| ico | convert | ico_standard/ico_web/ico_app/ico_single | 7 种图片+ico | — |

### API 路径规范

所有 JSON API 在 `/api/v1/*` 下：
- `GET /api/v1/tools` — 工具列表
- `GET /api/v1/tools/{pid}` — 文件可用工具
- `POST /api/v1/tools/execute` — 执行工具
- `GET /api/v1/uploads` — 上传列表
- `POST /api/v1/uploads` — 上传文件
- `DELETE /api/v1/uploads/{pid}` — 删除文件
- `PATCH /api/v1/uploads/{pid}` — 重命名文件
- `GET /api/v1/me` — 当前用户

---

## 开发流程

### 修改前

1. **读完本文件**——特别是反越界红线
2. **确认作用域**——你要改的是哪个页面/组件/API？影响范围多大？
3. **搜索全局引用**——改一个 key/class/ID 前，grep 全项目确认没有其他地方依赖它

### 修改中

1. **一次只改一个功能**——不要顺手改无关代码
2. **改 CSS 检查选择器作用域**——是否会影响其他页面
3. **改文案加 `data-i18n`**——确保语言切换能刷新
4. **改 i18n 同步两个文件**——`i18n.py` + `i18n.json`
5. **改 CSS/JS 后重启服务**

### 修改后

1. `node --check static/app.js`（JS 语法验证）
2. `/usr/bin/python3 -c "import ast; ast.parse(open('file.py').read())"`（Python 语法验证）
3. `sudo systemctl restart flashshare-api.service`
4. `curl` 验证页面渲染 + API 响应
5. `git add -A && git commit -m "描述"`
6. **更新 `devlog.md`**——根目录，每次改动一条摘要（日期 | commit | 摘要），新条目加在最上面

### 常用命令

```bash
# 重启服务（改代码后必须执行）
sudo systemctl restart flashshare-api.service

# 查看服务状态
systemctl status flashshare-api.service

# 查看日志
sudo journalctl -u flashshare-api --no-pager -n 20

# 语法检查
node --check static/app.js
python3 -c "import ast; ast.parse(open('core/file.py').read())"

# Nginx 重载
sudo nginx -t && sudo nginx -s reload

# Git 提交
git add -A && git commit -m "feat/fix/refactor: 描述"
```

---

## 关键约束

- **1GB VPS**：工具处理峰值内存 < 100MB，图片长边 ≤ 2048px
- **不引入 numpy**（用户明确拒绝）
- **不用 venv**：系统 Python `/usr/bin/python3`
- **Pillow 是唯一外部依赖**（通过 `apt install python3-pil` 安装）
- **用户极度不容忍回归**——"不要越界"意味着任何破坏现有功能的改动都不可接受
- **统一改 ALL 相关页面/元素**——改一个按钮样式就改所有同类按钮
- **重构是为了规范**——不要简化、不打补丁、不制造分叉
