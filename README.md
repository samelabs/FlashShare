# FlashShare · 518.PLUS

同时面向人和 AI Agent 的工具平台：一套工具目录，网页直接使用，或通过 MCP 让 AI 调用。当前工具以文件处理为主——上传、处理、生成临时分享链接。

## 功能

- 📁 文件上传（最大 50MB，格式族见 `core/governance/formats.py`）
- 🔗 临时分享链接：有效期与下载次数上限由会员等级决定（免费 1/3/7 天、1–200 次；Plus/Pro 另含 30 天、1000 次）
- 📱 每个文件自动生成二维码
- 🔧 内置文件工具：图片压缩 / 截图美化 / 隐私净化（剥元数据与 AI 水印）/ 图片转 PDF / PDF 合并 / 文档转 PDF / ICO 图标 / 裁白边 / ZIP 打包
- 🧩 开放工具链：第三方以 HTTP API 契约接入自己的工具，平台负责计量与分发
- 🤖 MCP 服务端（Streamable HTTP）：任意 MCP 客户端可调用全部工具，按次计费、失败自动退款
- 💰 两类资产隔离：积分（1000 积分 = 1 美元，只能消费）/ 开发者收益（美元计，只能提现）
- 🌍 10 种语言界面，阿拉伯语 RTL 原生支持
- 👤 用户账号：本地注册或 X (Twitter) OAuth，支持绑定/解绑
- 📦 流式上传（64KB 分块），内存友好

## 环境要求

