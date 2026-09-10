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

## Local / test DB setup

```bash
sudo -u postgres psql -c "CREATE DATABASE flashshare_test OWNER flashshare"
# 用分支代码初始化 + seed：
DATABASE_URL=postgresql+psycopg://…/flashshare_test \
FLASHSHARE_SESSION_SECRET=<test-secret> \
PYTHONPATH=core venv/bin/python -c "from database import init_db; from tools import load_all; load_all(); from governance.catalog import seed_from_registry; init_db(); print(seed_from_registry())"
# 起隔离实例：
DATABASE_URL=… FLASHSHARE_SESSION_SECRET=… venv/bin/python -m uvicorn app.main:app --port 8100 --workers 1
```

**禁止**对 production DB 跑 test 分支的 `init_db`。

## Run / test commands

```bash
python -m compileall -q app core          # 语法
node --check static/app.js                # 每个改动 JS
git diff --check
FLASHSHARE_TEST_PORT=8100 python tests/canonical/test_invocation_canonical.py   # Stage C 42
FLASHSHARE_TEST_PORT=8100 python tests/canonical/test_supply_accept_multi.py    # 4C-close 15
```

canonical 测试需要一个已启动的（隔离）服务实例 + 可用 DB；断言走 HTTP + DB 查询。
测试账号在测试库自建（脚本内 `new_user()`），不留生产数据。

## Branch workflow

1. `git fetch origin`；从 `origin/main` 建分支。
2. 小步改动 + diff 自审 + 门禁（compile/node/canonical）。
3. push 分支，等二次核查；main 的合并由 owner 裁定。

## Production deploy 概要

- 生产 = 服务器 checkout `main` + systemd `flashshare-api`。
- 部署 = ff-only 同步 main → restart unit → healthz → 启动日志（seed/recovery/index）→ DB postflight → smoke。
- schema migration 由新进程 startup `init_db()` 自动执行，与代码切换一体。
- 回滚 = checkout 旧 SHA + restart（新版本未接流量前）。

## Gokapi 集成约束（已验证事实）

- 生产 Gokapi = **v2.2.4**。
- `GET /api/files/download/{id}` 用 apikey 返回 **401**（admin-session scope）——当前版本**没有**可用的 API-key non-counting 读通道。
- `/downloadFile` 强制计数（`increaseCounter:false` 无效）。
- 因此：**站内预览不消耗下载次数**是已决定的产品语义，但实现等待 Gokapi 升级或明确的 deployment-level integration 决策。
- **DO NOT bypass Gokapi abstraction**：不读它的私有 sqlite / schema / content 目录，不做同机文件系统 fallback。
