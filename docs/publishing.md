# Publishing a tool

The declaration and execution contract for tools published on 518.PLUS.


用户发布的工具通过一份 JSON manifest 声明。本节描述当前已落地的声明契约：结构、多语言文本、Web 编辑与 API 写入语义、参数校验与错误码。

## LocalizedText 类型

允许多语言的文本字段接受两种形态：

```json
"Image Compression"
```

```json
{ "en": "Image Compression", "zh": "图片压缩", "ja": "画像圧縮" }
```

规则：

- locale 属于文本值，不属于 tool 对象。不存在 tool-level locale，不存在按语言拆分的 manifest。
- 工具结构只有一份；多语言只发生在下文明确允许的文本字段上。
- locale 键为平台 10 语言码：`ar` `de` `en` `es` `fr` `ja` `ko` `pt` `ru` `zh`（即界面语言码本身，不是 `zh-CN` 之类的 BCP-47 标签）。

## 字段多语言许可表

允许 LocalizedText（文本字段）：

| 字段 | 位置 |
|---|---|
| `label` | 工具名 / param / option |
| `desc` | 工具简介 |
| `placeholder` | param（text 型） |

语言无关（机器结构，禁止多语言）：

| 字段 | 说明 |
|---|---|
| `tool_id` / `tool_key` | 唯一身份，`^[a-z][a-z0-9_]{1,40}$` |
| `execution` | 当前用户工具固定为 `http` |
| `format_families` | 八道准入词表单选（image/document/data/text/audio/video/cad3d/archive），accept 由族派生；纯文本工具不带 |
| `files_min` | 输入文件数下限，int ≥ 0（0 = 可不带文件调用）；缺省 1 |
| `files_max` | 输入文件数上限，int 0..20；须 ≥ files_min；缺省 1。0 = 纯文本工具：无文件位，不带 format_families/accept，第一个 text 参数作为输入框 |
| `outputs` | 产出类型非空子集 `["text","json","file"]`（声明工具可能返回哪些结果项）；缺省 `["file"]` |
| `icon` | **发布不可声明**（见「图标语义」）。工具图标不是工具作者的字段——表达的是"这工具吃什么" |
| `param.key` | `^[a-z][a-z0-9_]{0,31}$`，同工具内唯一 |
| `param.type` | `enum` / `text` / `number` / `boolean` |
| `param.required` | bool |
| `param.default` | 与 type 同型；enum 必须 ∈ options[].value |
| `min` / `max` | number 边界 |
| `option.value` | 稳定机器值，唯一，非空 |
| `endpoint url` / `timeout_s` | 用户端点必须 https |

上限（服务端 validator 强制）：

| 约束 | 值 |
|---|---|
| tool label | 每语言 ≤ 40 字符 |
| tool desc | 每语言 ≤ 200 字符 |
| param.label / param.placeholder / option.label | 每语言 ≤ 64 字符 |
| params | 每工具 ≤ 8 个 |
| options | 每 enum 参数 ≤ 32 个 |

## 合法 manifest 示例

```json
{
  "tool_id": "image_compress",
  "label": { "en": "Image Compression", "zh": "图片压缩", "ja": "画像圧縮" },
  "desc": { "en": "Reduce image file size", "zh": "减小图片文件大小" },
  "url": "https://example.com/compress",
  "timeout_s": 60,
  "format_families": "image",
  "params": [
    {
      "key": "quality",
      "type": "number",
      "min": 1,
      "max": 100,
      "default": 80,
      "label": { "en": "Quality", "zh": "质量" }
    },
    {
      "key": "style",
      "type": "enum",
      "label": { "en": "Style", "zh": "风格" },
      "options": [
        { "value": "fast", "label": { "en": "Fast", "zh": "快速" } },
        { "value": "best", "label": { "en": "Best", "zh": "最佳" } }
      ],
      "default": "fast"
    }
  ]
}
```

`option.value`（`fast`/`best`）是稳定机器值；`option.label` 才是 LocalizedText。禁止把译文当作 value。

## 图标语义（tool diet → icon）

工具图标表达**"这工具吃什么"**，与上传区的格式汇总文案同一聚合（`governance/formats.py` 族词表）的两个投影：文本投影是汇总行，可视投影是图标。因此图标不是工具作者的字段——发布表单没有、也不应有图标项。目录展示值为：

- **operator 策展优先**：`platform_tools.icon` 是运营策展位（系统工具的 shield / convert / merge 等动词语义靠它）；
- **未策展行由声明派生**（`core/icons.py derived_tool_icon`）：纯文本输入（files_max=0）→ `text`；accept 未声明（全族兼容）→ `wand`；命中单一族 → 该族字形；命中多族 → `convert`。改声明图标自动跟随，语义上不可能与工具实际吃的内容不符。

## 读取规则（canonical fallback）

全平台唯一的 LocalizedText 回落链：

```
requested locale → en → 任意第一个非空值 → ""
```

- Python `resolve_label()` 与 JS `resolveLabel()` 是该算法在两个 runtime 的等价实现，由协议回归测试保证行为一致。
- 调用方可在 resolver 返回后追加业务兜底（如工具名回落 `tool_id`）——这属于调用方，不属于 resolver。
- 其余调用方禁止再手写第二套 locale 回落链。
- 搜索例外：市场搜索索引（`data-name`）故意拼接所有语言的值，跨语言搜索是设计行为，不走单语言 resolver。

## 写入语义：Web 编辑 vs API

两种上下文由请求头 `X-518-Edit-Locale: <平台 locale>` 区分。

**Web 编辑（携带该头，发布表单）**——label / desc 一律解释为 locale patch：

