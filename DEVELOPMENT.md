# DEVELOPMENT.md

长期开发事实：架构总览、环境搭建、运行与测试命令。 secrets/values 不在本文件。

## 架构总览

```
用户 → Cloudflare → Nginx → Uvicorn:8000 (FastAPI, 单 worker)
  ├── app/main.py            实例 + 中间件 (auth/quota/语言检测/recovery hook)
  ├── app/routes/            pages / auth / api(v1) / proxy(/f) / mcp / earn / topup / samelabs / usertools
  ├── core/execution/        invocation(五键 parser) / dispatch(四面单入口) / gate(计量+settlement) / orchestrator
  ├── core/governance/       tool_supply(供给单点) / catalog / pricing / formats(格式族单源) / platform / delivery
  ├── core/economy/          billing(双池) / earnings / reconcile / subscription / creem
  ├── core/identity/         auth / sessions / apitokens
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
- `docs/plan/` 架构计划文档（TOOLCHAIN_UNIFICATION.md 等）
- `scripts/` 一次性迁移脚本

## Prerequisites

- Python 3.12（生产 venv：`venv/`）
- Node.js（`node --check` JS 语法门禁）
- PostgreSQL（业务库 + isolated test DB）
- 外部 runtime dependencies：**Gokapi**（文件存储）、**Nginx**（反代/静态）、Cloudflare（DNS/缓存）

## Environment variables（只列名字，不列值）

- `DATABASE_URL` — PostgreSQL DSN（`postgresql+psycopg://…`）
- `FLASHSHARE_SESSION_SECRET` — session/token 签名密钥
- `FLASHSHARE_GOKAPI_URL` / `FLASHSHARE_GOKAPI_KEY` — Gokapi API
- 其余见 `core/config.py`（全部 `FLASHSHARE_*` 前缀）

生产值在服务器 env 文件（不进仓库）；测试用 isolated DB + 任意测试 secret。

## Local / test stack（isolated — 全部必须显式）

canonical integration tests **fail closed**：未显式提供 isolated port 与
TEST_DB_URL 时直接退出（不发任何请求）。两种合法方式：

**A. manual isolated stack**

- isolated PostgreSQL（临时库，验收后 DROP）
- isolated Gokapi（disposable 实例，见下）
- isolated app 端口（≠ 8000；8000 是生产端口，测试显式拒绝）

```bash
# PostgreSQL
sudo -u postgres psql -c "CREATE DATABASE flashshare_test OWNER flashshare"
# 初始化 + seed（分支代码）
DATABASE_URL=postgresql+psycopg://…/flashshare_test \
FLASHSHARE_SESSION_SECRET=<test-secret> \
FLASHSHARE_GOKAPI_URL=<isolated-gokapi> FLASHSHARE_GOKAPI_KEY=<its-key> \
PYTHONPATH=core venv/bin/python -c "from database import init_db; from tools import load_all; load_all(); from governance.catalog import seed_from_registry; init_db(); print(seed_from_registry())"
# 隔离实例（端口 != 8000）
DATABASE_URL=… FLASHSHARE_SESSION_SECRET=… FLASHSHARE_GOKAPI_URL=… FLASHSHARE_GOKAPI_KEY=… \
  venv/bin/python -m uvicorn app.main:app --port 8100 --workers 1
```

**B. GitHub Actions CI v1**：真实 PostgreSQL 16 + 真实 FlashShare app +
`tests/support/fake_gokapi.py` 提供 Gokapi HTTP boundary test service，然后运行
`tests/canonical/`。workflow 在 `.github/workflows/ci.yml`。

CI v1 测的是 518.PLUS 自己的 invariant，不是 Gokapi compatibility。fake Gokapi
只实现 `core/gokapi.py` 当前消费的外部 HTTP contract（add/list/get/download/delete），
不实现 ownership/quota/tool/economy/lifecycle，不读 Gokapi 私有 sqlite/schema/content。
生产 Gokapi、生产 PostgreSQL、生产 secrets 和 deployment action 均不进入 CI。

真实 Gokapi compatibility smoke 可以后作为独立低频测试；不要把 web wizard 或
生产 Gokapi 接到普通 commit CI。

### Isolated Gokapi（人工正式测试前提）

Canonical integration tests 会真实 upload / local execute / result upload /
MCP tools/call，因此人工运行时**必须**指向 disposable/local Gokapi，**绝不使用
production Gokapi**。只隔离 DB 而复用生产 Gokapi 是禁止的。

全新 Gokapi 实例初始化：容器/二进制首次启动后，在它的 admin UI 里完成初始
设置并生成 API key（无固定默认 key — 不要假设）；将实例 URL+key 注入测试
服务的 `FLASHSHARE_GOKAPI_URL`/`FLASHSHARE_GOKAPI_KEY`。

**禁止**对 production DB 跑 test 分支的 `init_db`；禁止测试写 production
Gokapi。

## Run / test commands

```bash
python -m compileall -q app core          # 语法
node --check static/app.js                # 每个改动 JS
git diff --check
# canonical tests — 环境变量无默认值，必须显式提供：
FLASHSHARE_TEST_PORT=8100 TEST_DB_URL=postgresql://…/flashshare_test \
  python tests/canonical/test_invocation_canonical.py     # Stage C 42
FLASHSHARE_TEST_PORT=8100 TEST_DB_URL=postgresql://…/flashshare_test \
  python tests/canonical/test_supply_accept_multi.py      # 4C-close 15
```

测试账号在测试库自建（脚本内 `new_user()`），不留数据（测试库最终 DROP）。

## Branch workflow

1. `git fetch origin`；从 `origin/main` 建分支。
2. 小步改动 + diff 自审 + 门禁（compile/node/canonical）。
3. push non-main branch → **STOP**。
4. Server Agent 无 GitHub API 权限（仅 SSH deploy key）：PR 创建/diff 审查/
   GitHub CI 核验/merge 全部由 owner 在 GitHub 侧完成；本地测试结果不得
   声明为 CI 结果。
5. main 前进后，production deploy 由 Server Agent 按指令执行。
4. 合入 main 前 GitHub CI required checks 应全绿；CI 不执行 production deploy。

## Production deploy 概要

- 生产 = 服务器 checkout `main` + systemd `flashshare-api`。
- 部署 = ff-only 同步 main → restart unit → healthz → 启动日志（seed/recovery/index）→ DB postflight → smoke。
- schema migration 由新进程 startup `init_db()` 自动执行，与代码切换一体。
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
