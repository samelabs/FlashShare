# AGENTS.md — 518.PLUS / FlashShare AI 开发规范

> 所有 AI agent 开发前必须完整阅读本文件。违反规范导致的 bug 由 agent 全责。
> 本文件随仓库演进；服务器本地副本不是事实源，以 GitHub 上的本文件为准。

---

## A. GitHub 是 source of truth

- 开发前先 `git fetch origin`。功能分支默认从 `origin/main` 创建；发布收口期间从
  PM 指定的集成分支创建（1.0 收口期间为 `fix/order-payment-production-closure`，
  即 PR #38）。不得自选基线，不得从其他功能分支或过期分支派生。
- 服务器上的 checkout 只是运行/部署环境，其本地状态不凌驾于 GitHub 仓库。
- 禁止在 production checkout 上做未提交的源码修改后直接重启服务跑新代码。
- 禁止 force push `main` 与集成分支；禁止未经裁定的 merge。
- 长期开发规则、架构约束、正式测试都只存在于仓库内；服务器本地只允许
  secrets、runtime env、部署专属配置、scratch/临时诊断和 dev log。

### 角色与 GitHub 边界

- **Owner**：产品意图与最终裁定；授权 merge 到 main 与 production deploy。
- **PM（主线把控）**：冻结每个批次的执行指令，按精确 SHA 验收（diff / 本地
  canonical / GitHub CI / 必要时真浏览器），创建 PR、决定集成顺序、合入集成分支。
- **执行 agent**（本地或服务器）：只执行 PM 下发的单个批次。
  MAY：`git fetch`、在指定分支上 commit、push 该 non-main 分支（替换自己的
  WIP 提交时仅可 `--force-with-lease` 该分支）。
  MUST NOT：push main 或集成分支、force push 他人分支、开/合 PR、deploy、
  超出批次范围修改、把本地测试结果声明为 GitHub CI 结果（本地绿 ≠ CI 绿，
  两者来源分开表述）。完成后报告并 **STOP**。
- **Production deploy**：由服务器侧 agent 在 owner 明确指令后执行。

### 仓库卫生

- 批次分支命名唯一形态 `batch/<批次号>`（如 `batch/b6-release`）；PM 的验收修复
  直接提交到该批次分支，不另开分支。
- 合入即删除远程分支（仓库已开启合并后自动删除）；不再推进的分支打
  `archive/<原分支名>` 标签后删除，不保留悬空分支。
- 本地同一时间只保留：主 checkout（跟踪集成分支）+ 当前批次的一个 worktree、
  一个 isolated test DB、一个截图目录（`.claude/shots/<批次号>/`，只在本地查看，
  不上传任何外部服务）；批次合入后全部清理。
- 提交邮箱一律使用 GitHub noreply 地址，不得使用个人邮箱。

## B. 四主链

产品只有四条核心机制链，任何改动先定位它落在哪条链、影响哪条链：

- **Tool**：Declaration → Supply → Catalog → Invocation → Dispatch → Gate → Local/Remote → Result
- **Sharing**：Upload → sniff/quota → Gokapi → upload record → public link → expiry/download limit → preview/download → cleanup
- **Identity**：注册/登录/OAuth → session/Bearer → API token → ownership/visibility → profile/password/logout → operator authority
- **Economy**：pricing → 消费池 → consume（幂等）→ 失败 refund → tool_call fact → provider earning/platform income → topup/withdrawal

## C. Toolchain 冻结区

以下契约已冻结，除 concrete regression 外不主动重开、不重新设计：

- **ToolDeclaration**：canonical 三源（system/operator/user）同一 schema；
  inputs.files = {min(≥0), max(≤20), accept}（accept = canonical lowercase
  extension tokens，缺失=全族兼容、显式 `[]`=拒绝）；outputs = ("text","json","file")
  非空子集；label = LocalizedText。
- **Catalog**：canonical projection 形状；official 判定 = owner 关系（bare id ↔ `/` in id）三面等价。
- **ToolInvocation（IO-1）**：精确五键 `{tool_id, source:{files:[...]}（0..N，按工具声明 [files_min, files_max]）, params（text 参数上限 100,000 字符）, delivery, client_ref}`；结果 = 有序 items（text/json/file，≤20 项，text+json ≤1MB，file 沿用单文件上限），文件项同事务各自落地，结果页 /r/{id}；开发者 http 契约唯一形态：请求 `{files:[{filename,content_type,size,url}], params}`、响应 `{items:[...]}`（file 项 data_b64|url）。
  root/source/delivery 精确键集；params 显式 dict；client_ref 必填 non-empty string（≤128），作为 client-generated stable operation identity；null / missing 均拒绝。
  MCP adapter 只对"缺失"填默认，显式非法值 → -32602。
