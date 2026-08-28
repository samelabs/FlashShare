# AGENTS.md — 518.PLUS FlashShare AI 开发规范

> 所有 AI agent 开发前**必须完整阅读本文件**。违反规范导致的 bug 由 agent 全责。
> 最后更新：2026-08-28（全量重写对齐现实：PostgreSQL / 单源 i18n / 前端分层 / 服务器直开发）

---

## 项目概况

FlashShare（518.PLUS）是开放工具链平台（输入暂存+能力路由+产出落点）+ C2C 市场（双池账本、平台抽成、提现闭环）。

- **域名**：518.plus（Cloudflare → Nginx → Uvicorn:8000）
- **技术栈**：FastAPI + Jinja2 + Uvicorn + Pillow 10.2 + gokapi + **PostgreSQL** + Nginx
- **数据库**：PostgreSQL（`/etc/flashshare.env` 的 `DATABASE_URL`，psycopg2 连 `127.0.0.1:5432/flashshare`）。`/var/lib/flashshare/flashshare.sqlite` 是废弃开发残留，**勿改勿验**
- **Python**：系统 `/usr/bin/python3`（3.12），**不用 venv**
- **服务**：`flashshare-api.service`（`sudo systemctl restart flashshare-api.service`）
- **用户**：`ubuntu`
- **Git**：`/opt/flashshare/.git`，分支 `main`，可直接 push origin
- **开发模式**：**服务器直开发** → 测试 → `git push origin main`。（本地 macOS/docker 三端流程已废弃，相关文件不存在）

## 商业与产品语义（不可漂移）

- 站名 **518.PLUS**（全部 SEO/meta/title/manifest），hero slogan = Flash Share
- **供给侧单线**：只收 HTTP API 契约（开发者写一个 https 端点接入；518 做 API→MCP 适配+计量分账+分发）
- **MCP 是北向主入口**（顶栏常驻"MCP 接入"）；MCP 页技术说明**硬编码不进语言包**
- **双池账本**：消费池（签到/充值/消费/退款，`spendable_balance` 派生）与收益池（provider_earn，仅可提现）物理隔离；ledger 唯一事实源
- **活动=满足机制参与**：注册赠送已剥离（耦合进注册逻辑的伪活动），现存仅 daily_login（activities 表驱动，运营可改）
- **后台管理员**：id=2 ainotebook（PG users.status='samelabs'），门禁实时查库

## 架构

```
用户 → Cloudflare → Nginx → Uvicorn:8000 (FastAPI)
  ├── app/main.py            实例+中间件+Jinja2(autoescape=False)+detect_lang
  ├── app/routes/pages.py    页面渲染（版本戳 _CSS_VERSION/_JS_VERSION/_I18N_VERSION）
  ├── app/routes/api.py      /api/v1/* + /api/tokens|usertools|earnings（ProcessPool 在此）
  ├── app/routes/auth_route.py  /auth/*
  ├── app/routes/proxy.py    /f/{pid} 详情+下载
  ├── app/routes/earn.py     收益页+API
  ├── app/routes/mcp.py      MCP 协议端点（streamable-http）
  ├── core/                  业务核心（i18n/billing/earnings/catalog/remote/storage/...）
  └── static/pages/*.js      前端逻辑层（见"前端分层契约"）
```

## 前端分层契约（2026-08-27 治理，违者必炸）

**结构**：
- 模板只留**数据注入** inline（`window.__USER__`/`__LANG`/`__PAGE__`），其余逻辑全部 `static/pages/*.js`
- base.html 加载链：注入块 → icons.js → base-engine.js → i18n.js → `{% block script_head %}` → `{% block script %}` → `{% block script_tail %}` → footer.js
- block script/script_head/script_tail **独立于任何 `<script>` 体外**（曾因嵌进 script 体导致首页乱码+工具点不了两起事故）
- pico CSS = **conditional 变体**（元素规则全部 scope 在 `.pico` 容器内；classless 会全局重绘裸 button/a——用过一次把全站按钮打崩）