- Python 3.12+
- PostgreSQL
- [Gokapi](https://github.com/Forceuk/Gokapi)（文件存储）
- nginx（HTTPS 与反向代理，推荐）

## 快速开始

```bash
git clone https://github.com/samelabs/FlashShare.git
cd FlashShare
```

1. 配置环境变量（`/etc/flashshare.env` 或导出到 shell）：

```
DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/flashshare
FLASHSHARE_SESSION_SECRET=<随机长字符串>
FLASHSHARE_BASE_URL=https://your-domain
```

2. 启动：

```bash
./run.sh   # uvicorn app.main:app --workers 1
```

3. nginx 反代到 `127.0.0.1:8000`，`/d/`、`/dh/` 交给 Gokapi。

## API

### MCP（智能体 / 程序接入的唯一正式通道）

端点 `https://your-domain/mcp`（Streamable HTTP，无状态 POST→JSON），
Bearer API key 认证（在网页 /account/keys 创建，随时可查看）。在任意 MCP 客户端添加服务端 URL + API key 即可调用全部
工具：`tools/list` 分页发现、`catalog_search` 关键字检索、`tools/call`
canonical 调用。支持协议版本 `2025-11-25` / `2025-06-18` / `2025-03-26` /
`2024-11-05`（initialize 按参数协商；`MCP-Protocol-Version` 请求头缺失按
`2025-03-26` 处理，`2025-06-18` 起不再接受 JSON-RPC 批量请求）。

`tools/call` 的输入 = 0..20 个文件（按工具声明的 [min, max]，纯文本工具
允许 0 文件，文本经 params 传入，上限 100,000 字符）+ 参数。每个 `files[]` 项
三选一：

- 内嵌：`{"filename": "a.png", "b64": "<base64>"}`；
- URL：`{"url": "https://…/a.png"}` — 服务端经 SSRF 守卫（仅 https、仅公网
  IP、重定向逐跳复检）拉取，大小上限与内嵌一致；
- 引用：`{"public_id": "…", "source_token": "…"}` — 复用调用者自己的已上传
  文件，不再占用上传额度。

三种形态走同一 ingress / 额度 / `client_ref` 幂等路径（同 `client_ref`、
不同输入 → 幂等冲突）。

结果模型（IO-1）：一次调用的结果是有序的 0..20 个"项"——
`{"kind":"text","text"}` / `{"kind":"json","data"}` /
`{"kind":"file",filename,content_type,size}`（text+json 合计 ≤ 1MB，单文件
沿用上传上限）。MCP 直接把 items 交给 agent（structuredContent.items，文件项
带 download_url 直链，小图附 image 内容块，result_url 为人类结果页
`/r/{id}`）；网页渲染结果页。文件项各自落 Gokapi + uploads 行，与计费分账
同一事务。

大文件不要走 base64：用元工具 `create_upload` 两段式上传，文件字节全程
不经过模型、不出现在任何 JSON-RPC 请求体里：

1. `tools/call(name="create_upload", arguments={filename, size,
   content_type?, client_ref})` — 免费元工具（不产生 tool_call 计费）。
   签发前完成校验（单文件上限、24h 上传额度预检），返回一次性
   `upload_url`（10 分钟有效、单次使用、绑定 user 与声明大小）。
2. `PUT upload_url`，请求体就是原始文件字节（无需 Authorization，
   URL 即凭据；超出声明大小立即 413 中止）。成功走与内嵌 base64 完全
   相同的 ingress 路径（额度占用 / Gokapi 存储 / 上传记录），返回
   `{public_id, source_token}`。
3. 把 `{public_id, source_token}` 作为 `tools/call` 的 files[] 引用形态
   调用任意工具。

`create_upload` 幂等：同 `client_ref` 同参数返回同一未用凭据；同
`client_ref` 不同参数 → `-32009`。

面向 LLM agent 的纯文本指南在 **`/llms.txt`**（服务端从代码生成：协议
版本、端点、curl 示例、三种文件形态与两段式上传、计费与实时公开工具
目录，与 `tools/list` 同源）。每页 `<head>` 带
`<link rel="alternate" type="text/plain" href="/llms.txt">` 与
`<link rel="service" href="/mcp">`；首页与 `/agents` 服务端渲染可见的
端点说明（不依赖 JS）。

分享链接对 agent 的语义：`/f/{id}` 是人类页面（响应带
`X-Robots-Tag: noindex, nofollow` 与 `Link: <…?download=1>; rel="enclosure"`
头，页面含普通 `<a>` 直链与 `<link rel=enclosure>`）；`/f/{id}?download=1`
是直接下载（302）。两者均支持 HEAD——HEAD 返回与 GET 一致的状态码与
头、空 body、不消耗下载次数。未知页面返回真实 404 页（含首页/工具/
MCP//llms.txt 出口）；`robots.txt` 不再屏蔽 `/f/`（索引由响应头拒绝），
`/api/`、`/auth/`、`/dh/` 维持 Disallow。

```bash
# 1) create_upload（略去 Bearer 头与 initialize）
curl -s https://your-domain/mcp -H "Authorization: Bearer fsk_…" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,
    "method":"tools/call","params":{"name":"create_upload",
    "arguments":{"filename":"big.png","size":5242880,
                 "client_ref":"stage-big-png"}}}'
# → result.content[0].text = {"upload_url":"https://…/mcp/upload/<ticket>",
#                             "expires_at":…,"max_bytes":…}

# 2) 上传原始字节（URL 即一次性凭据，无需 Authorization）
curl -s -X PUT --data-binary @big.png \
  "https://your-domain/mcp/upload/<ticket>"
# → {"public_id":"…","source_token":"…"}

# 3) 以引用形态调用工具
curl -s https://your-domain/mcp -H "Authorization: Bearer fsk_…" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,
    "method":"tools/call","params":{"name":"share",
    "arguments":{"files":[{"public_id":"…","source_token":"…"}],
                 "client_ref":"share-big-png"}}}'
```

## 用户工具声明协议

用户发布的工具通过一份 JSON manifest 声明。本节描述当前已落地的声明契约：结构、多语言文本、Web 编辑与 API 写入语义、参数校验与错误码。

### LocalizedText 类型

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

### 字段多语言许可表

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
| `format_families` | 七族准入词表单选（image/document/archive/cad3d/media/data/code），accept 由族派生 |
| `files_min` | 输入文件数下限，int ≥ 0（0 = 纯文本/参数工具，可不带文件调用）；缺省 1 |
| `files_max` | 输入文件数上限，int 1..20；须 ≥ files_min；缺省 1 |
| `outputs` | 产出类型非空子集 `["text","json","file"]`（声明工具可能返回哪些结果项）；缺省 `["file"]` |
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

### 合法 manifest 示例

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

### 读取规则（canonical fallback）

全平台唯一的 LocalizedText 回落链：

```
requested locale → en → 任意第一个非空值 → ""
```

- Python `resolve_label()` 与 JS `resolveLabel()` 是该算法在两个 runtime 的等价实现，由协议回归测试保证行为一致。
- 调用方可在 resolver 返回后追加业务兜底（如工具名回落 `tool_id`）——这属于调用方，不属于 resolver。
- 其余调用方禁止再手写第二套 locale 回落链。
- 搜索例外：市场搜索索引（`data-name`）故意拼接所有语言的值，跨语言搜索是设计行为，不走单语言 resolver。

### 写入语义：Web 编辑 vs API

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

### params 写入契约

机器结构（参数/选项的增删改）在两种上下文下都以提交的 params 为准：parameter 按 `key` 对齐，option 按 `value` 对齐，删除即结构删除（其翻译随结构删除）。

差异在三个 LocalizedText 叶子（`param.label` / `param.placeholder` / `options[].label`）：

- **Web Form（携头）**：对旧声明做当前 locale merge，保留其他 locale 文本。禁止按数组 index 合并。
- **API / manifest（无头）**：显式全集替换，不做 locale merge——提交什么存什么。

### 工具参数错误码

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

### 执行契约

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
- 响应必须是 JSON 的 items 数组（唯一形态；IO-1）：

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

### 协议原则

**结构单份，文本多形态。** locale 停留在文本叶子节点，不上升为 schema 维度：没有 `default_locale`、没有 `tool.locale`、没有 per-language manifest。Agent 与 MCP 消费的是稳定结构；人类看到的翻译只存在于 LocalizedText 字段里。

## 部署架构

```
用户 → Cloudflare → nginx → uvicorn:8000 (FastAPI)
                                  ├── PostgreSQL（账本/用户/工具）
                                  └── Gokapi（文件存储）
```

## License

[GNU Affero General Public License v3.0](LICENSE)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。
