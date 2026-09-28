# DEVELOPMENT.md

长期开发事实：架构总览、环境搭建、运行与测试命令。 secrets/values 不在本文件。

## 架构总览

```
用户 → Cloudflare → Nginx → Uvicorn:8000 (FastAPI, 单 worker)
  ├── app/main.py            实例 + 中间件 (auth/quota/语言检测/recovery hook)
  ├── app/routes/            pages / auth / api(browser-internal) / proxy(/f) / mcp / earn / topup / samelabs / usertools
  ├── core/execution/        invocation(五键 parser) / dispatch(四面单入口) / gate(计量+settlement) / orchestrator
  ├── core/governance/       tool_supply(供给单点) / catalog / pricing / formats(格式族单源) / platform / delivery
  ├── core/economy/          billing(双池) / earnings / reconcile / subscription / creem
  ├── core/identity/         auth / sessions / apikeys
  ├── core/sharing/          uploads / housekeeping
  ├── core/repo/             SQL 数据访问层
  ├── core/gokapi.py         Gokapi 集成 (HTTP API only)
  └── static/pages/*.js      前端逻辑层 (模板只做数据注入)
```

四主链（Tool / Sharing / Identity / Economy）与冻结契约见 AGENTS.md。

## Repo layout

- `app/` FastAPI 应用（routes + main + web_auth）
- `core/` 业务域（execution / governance / economy / identity / sharing / repo）
- `static/` 前端（app.js + pages/*.js + css）
- `tools/` 代码生成（gen_i18n.py）
- `tests/canonical/` 正式回归测试（可移植，见下）
- `tests/ci/` CI 边界契约测试
- `tests/support/` CI test service；不得复制 518 业务逻辑
- `docs/plan/` 尚未执行的 release migration 资产（执行后移除）
- `scripts/` production backup / restore-proof 运维脚本 + systemd units/timers

## Prerequisites

- Python 3.12（生产 venv：`venv/`）
- Node.js（`node --check` JS 语法门禁）
- PostgreSQL（业务库 + isolated test DB）
- 外部 runtime dependencies：**Gokapi**（文件存储）、**Nginx**（反代/静态）、Cloudflare（DNS/缓存）

## Environment variables（只列名字，不列值）

必填（缺失时启动直接报错并指名变量；无默认值）：

- `DATABASE_URL` — PostgreSQL DSN（`postgresql+psycopg://…`）
- `FLASHSHARE_SESSION_SECRET` — session/token 签名密钥
- `FLASHSHARE_BASE_URL` — 对外基地址（og 链接/MCP endpoint/分享链接）
- `FLASHSHARE_GOKAPI_URL` / `FLASHSHARE_GOKAPI_KEY` — Gokapi API

可选（未配置时对应能力关闭或取默认值）：

- 限额：`FLASHSHARE_MAX_UPLOAD_MB`（单文件，默认 50）、`FLASHSHARE_MAX_BATCH_MB`（单次合计，默认 100）；
  `FLASHSHARE_DAILY_UPLOADS` / `FLASHSHARE_DAILY_MB`（仅用于**首次**创建 free 档的 24h 配额，默认 100 / 1024；
  free 行建成后以 plans 表为准，改 env 不覆盖）
- 风控：`FLASHSHARE_REGISTER_LIMIT_PER_HOUR`（注册每 IP 每小时，默认 30）、
  `FLASHSHARE_LOGIN_LIMIT_PER_15MIN`（登录失败每 IP 每 15 分钟，默认 50）；CI 等隔离栈调高
- `TOOL_VAULT_KEY` — secrets_vault 加密密钥（Fernet）：工具鉴权凭据与 API key 加密副本共用；
  未配置时创建 API key 直接失败（不降级为只显示一次）
- `FLASHSHARE_MCP_ALLOWED_ORIGINS` — 逗号分隔的额外浏览器 origin（`scheme://host[:port]`）。
  `/mcp` 对带 `Origin` 头的请求只放行 `FLASHSHARE_BASE_URL` 的 origin 与此列表，其余 403；
  无 `Origin` 头的非浏览器客户端不受影响
- X 登录：`FLASHSHARE_X_CLIENT_ID` + `FLASHSHARE_X_CLIENT_SECRET`（两者都配才启用；只配一半时
  启动日志 WARNING，登录入口隐藏）。X Developer Portal 设置：OAuth 2.0、类型 Web App
  （confidential client）、权限 `tweet.read` `users.read`、Callback URI
  `${FLASHSHARE_BASE_URL}/auth/x/callback`（逐字一致）。运营后台总览「集成」卡显示是否已配置与回调地址
- 支付（Creem）：`CREEM_API_KEY`、`CREEM_WEBHOOK_SECRET`、`CREEM_CREDITS_PRODUCT_ID`、
  `CREEM_CREDITS_CURRENCY`；`CREEM_TEST_MODE=1` 走测试环境。三项（key / product / currency）齐全才开放积分充值。
  会员档与积分包在运营后台配置；公开定价页 `/pricing` 只列出可售项（会员档需定价 + 绑定 Creem product + 权益齐全），
  都不可售时显示「尚未开放」。退款请求经 `FLASHSHARE_PUBLIC_SUPPORT_CHANNEL` 受理（服务条款第 8 节），在 Creem 后台执行退款
- `FLASHSHARE_PUBLIC_SUPPORT_CHANNEL` — 对外支持渠道（页面展示）
- `FLASHSHARE_SOFFICE` — LibreOffice 可执行路径（文档转 PDF，默认 `soffice`）
- `FLASHSHARE_LOG_DIR` — 日志目录（默认 `/var/log/flashshare`）
- `FLASHSHARE_BACKUP_REMOTE` — 备份脚本的 off-host rclone 目的地（见下文 backup）

生产值在服务器 env 文件（不进仓库）；测试用 isolated DB + 任意测试 secret。

## Local / test stack（isolated — 全部必须显式）

canonical integration tests **fail closed**：未显式提供 isolated port 与
TEST_DB_URL 时直接退出（不发任何请求）。两种合法方式：

**A. local isolated stack（与 CI 同构，日常开发/验收用）**

唯一本地入口是 `tools/ci_local.sh`：自建全新库 + fake Gokapi(:53898) + app(:8100)，
按给定顺序跑套件、输出一张退出码表、结束后自动停服务并 DROP 库。不要另写驱动脚本。

```bash
PG_ADMIN_URL=postgresql://<user>@127.0.0.1:5432/postgres \
  tools/ci_local.sh tests/canonical/<affected>.py ...     # 受影响套件
tools/ci_local.sh --browser                               # 不带参数 = ci.yml 全部 + 两道浏览器门禁
```

浏览器门禁需 `PLAYWRIGHT_CORE_PATH`（playwright-core 1.55.0）；本机无 Playwright Chromium 时设
`CHROMIUM_PATH` 指向系统 Chrome。下面是它内部使用的环境（手工排障时参考）：

- isolated PostgreSQL 16 库（每个分支/批次独立库，验收后 DROP）
- `tests/support/fake_gokapi.py`（Gokapi HTTP boundary test service）
- isolated app 端口（≠ 8000；8000 是生产端口，测试显式拒绝）

```bash
export PYTHONDONTWRITEBYTECODE=1
export DATABASE_URL=postgresql+psycopg://<user>:<pw>@127.0.0.1:<pg-port>/<db>
export TEST_DB_URL=postgresql://<user>:<pw>@127.0.0.1:<pg-port>/<db>
export FLASHSHARE_SESSION_SECRET=<test-secret> FLASHSHARE_GOKAPI_KEY=<test-key>
export FLASHSHARE_GOKAPI_URL=http://127.0.0.1:<gk-port>
export FLASHSHARE_BASE_URL=http://127.0.0.1:<app-port> FLASHSHARE_TEST_PORT=<app-port>
export FLASHSHARE_LOG_DIR=/tmp/flashshare-test-log
venv/bin/python tests/support/fake_gokapi.py --host 127.0.0.1 --port <gk-port> --key "$FLASHSHARE_GOKAPI_KEY" &
venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port <app-port> --workers 1 &
```

- app startup 的 `init_db()` 负责建表与 seed：重置测试库后必须先（重）启 app，再跑测试。
- 本地运行前清掉 `__pycache__`：部分 semantic guard 用 grep 扫源码，`.pyc` 会造成误报（CI 为干净 checkout，无此问题）。
- 本地 DNS 若为代理 fake-ip（198.18.0.0/15），依赖公网解析的 SSRF 正向用例会被守卫拒绝；这是环境噪音，不是代码回归。

**B. GitHub Actions CI v1**：真实 PostgreSQL 16 + 真实 FlashShare app +
`tests/support/fake_gokapi.py` 提供 Gokapi HTTP boundary test service，然后运行
`tests/canonical/`。workflow 在 `.github/workflows/ci.yml`。

CI v1 测的是 518.PLUS 自己的 invariant，不是 Gokapi compatibility。fake Gokapi
只实现 `core/gokapi.py` 当前消费的外部 HTTP contract（add/list/get/download/delete），
不实现 ownership/quota/tool/economy/lifecycle，不读 Gokapi 私有 sqlite/schema/content。
生产 Gokapi、生产 PostgreSQL、生产 secrets 和 deployment action 均不进入 CI。

真实 Gokapi compatibility smoke 可以后作为独立低频测试；不要把 web wizard 或
生产 Gokapi 接到普通 commit CI。

### 真实 Gokapi（仅 compatibility proof）

日常开发与 CI 一律使用 fake Gokapi。需要验证真实 Gokapi v2.2.4 兼容性（release gate）
时，使用 disposable 实例：首次启动后在其 admin UI 完成初始设置并生成带 DOWNLOAD
permission 的 API key（无固定默认 key），注入 `FLASHSHARE_GOKAPI_URL`/`FLASHSHARE_GOKAPI_KEY`。

**禁止**对 production DB 跑分支代码的 `init_db`；禁止测试读写 production Gokapi。

## Run / test commands

```bash
git diff --check
python -m compileall -q app core                     # 语法
git ls-files '*.js' | xargs -n1 node --check         # JS 语法
# canonical 套件：按 .github/workflows/ci.yml 中的顺序逐个运行（环境变量见上，无默认值）
python tests/canonical/<suite>.py
python tests/ci/<contract>.py ; node tests/ci/<contract>.js
```

测试账号/数据由各套件自建，每次运行唯一命名并自行清理；测试库最终 DROP。

## 设计系统（frontend）

- `static/css/tokens.css` — 设计令牌唯一来源（颜色/字阶/间距/圆角/阴影/断点），语义命名留有 dark-mode 覆盖位。
- `static/css/components.css` — 共享组件唯一样式（btn/card/input/select/check/switch/file-row/rs-* 结果项/tag/hint/empty/tbl/page-head/crumbs/modal/toast/ti-* 输入组件）。新组件加在这里。
- `static/518.css` / `static/samelabs.css` — 仅页面布局，选择器必须带页面作用域。
- 加载顺序 tokens → 518.css(页面) → components.css（组件最终生效，页面不得覆盖组件视觉）；组件改动只发生在 components.css；CSS 版本戳由 web_ctx 对三个文件联合摘要。

## Branch workflow

角色与边界见 AGENTS.md A 节。

1. `git fetch origin`；从 PM 指定的基线（默认 `origin/main`）建分支。
2. 改动 + diff 自审 + 门禁：compile / node --check / `tools/ci_local.sh` 跑受影响套件
   （UI 改动加 `--browser`）。全量验证由 GitHub CI 负责，执行 agent 不跑本地全量，
   不用 sleep 轮询后台任务——套件前台运行，直接读退出码表。
3. push 该 non-main 分支 → 报告 → **STOP**。
4. PM 按精确 SHA 验收，开 PR，等 GitHub CI（fast-gate / canonical-integration /
   browser-runtime）全绿后合入 `main`；production deploy 需 owner 授权。

## Production deploy 概要

- 生产 = 服务器 checkout `main` + systemd `flashshare-api`。
- 常规部署 = stop unit → 显式执行 release migration SQL（如有，见 `docs/plan/`）→ ff-only 同步 main → start unit → healthz → 启动日志（seed/recovery/index）→ DB postflight → smoke。
- **1.0 上线 = 全新数据库**（不迁移旧数据，无 migration SQL）：
  1. 在服务器 env 文件补齐上面的必填项，以及要开放的可选项（X 登录、Creem、`TOOL_VAULT_KEY`）；
  2. 新建空库，`DATABASE_URL` 指向它；旧库保留只读，不与新代码混跑；
  3. ff-only 同步 `main` → start unit → `init_db()` 建表并写入 free 档与预置工具；
  4. `curl $FLASHSHARE_BASE_URL/healthz` 返回 `source_sha` = 目标 SHA、`source_dirty=false`；
     启动日志有 gokapi capability probe OK，无 X 登录半配置 WARNING；
  5. smoke：注册 → 首页分享一个文件拿到链接 → 打开 `/r/{id}` → 用 API key 走一次 MCP `share`；
  6. 运营账号（users.status 设为运营状态）登录后台：总览「集成」卡、按需在「平台收入」设置抽佣率与每日登录积分。
- startup `init_db()` 执行 current-schema CREATE … IF NOT EXISTS、current bootstrap 与 routine cleanup；不负责 retired-schema migration / repair / backfill。
- 回滚 = checkout 旧 SHA + restart（新版本未接流量前）。

## Production backup（scripts/）

> 口径：same-server backup = **LOCAL RECOVERY（READY）**，**不是** off-host
> disaster recovery（NOT CONFIGURED）。`FLASHSHARE_BACKUP_REMOTE` 保持空，
> 直到 owner 单独选择 off-host 目的地（Oracle Object Storage / R2 / S3 兼容
> remote）。不要把同服务器目录当作 remote。

- `backup.sh`：daily 04:30 UTC（systemd timer）pg_dump custom 格式 + Gokapi sqlite（`.backup` 一致性快照）与 data 目录 tar.gz；SHA256SUMS；本地 `/var/backups/flashshare/`（daily×7 + 周日 weekly×4 轮换）。
- off-host 上传：`FLASHSHARE_BACKUP_REMOTE`（rclone remote:path）配置后自动启用；未配置时显式 WARNING（仅 on-host）。rclone 未装而 remote 已配 → 非零退出（timer failed 可见）。
- `restore-proof.sh`：monthly timer — 最新备份恢复到 scratch DB + 解档校验行数/校验和后清理。
- failure 可观测：journalctl -u flashshare-backup（非零退出 = unit failed）。

## Gokapi 集成约束（已验证事实）

- 生产目标 Gokapi = **v2.2.4**（disposable 同版本实例实测，见下）。
- `GET /api/files/download/{id}`（header `apikey`，`increaseCounter: false`）是 **authenticated API-key non-counting internal read contract**：200 全量 / 206 Range，DownloadCount 不动。
- service API key 必须拥有 **DOWNLOAD permission**（v2.2.4 `ApiPermDownload`，位 512）；无该 permission 时 internal read 实际返回 **401**（`{"Result":"error","ErrorMessage":"Unauthorized","ErrorCode":2}`，鉴权门在文件查找之前）。早期记录的"401（admin-session scope）"实为 key 缺 DOWNLOAD permission 所致，并非通道不存在。
- missing file（permission 通过后）= **404** + `{"Result":"error","ErrorMessage":"file not found","ErrorCode":5}`；startup probe 的 synthetic id 分支据此 exact response 判 auth-passed，其余一切 status/body fail-closed（`gokapi_download_permission_missing` / `gokapi_download_capability_unknown_*`）。
- `/downloadFile` 是 recipient counted path：**200 且 DownloadCount +1**（实测 0→1）。
- **禁止 sqlite/data-dir/private-storage fallback**：不读 Gokapi 私有 sqlite/schema/content 目录，不做同机文件系统 fallback。
- **验证分层**：fake Gokapi CI（`tests/support/fake_gokapi.py`）只验证 518 adapter contract；真实 v2.2.4 compatibility proof 是 release gate，不由 fake 替代。

### Disposable 真实 v2.2.4 实测证据（2026-09-20，本机同 binary `/opt/gokapi/gokapi` 独立实例）

| 项 | 实测 |
| --- | --- |
| image/version | Gokapi v2.2.4（Go build `9a4b289c`，非 docker 原生 binary） |
| key permissions | full key = VIEW\|UPLOAD\|DELETE\|API_MOD\|EDIT\|DOWNLOAD（543）；对照 key 同权但无 DOWNLOAD（31） |
| upload initial DownloadCount | 0 |
| internal full read | **200**（32 B 全量一致），DownloadCount 0→0 |
| internal Range read | **206**（`Range: bytes=0-9`，10 B），DownloadCount 0→0 |
| `/downloadFile`（recipient counted） | **200**，DownloadCount **0→1** |
| 无 DOWNLOAD permission 的 internal read | **401**（ErrorCode 2，无计数移动） |
| synthetic missing id（empty storage） | full key → **404** ErrorCode 5；no-perm key → **401**（可严格区分 route-missing ≠ permission-pass+file-missing） |
| startup probe | 有 DOWNLOAD 权限 key → PASS；无权限 key → fail-closed `gokapi_download_permission_missing` |
