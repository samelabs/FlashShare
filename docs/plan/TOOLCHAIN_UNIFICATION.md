# Toolchain Unification — 架构基线

> 本文件是 FlashShare 工具体系的架构基线，描述目标契约、当前兼容层与收敛
> 阶段。它不是开发流水账；批次实现细节见 commit history。
> 阶段 A（Tool Input Contract）落地后，accept/multi 三源同构即以此为准。

---

## 1. North Star

任何 Tool，不论供给来源：

- **system local**（一方注册、进程内 handler）
- **operator remote**（平台受信 HTTP endpoint）
- **user remote**（用户自助发布的 HTTP endpoint）

都共享**同一个 declarative Tool Contract**。三源之间只允许以下三点不同：

1. **ownership** — owner_id（一方/operator 为 NULL，用户工具为作者 id）
2. **trust policy** — visibility / trusted / gate 策略
3. **execution binding** — local handler 或 HTTP endpoint 声明

除此之外，身份、展示、输入契约、参数契约、能力声明的**形状完全同构**；
Web 首页、Tool Page、`/api/v1`、MCP 四个消费面看到的是同一份 projection。

---

## 2. Canonical contracts

### 2.1 ToolDeclaration（工具的静态声明）

| 字段 | 语义 |
|---|---|
| identity | `tool_key` / `tool_id` / owner |
| label / desc | LocalizedText 多语言展示 |
| tool_type | view / peek / produce（TYPE_VIEW / TYPE_PEEK / TYPE_PRODUCE，canonical 协议枚举） |
| **accept** | **exact executable input contract：canonical 小写 extension token 列表（`[".pdf", ".docx"]`）或 `null`（不限制，仍受平台 ALLOWED_EXT 总边界）** |
| **multi** | **标准 capability：false=单文件；true=允许 1..20 个输入文件** |
| params | canonical 参数声明（enum/number/boolean/text），与 legacy presets 二选一投影 |
| visibility | public / unlisted / private，由 `governance.catalog.resolve()` 单一治理 |
| execution binding | local handler 或 HTTP endpoint（endpoint 密钥不进公开 projection） |

### 2.2 ToolInvocation（canonical invocation — Stage C 已落地冻结）

- `tool_id` — 目标工具
- `source.files[]` — 每项 `{public_id, source_token}`；single/multi 只有
  files 数量区别，schema 唯一（1..20）
- `params` — canonical tool params（**不含平台生命周期字段**）；
  `params: {}` 表示无参数
- `delivery` — `{expiry_days, allowed_downloads}`（精确两键，值域单源
  `governance.delivery`）
- `client_ref` — `null` = fresh invocation；非空 string ≤128 =
  user-scoped 幂等键（gate replay 语义）

root 五键全部显式存在（缺失即 422 invalid_request）；无 legacy
invocation adapter。

### 2.3 ToolResult（目标说明）

统一的产物记录：落库声明、生命周期（expiry_days / allowed_downloads）、
计费与 gate 结果。生命周期属于**平台产物保存**层，与 tool params 分层，
不通过 canonical params 清洗链路。

---

## 3. Input invariants（不可回退的输入不变量）

1. **family ≠ accept**：family（image/document/archive/cad3d/media/data/code）
   只是 authoring / discovery 分类维度。
2. **family 不参与 runtime 放宽**：执行闸门不得把精确 accept 反向扩大成
   整个 family；`application/pdf` 只放行 `.pdf`，不放大到 document 全族。
3. **accept 精确到 extension**：canonical token 是小写扩展名（`.pdf`、
   `.dxf`）；同属 `application/octet-stream` 的 `.dxf` 声明不得放行 `.fbx`。
4. **MIME 只是 compatibility input/read**：legacy MIME 声明经
   `governance.formats.extensions_for_accept()` 展开成 extension；新写入的
   声明一律 canonical ext，MIME 不再作为 canonical storage / projection。
5. **multi 属于 ToolDeclaration**：不是 params；multi=true 允许 1..20，
   >20 与 multi=false 显式多文件形态都返回 typed `err_too_many_files`，
   任何入口（dispatch / MCP / process-flow）都不得静默截断。
6. **四面同一 accept/multi**：Web 首页、Tool Page、`/api/v1`、MCP 对同一
   工具看到的 accept token 列表与 multi 布尔完全一致，来自同一 canonical
   Catalog projection（`governance.catalog.row_to_item()`）。

格式事实源唯一：`core/governance/formats.py`（FAMILIES / MIME_EXTS /
VOCAB_EXTS / extensions_for_accept / format_manifest）。前端、发布页、
operator 入口都从它派生，禁止第二份手写格式表。

---

## 4. Current compatibility shims（当前兼容层登记）

