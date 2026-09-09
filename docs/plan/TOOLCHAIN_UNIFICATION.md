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

### 2.2 ToolInvocation（一次调用的目标 envelope — 目标登记，本阶段不实现）

- `tool_id` — 目标工具
- `source` — public_id / public_ids（multi）或既有上传句柄
- `params` — canonical tool params（**不再混入平台生命周期字段**）
- `delivery` — 产物交付策略（正式协议留待阶段 C）
- `client_ref` — 幂等键（gate replay 语义）

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
| **lifecycle 暂混 params 外层旧 envelope**（expiry_days / allowed_downloads 随请求 params 传入） | Web 请求 shape 历史形态 | ToolInvocation 正式分层：lifecycle 独立于 canonical tool params（阶段 C delivery envelope） | delivery 正式协议落地，Web/API/MCP 请求 shape 切换 |
| **compatibility selftest endpoint**（`/api/usertools/{id}/selftest`） | My Tools 旧自测链的薄适配 | 作者从真实工具页 `/tool/{owner}/{tool_id}` 走 runToolFlow 完整消费链 | 确认无外部调用方后删除该 endpoint |
| ~~first-party `register()` 仍接受 MIME creation input~~ **已删除（阶段 B 完成）** | — | register 直接声明 canonical ext；seed 无需再 canonicalize | 已随阶段 B 迁移全部一方源码并删除 shim |

每个 shim 的容忍策略：**读兼容、写 canonical**。新写入（用户发布、operator
创建、一方 seed）落库永远是 canonical ext；只有读取存量时才展开/回落，
且遇到完全未知 token 时安全保留原 token，不猜格式、不 500。

---

## 5. 后续阶段

- **阶段 A — Tool Input Contract（本阶段）**
  canonical AcceptToken、精确执行闸门、multi 真实边界、Catalog canonical
  projection、用户/operator 发布 canonical write、前端 exact selector。
- **阶段 B — Tool Supply Unification（已完成）**
  system local / operator remote / user remote 三入口的 create/update
  service 收口为同一供给路径；移除 register 的 MIME creation shim。
  落地事实：三源 source adapter → `governance.tool_supply` 的
  `validate_tool_declaration()`（纯 declaration 校验，无来源政策）→
  `create_tool / update_tool / upsert_seed_tool`（DB 列映射唯一份）→
  `repo.catalog` 通用 row writer。HTTP binding 组装单点
  `build_http_binding()`（seal 一次，user 路由的 `_endpoint_decl` 已删除）。
  来源政策（user caps/SSRF/self-test gate、operator public/trusted、
  first-party seed 语义、pricing）保持在各 source adapter。
- **阶段 C — ToolInvocation source/params/delivery/client_ref 分层**
  正式 invocation envelope；lifecycle/delivery 从 params 外层彻底分离；
  移除 lifecycle 旧 envelope shim。

---

## 6. Definition of Done

- system / user / operator 三类 declaration **同构**（仅 ownership / trust /
  execution binding 不同）。
- Web / API / MCP / Tool Page 的 projection **同构**（同一 row_to_item）。
- execution **无旁路**：自测、MCP、Web、token 调用全部经过同一
  dispatch → gate → endpoint 链路。
- 所有 compatibility shim 都在第 4 节登记，且具备明确删除条件；shim 只做
  读兼容，新写入永远 canonical，最终可整体删除。