**铁律**：
- JS 文件**顶层禁止 `function t`/`function applyI18n` 等引擎同名声明**——会挂 window 遮蔽引擎导致无限递归（Maximum call stack 事故）。别名一律 `const t = (...) => window.t(...)`
- 版本戳：`?v=` = md5(内容+mtime)，**js_version 与 css_version 独立**——app.js 借 css_version 戳曾让边缘永远吐旧 bug 版
- 改任何 static 文件后**必须重启服务**（pages.py 启动时读入内存算戳）
- 全部 pages/*.js 过 `node --check`（feguard.py 门禁：22 文件+模板内联≤500 字符+零 jinja 泄漏）

## i18n 单源规范

- **唯一数据源**：`core/i18n.py` 的 `SERVER_I18N`（10 语言，snake_case）
- 生成 `static/i18n.js`：`/usr/bin/python3 tools/gen_i18n.py（仓库内，防 /tmp 丢失）`（AUTO-GENERATED，勿手编；**该脚本在 /tmp，重启丢失后从 git 历史 tests/ 或重建**）
- `static/i18n.json` **已删除**（双系统同步章节作废）
- 错误码族：执行码自带 `err_` 前缀；提交/CRUD 裸码走 `_errm()` 统一解析（common.js 单份：`mytools_err_{code}` → `err_{code}` → 原文回退）
- **死键删除前必须六引用形态全查**：`{{texts.x}}`/`texts['x']`/`data-i18n`/`window.t("x")`/裸 `t('x')`/`_msg/_tx('x')` + 动态族（工具 id、preset 档名、ledger_ 前缀、后端错误码）——两次误删事故的教训
- 语言切换：base-engine.js 引擎（applyI18n 逐元素 try/catch）；账户页无 `__afterI18n` 时切换=reload

## ⛔ 反越界红线（RECURRING ISSUES）

1. **CSS 选择器作用域**：页面样式必须带作用域前缀（`.workspace .xxx`/`.auth-page .xxx`），禁裸 `h1{}`
2. **data-i18n 遗漏**：所有动态文案 SSR 用 `{{ texts.x }}` + `data-i18n="x"`（**禁硬编码英文占位**——曾导致中文下 Upload）
3. **preset ID 全局唯一**：加工具前缀（`ico_standard`）
4. **icons.py 单一数据源**：SVG path 只在 `core/icons.py` `_ICONS`；JS 端 `window.__ICONS__`（由 static/pages/icons.js 提供）
5. **Jinja2 autoescape=False**：HTML/SVG 输出用 `|safe`；改 main.py 后重启
6. **Jinja2 dict 下标**：`texts['copy']` 而非 `texts.copy`（命中 dict.copy() 方法）
7. **gokapi 下载 URL**：`/h/{id}` 旧格式（Nginx /dh/ 代理）、`/downloadFile?id=` 新格式；302 保留 query；禁把 HTML 下载页当 blob
8. **Web Share AbortError**：用户取消返回 true 不触发 fallback；禁用 canShare 前置检查（iOS 误判）
9. **语言包精简**：优先视觉表达；复用通用 key；纯数字 preset label 不占 key
10. **删除代码块警惕隐性保护**：曾把 cost=0 守卫随 lazy 块删掉导致 consume(0) 500；删完必跑全量回归
11. **CSS 禁区间替换**：只动目标块+前后探针；删后必跑 selector 残骸扫描（双前缀）+逐块括号栈校验（总平衡=0 不够，错位闭合会假平衡）
12. **生产代码禁边写边猜**：改前完整读现有逻辑→定案→一次写对

## 验收体系（改完必跑）

```bash
sudo sh tests/acceptance/ui_accept.sh        # UI 基线
sudo sh tests/acceptance/v2_accept.sh        # 第二批
sudo sh tests/e2e_metering.sh                # 计量 C1+C2（留脏会改 doc2pdf cost，失败必清）
sudo sh tests/e2e_mcp.sh                     # MCP 协议
sudo sh tests/acceptance/p3_e2e.sh           # C2C
sudo sh tests/acceptance/earn_e2e.sh         # 收益闭环
sudo sh tests/acceptance/act_accept.sh       # 活动
/usr/bin/python3 tests/acceptance/feguard.py # 前端结构门禁
sudo sh -c 'set -a; . /etc/flashshare.env; set +a; /usr/bin/python3 tests/acceptance/full_audit.py'  # 六维审计
sudo sh -c 'set -a; . /etc/flashshare.env; set +a; /usr/bin/python3 tests/acceptance/visual_sweep.py' # pico越界+表面锚
```

探针纪律：断言用**结构性锚**（类名/i18n 键/id），禁中文文案锚（默认 en SSR 会假失败）；E2E 失败必须清残留再跑。

## 新增工具 checklist

1. `core/tools_{name}.py`：handler（纯 Pillow，零 I/O）
2. `core/catalog.py` 平台工具注册表（id/label/presets/accept/pricing）
3. `core/i18n.py` ×10 语：`tool_id`、`tool_id_desc`、各 preset_id → 重生成 i18n.js
4. `app/main.py`：`import tools_{name}`
5. 重启服务

工具**无图标无分类**（用户四次指令）：提交面只留业务字段（名称/ID/执行类/URL/超时/输出/鉴权/定价）。

## 关键约束

- **用户极度不容忍回归**：任何破坏现有功能的改动不可接受；一次只改一个功能，禁顺手改
- **Pillow 是唯一外部 Python 依赖**；不引入 numpy
- 改动前 grep 全局引用；Python 改完 `ast.parse` 验证；JS 改完 `node --check`
- 客户端崩溃会上报 `/api/client-error`（base-engine.js `__errTap`）→ 应用日志 `CLIENT-CRASH`——排查浏览器侧故障先看这个