- str 与 dict 相同语义：只写明确提及的 locale，未提及的 locale 原样保留。
- 空字符串 = tombstone，显式删除该 locale。
- 回填的 fallback 值若用户未修改，不会物化成新的 locale 键（no-change 判定基于修改前的原始旧值快照，结果与提交 dict 的键顺序无关）。
- 工具 label 至少保留一个有效语言值，删除到全空时保留原值；desc 可以删到空。

**API / manifest PATCH（无该头）**——替换语义：

- dict = 显式全集：未提及的语言即删除。
- str 归一化为 `{"en": str}`。

对比例——旧值 `{ "en": "Quality", "zh": "质量" }`：

| 请求 | 提交 | 结果 |
|---|---|---|
| Web zh patch（携头） | `"label": "质量"`（未改则不提交亦可） | `{ "en": "Quality", "zh": "质量" }`，en 保留 |
| API dict（无头） | `"label": {"en": "Quality"}` | `{ "en": "Quality" }`，zh 被删除 |

tombstone 语义以 LocalizedText map 表示（`locale` 是 dict 的 key，不是 manifest 字段；不存在 `tool.locale`、不存在 `default_locale`）。旧值 `{ "en": "Quality", "zh": "质量" }`：

- Web patch 提交 `{ "zh": "" }` → `""` 是 tombstone，显式删除 zh
- Web patch 提交 `{}` 或完全不出现 `zh` 键 → zh 未提及，保留旧值

## params 写入契约

机器结构（参数/选项的增删改）在两种上下文下都以提交的 params 为准：parameter 按 `key` 对齐，option 按 `value` 对齐，删除即结构删除（其翻译随结构删除）。

差异在三个 LocalizedText 叶子（`param.label` / `param.placeholder` / `options[].label`）：

- **Web Form（携头）**：对旧声明做当前 locale merge，保留其他 locale 文本。禁止按数组 index 合并。
- **API / manifest（无头）**：显式全集替换，不做 locale merge——提交什么存什么。

## 工具参数错误码

声明期与执行期共用一套平台错误码（属于 platform i18n，经 `_msg`/`_errm` 转译为界面语言；与用户声明的 LocalizedText 是两个不同层，机制不合并）：

| code | 阶段 | 含义 |
|---|---|---|
| `bad_params_json` | 声明期 | 参数声明结构非法 |
| `bad_param_key` | 声明期 | key 格式/唯一性违规 |
| `bad_param_type` | 声明期 | type 不在四型之内 |
| `bad_param_options` | 声明期 | enum options 缺失/超限/重复 |
| `bad_param_default` | 声明期 | default 与类型/范围不符 |
| `params_too_many` | 声明期 | 参数超过 8 个 |
| `label_too_long` | 声明期 | 文本超过 64 字符 |
| `param_required` | 执行期 | 必填参数缺失（422） |
| `param_invalid` | 执行期 | enum 值不在 options 内（422） |
| `param_out_of_range` | 执行期 | number 越界（422） |

## 发布状态流转（publish gate）

平台不审核工具，但公开市场里的工具至少被作者本人成功运行过一次：

- 新建（POST /api/usertools）默认 `visibility=unlisted`：工具页可访问、可调用，但不进 `/tools` 市场与 MCP `catalog_search`，也不能被他人收藏。创建请求显式携带 `visibility: "public"` → 422 `needs_successful_run`（新工具不可能已有成功调用）。
- 改为 `public`（PATCH `visibility: "public"`）要求该工具存在至少一条"所有者本人发起、状态为 `ok`"的 `tool_calls` 记录；不满足 → 422 `needs_successful_run`。判定与可见性写入同一事务。
- 他人的成功调用、本人失败的调用都不解锁；仅"本人 + 成功"计入。
- 已经是 `public` 的工具不受影响；`public → unlisted/private`（取消公开）不受限制。
- 公开工具都能被搜索到（`/tools?q=`、首页工具选择器、MCP `catalog_search`）；不带查询的列表（首页工具区、`/tools`、`catalog_search` 空查询、`/llms.txt`）只展示运营推荐的工具，按运营排序。

## 执行契约

用户工具当前执行类型为 `http`：平台在调用时向开发者端点发起 `POST`，请求体：

```json
{
  "files": [
    { "filename": "a.png", "content_type": "image/png", "size": 1024, "url": "<一次性拉取 URL>" }
  ],
  "params": { "quality": 80, "style": "fast" }
}
```

- `params` 只含声明内的键值（声明外键在入口剥离），已按声明校验；纯文本输入
  经 text 参数传入（上限 100,000 字符），0 文件调用合法。
- 响应必须是 JSON 的 items 数组（唯一形态）：

```json
{
  "items": [
    { "kind": "text", "text": "processed 2 files" },
    { "kind": "json", "data": { "width": 800 } },
    { "kind": "file", "filename": "out.png", "content_type": "image/png",
      "data_b64": "..." }
  ]
}
```

  file 项用 `data_b64` 或 `url`（平台拉取，走同一 SSRF 守卫与大小上限）交
  付字节；`items` ≤ 20、text+json 合计 ≤ 1MB、单文件沿用上传上限。响应不符
  合形态 → 调用失败并全额退款。
- `output_filename` / `output_content_type` 已不可声明：提交会被拒绝（`bad_output_filename` / `bad_output_content_type`），产物名与类型由 items 声明或平台嗅探决定。
- 端点必须 https 且解析到公网地址（SSRF 守卫；运营方采纳的 trusted 行豁免）。

## 协议原则

**结构单份，文本多形态。** locale 停留在文本叶子节点，不上升为 schema 维度：没有 `default_locale`、没有 `tool.locale`、没有 per-language manifest。Agent 与 MCP 消费的是稳定结构；人类看到的翻译只存在于 LocalizedText 字段里。