| Shim | Why it exists | Canonical replacement | Removal condition |
|---|---|---|---|
| **legacy MIME accept**（DB 存量行 / runtime 声明含 MIME） | 历史工具与用户存量数据以 MIME 存储 | accept 一律 canonical extension token；读时由 `extensions_for_accept` 展开、projection 时 canonicalize | 存量行全部重写为 ext，且 `extensions_for_mimes` 无消费者后删除 MIME 展开分支 |
| **legacy presets**（presets_json 与 params_json 双轨） | 一方工具旧预设模型 | canonical params 声明；projection 有 params 时 presets=None | 一方 registry 全部迁移到 params 声明 |
| ~~lifecycle 混 params 外层旧 envelope~~ **已删除（阶段 C 完成）** | — | ToolInvocation delivery envelope 独立字段 | Web/API/MCP 已全部切换 canonical invocation |
| ~~compatibility selftest endpoint~~ **已删除（阶段 C 完成）** | — | 作者从真实工具页 `/tool/{owner}/{tool_id}` 走 runToolFlow 完整消费链 | endpoint 已删除；self_test_required 发布门与 UI 自测概念保留 |
| ~~first-party `register()` 仍接受 MIME creation input~~ **已删除（阶段 B 完成）** | — | register 直接声明 canonical ext；seed 无需再 canonicalize | 已随阶段 B 迁移全部一方源码并删除 shim |

每个 shim 的容忍策略：**读兼容、写 canonical**。新写入（用户发布、operator
创建、一方 seed）落库永远是 canonical ext；只有读取存量时才展开/回落，
且遇到完全未知 token 时安全保留原 token，不猜格式、不 500。

---

## 5. 后续阶段

- **阶段 A — Tool Input Contract（已完成）**
  canonical AcceptToken、精确执行闸门、multi 真实边界、Catalog canonical
  projection、用户/operator 发布 canonical write、前端 exact selector。
- **阶段 B — Tool Supply Unification（已完成）**
  system local / operator remote / user remote 三入口的 create/update
  service 收口为同一供给路径；移除 register 的 MIME creation shim。
  落地事实：三源 source adapter → `governance.tool_supply` 的
  `validate_tool_declaration()`（declaration 校验 + normalization：accept
  限 VOCAB_EXTS 输出 sorted unique、params 采 validate_params_decl canonical
  输出、label 必填/desc 可选；返回值即**可持久化 canonical declaration**，
  无来源政策）与 `validate_tool_patch()`（update patch 字段级同规则，
  不静默强转）→ `create_tool / update_tool / upsert_seed_tool`（DB 列映射
  唯一一份 `_serialize_row`；当前仅 `create_tool(con=...)` 支持加入
  source adapter 事务，`update_tool`/`upsert_seed_tool` 自持事务）→
  `repo.catalog` 通用 row writer。HTTP binding 组装与 auth seal 唯一入口
  `build_http_binding()`（`_endpoint_decl` 与 operator 旧 endpoint 组装段
  均已删除，全仓 supply 路径无第二处 seal）。
  来源政策（user caps/SSRF/self-test gate、operator public/trusted、
  first-party seed 语义、pricing）保持在各 source adapter；user 并发 cap
  由 source adapter 对 owner 行 FOR UPDATE 后同事务 count+insert 保证。
- **阶段 C — ToolInvocation source/params/delivery/client_ref 分层（已完成）**
  唯一 canonical ToolInvocation（`execution/invocation.py`）：
  `tool_id` / `source.files[]`（1..20，每项 `{public_id, source_token}`）/
  `params`（只含 tool params）/ `delivery`（`{expiry_days, allowed_downloads}`，
  值域单源 `governance.delivery`，严格类型无强转）/ `client_ref`
  （null = fresh；非空 = user-scoped 幂等键，gate 语义不变）。
  Web（app.js + process-flow runToolFlow）/ REST v1 / MCP 构造完全相同的
  canonical invocation；dispatch 只认这一种 schema，旧 flat shape 一律
  422 invalid_request，无 compatibility adapter。delivery/client_ref 等
  平台字段不泄漏给 handler 与 remote endpoint。lifecycle 旧 envelope 与
  selftest compatibility endpoint 均已删除。C-close 收口：root 精确五键
  （缺失键同样拒绝，`params: null` 拒绝）、delivery 精确两键（未知键不
  剥离）、MCP adapter 只对完全缺失填默认、显式非法输入 -32602。

---

## 6. Definition of Done

- system / user / operator 三类 declaration **同构**（仅 ownership / trust /
  execution binding 不同）。
- Web / API / MCP / Tool Page 的 projection **同构**（同一 row_to_item）。
- execution **无旁路**：自测、MCP、Web、token 调用全部经过同一
  dispatch → gate → endpoint 链路。
- 所有 compatibility shim 都在第 4 节登记，且具备明确删除条件；shim 只做
  读兼容，新写入永远 canonical，最终可整体删除。