- **Execution / Settlement**：claim running → consume（idem=`call:{call_id}`）→ execute →
  成功同事务 finalize ok + payout/commission/revenue；失败 refund 先于 finalize failed
  （refund idem=`refund:ledger:{consume ledger id}`）；refund 失败 → call 保持 running + 500
  `err_metering_failed`；startup `recover_abandoned_calls()` 幂等恢复。不恢复 COUNT attempt。
- **Supply**：`core/governance/tool_supply.py` 是供给侧单点（validator/seal/persistence）。
- **LocalizedText**：`string | Record<string,string>`；resolver fallback
  requested → en → first non-empty → ""；locale 属于文本值，不属于工具声明。

## D. Finding 分类

审计/排查产出必须归类，不同类不同处理：

- **BUG**：行为与契约不符 → 修。
- **INVARIANT BREAK**：跨链不变量被破坏 → 修 + 补不变量测试。
- **DECISION GAP**：产品语义未定 → **零代码改动**，先暴露决策等裁定。
- **DEBT**：已知欠账、无即时风险 → 记录，不顺手修。

## E. 开发原则

- 修点，不扩面：不把一个点扩成新系统/新框架。
- 不因 AI 重构制造新 abstraction；已有单点（supply/gate/formats/i18n）不另起炉灶。
- product semantics > implementation elegance。
- tests assert behavior，不复制 implementation。
- 没定位准问题不胡乱修改；按逻辑排查，不假定是环境问题。
- 方案未锁定不动代码；讨论阶段零文件改动。

## F. DB / runtime

- feature/test 分支禁止对 production DB 跑 `init_db`。
- 涉及 schema 的验收一律使用 isolated test DB（建临时库 → 跑分支代码 init_db+seed → 验收 → DROP）。
- production startup `init_db()` 只执行 current-schema CREATE … IF NOT EXISTS、current bootstrap 与 routine cleanup；retired-schema migration / repair / backfill 不属于 startup mechanism。
- 两套代码不在同一 production DB 混跑。

## G. 验证

每次改动至少：

- `git diff --check`
- `python -m compileall app core`
- 改动 JS 全部 `node --check`
- 相关 canonical tests（`tests/canonical/`）
- canonical integration tests fail closed：未显式提供 isolated port / TEST_DB_URL 时不得运行；禁止使用 production DB / production Gokapi。
- 任何 feature branch 合入 main 前，CI required checks 应保持全绿。
- UI 改动：真浏览器（CDP）验证用户可见行为

---

## 项目事实（代码机制）

- **站名** 518.PLUS（全部 SEO/meta/title/manifest），首页 hero = 品类/价值陈述（人 + AI Agent 的工具平台，网页主行动 + MCP 次行动，见 issue #43）。
- **栈**：FastAPI + Jinja2 + Uvicorn（单 worker）+ Pillow + PostgreSQL + Gokapi + Nginx + Cloudflare。
- **数据库**：PostgreSQL 是唯一应用数据库（`DATABASE_URL` 环境变量，psycopg）。
- **服务**：systemd unit `flashshare-api`；`KillMode=control-group`、`TimeoutStopSec=15`。
- **平台第三记账主体**：users 行 `username='platform'`。两条收入：用户工具抽佣（platform_commission）+ 系统工具直销（platform_revenue）。
- **双池账本**：消费池（spendable_balance 派生）与收益池（provider_earn，仅可提现）物理隔离；ledger 唯一事实源。
- **operator 权限**：users.status ∈ BACKOFFICE（如 'samelabs'），实时查库。
- **MCP** 是唯一正式 northbound machine surface（streamable-http）；Web 是人类消费面；两者同构 canonical invocation。开发者 HTTP 工具端点是 southbound execution contract，不是公开 API。
- **文件生命周期**：Gokapi 是物理文件权威；518 是 public mapping/页面可见性权威；startup + hourly housekeeping，无 cron/scheduler 子系统。
- **站内预览语义**（已实现）：internal preview 走 `/api/files/download/{id}`（`increaseCounter:false`）authenticated non-counting 通道，不消耗 allowed_downloads；service API key 必须拥有 DOWNLOAD permission（v2.2.4 `ApiPermDownload`），startup capability probe fail-closed 验证。**禁止穿透 Gokapi 私有存储（sqlite/schema/content 目录）实现预览**。见 DEVELOPMENT.md 的 Gokapi 约束（含 disposable 真实 v2.2.4 实测证据）。

## 文档治理

- 代码与项目意图是事实；文档只记录代码推不出的当前事实与规则。文档与代码冲突时以代码为准并修正文档。
- 仓库文档仅：`README.md`（产品与开发者协议）、`AGENTS.md`（开发规则与冻结契约）、
  `DEVELOPMENT.md`（环境/测试/部署运维）、`CHANGELOG.md`（面向用户的变更）。
  `docs/plan/` 仅存放尚未执行的 release migration 资产，执行后移除。
- 禁止计划书、流水账、会话记录、审计报告、历史说明进入仓库（它们属于 issue/PR）。
- 测试断言行为，不断言文档文字。
- 改变行为的批次必须在同一提交中更新受影响的文档行，不留过时描述。

## 前端分层契约（违者必炸）

- 模板只留数据注入 inline（`window.__USER__`/`__LANG__`/`__PAGE__`），其余逻辑全部 `static/pages/*.js`。
- 加载链：注入块 → icons.js → base-engine.js → i18n.js → script block 链。
- JS 文件顶层禁止 `function t` / `function applyI18n` 等引擎同名声明（会遮蔽引擎导致无限递归）；别名一律 `const t = (...) => window.t(...)`。
- 版本戳 `?v=` = md5(内容+mtime)，js/css 独立；CSS 大改后必须硬升 `app/web_ctx.py` 的 `_CSS_VERSION`（CF 缓存）。
- 改任何 static 文件后必须重启服务（版本戳启动时计算）。
- data-i18n 遗漏 = 事故：动态文案 SSR 必须带 `data-i18n`；动态按钮用动词键 `*_btn`。
- **用户层表面禁机器 id / 错语义图标**；动作图标必须语义匹配。

## i18n 单源

- 唯一数据源 `core/i18n.py` SERVER_I18N（10 语言）；生成 `static/i18n.js`：`venv/bin/python tools/gen_i18n.py`（生成物勿手编）。
- 死键删除前六引用形态全查（模板 texts / data-i18n / window.t / 裸 t / 动态族）。

## ⛔ 反越界红线（每条有真实事故背书）

1. CSS 选择器必须带页面作用域前缀，禁裸 `h1{}`；CSS 只动目标块+前后探针，删后扫 selector 残骸。
2. Jinja2 autoescape 恒为 ON：模板插值默认转义。平台自有 HTML/SVG 只经 Markup 标注的生产者（icons.py / utils.file_format_icon）进入；`|safe` 仅限平台生成 JSON（limits/delivery）与仓库内政策文档（doc_body）四处，用户可控值永不裸出。
3. Jinja dict 下标用 `texts['copy']`（`texts.copy` 命中方法）。
4. 删除代码块警惕隐性保护随块被删；删完必跑回归。
5. 生产代码禁边写边猜：改前完整读现有逻辑 → 定案 → 一次写对。
6. 格式族单源 `core/governance/formats.py`；禁从系统工具反推词表。
7. 输出元数据归端点：发布表单无 output 字段；文件名端点说了算。
8. 注释/commit 只写技术事实：禁内部会话编号、禁"用户裁定/拍板"过程措辞。
9. Web Share AbortError：用户取消返回 true 不触发 fallback。
10. 站名不可压缩：顶栏 brand `flex-shrink:0`。

## 改动硬流程

1. **先方案后动手**：改什么/为什么/影响面（哪些文件哪些消费方）在对话中明示；结构性改动出施工清单待确认。
2. **改完必 diff 自审**：`git diff` 逐行核对，确认每处变更都在方案范围内、无夹带。
3. **审核通过才提交**：单一 commit 单一主题，message 写病灶与修法。

审查/问答类请求只出报告+方案，不触发修复动作。全量回归只在用户要求收口或完整功能交付时跑。

## 关键约束

- 零回归容忍。一次一个 coherent mechanism batch；不要 one-bug-one-commit 微切片。P0/P1 或明确 regression 可单独切片；禁止无关顺手改。
- 改动前 grep 全局引用；Python 改完 `ast.parse`；JS 改完 `node --check`。
- 排障三层：`journalctl -u flashshare-api`（服务）+ nginx error.log（接入）+ `/api/client-error` 上报（浏览器）。
