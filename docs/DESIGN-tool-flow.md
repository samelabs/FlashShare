# 工具流规范

工具 + 分享链是本站核心。工具不是附加功能，是分享链上的处理环节。

本文档是工具系统的**唯一规范**。后续开发必须遵守。

---

## 一、架构原则

### 1.1 工具与分享链解耦

工具是原子对象：handler 只做 `bytes in → process → bytes out`。
分享链是独立管线：上传 → 存储 → 记录 → 分享链。
两者通过 ToolInput / ToolOutput 接口对接，互不依赖。

```
编排层 (_execute_and_share)
  ├── 工具 handler（纯计算，零 I/O 依赖）
  └── 分享链 pipeline
        ├── Storage Adapter（storage.py → gokapi）
        └── Record Store（uploads.py → SQLite）
```

**handler 永远不 import gokapi / database / storage / main。**

### 1.2 工具图标体系

全局通用图标在 `icons.py` 统一管理（单一数据源）。
工具图标属于**工具定义**，在 `VALID_ICONS` 中声明可用名称，
具体 path 从 `icons.py` 取。

```python
# tools.py
VALID_ICONS = {"upload", "image", "wand", "shield", "compress", ...}

# icons.py
_ICONS = {"upload": '<path ...>', "wand": '<path ...>', ...}
```

**禁止**在 app.js、views.py 里手绘工具图标的 path。
所有 SVG path 只存在于 `icons.py`。

### 1.3 工具命名

- `tool_id`：英文稳定标识（`privacy`、`beautify`），不变
- 显示名：走 i18n（`t(tool_id)`），每种语言有自己的翻译
- 描述：走 i18n（`t(tool_id + "_desc")`）

**禁止**在工具注册的 label 字典里维护显示名。
label 字典只作为 API fallback。真正的显示名在 i18n.py / i18n.json。

---

## 二、工具注册协议

### 2.1 register() 签名

```python
register(
    tool_id,       # str: 唯一标识，小写+下划线，不变
    tool_type,     # TYPE_PRODUCE（产出新文件）
    label,         # dict: {lang: name}，API fallback 用
    icon,          # str: VALID_ICONS 中的名称
    match,         # callable(upload) -> bool（详情页用，首页不用）
    handler,       # callable(tool_input, params) -> tool_output
    accept=None,   # set|None: MIME 集合，None=接受全部格式
    presets=None,  # list|None: 档位声明
)
```

### 2.2 handler 契约

```python
# 输入
tool_input = {
    "stream":       BytesIO,        # 可 seek 的文件流
    "filename":     str,            # 原始文件名
    "content_type": str,            # MIME 类型
    "size":         int,            # 字节数
}

# 输出
tool_output = {
    "stream":       BytesIO,        # 处理后的文件流
    "filename":     str,            # 输出文件名
    "content_type": str,            # 输出 MIME
    "size":         int,            # 输出字节数
}
```

handler 内部必须：
1. 验证 content_type（安全边界，不信任前端）
2. 解析 preset 参数（静默回退默认值）
3. 纯计算处理（不碰 gokapi / DB / HTTP）
4. 返回 tool_output

### 2.3 presets 声明

```python
presets=[
    {"id": "light",    "label": {"en":"Light",...}, "default": False},
    {"id": "standard", "label": {"en":"Standard",...}, "default": True},
]
```

有且仅有一个 `default=True`。
**preset ID 必须全局唯一**——推荐加工具前缀（如 `ico_standard`、`ico_web`），避免跨工具 key 冲突。
`list_all_tools()` 输出保留 `default` 字段供前端选中默认档。

### 2.4 accept 与格式联动

```python
# 工具声明接受的格式
accept={"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"}

# None = 接受所有格式（share 工具）
accept=None
```

前端联动（选中工具时同时触发）：
1. `file.accept = tool.accept.join(",")` — 文件选择器过滤
2. `.drop-formats` 标签收窄为工具支持的格式
3. 下方显示工具说明（`tool_id + "_desc"`）

取消工具时恢复全量。

---

## 三、控制点全景

```
[CP1 注册]     工具声明格式约束、类型、参数（启动时）
     ↓
[CP2 入口]     服务端渲染工具标签到 HTML（同步，无异步）
     ↓
[CP3 浮层]     用户点工具标签 → 浮层展示描述+模式+确认/取消
     ↓
[CP4 上传]     现有安全链路：白名单 → 嗅探 → 配额（不改）
     ↓
[CP5 准入]     文件存在 + 工具存在 + 登录墙
     ↓
[CP6 handler]  storage.read → handler → storage.write → DB INSERT
     ↓
[CP7 输出]     返回新分享链 + contentType，前端展示结果卡片
```

### 安全边界 vs 体验优化

| 是体验优化（可绕过） | 是安全边界（不可绕过） |
|---|---|
| CP3 accept 文件选择器过滤 | CP4 扩展名白名单 + magic bytes 嗅探 |
| | CP5 登录墙（PRODUCE 需登录） |
| | CP6 handler content_type 验证 |
| | CP6 参数校验（静默回退） |

---

## 四、前端交互规范

### 4.1 工具标签（tool-tab）

服务端渲染，不是 JS 动态创建。同步输出到 HTML，零抖动。

```html
<div class="tool-tab" data-tool-id="privacy" role="button" tabindex="0">
  <svg>...</svg>
  <span>图片净化</span>
</div>
```

样式：轻量标签（无边框），选中时 `accent-soft` 背景。
**不是按钮**——视觉上和提交按钮区分。

### 4.2 工具浮层（toolOverlay）

所有工具共用一个浮层组件。点击工具标签 → 弹出浮层。

浮层结构：
```
[图标] 工具名                         [✕]
工具描述文字

处理强度（如有 presets）
○ 模式1  ● 默认模式  ○ 模式3

[取消/取消工具]                    [确认]
```

**浮层规则**：
- 始终显示确认 + 取消两个按钮（无论工具是否已激活）
- 确认：应用选中的模式，写入 localStorage
- 取消（未激活）：关闭浮层
- 取消工具（已激活）：清除工具选择，恢复默认
- 关闭（✕ / 点遮罩）：关闭浮层，不改变当前状态

### 4.3 两步串行编排

```
Step 1: 上传（XHR POST /drop-api/upload）
  → 进度条 + 百分比 + 速度
  → 完成后拿到 publicId + downloadUrl

Step 2: 工具执行（fetch POST /drop-api/tool）
  → spinner + "处理中..."
  → 完成后拿到新分享链
```

无工具时只执行 Step 1，直接展示结果。

### 4.4 结果卡片

```
┌─────────────────────────────────┐
│            [✓ 绿色圆圈]           │
│            完成                   │
│      原始大小 → 输出大小 · 过期   │
│                                  │
│  [⬇ 下载] [📤 分享] [📋 复制]    │
│  [QR] [𝕏]                        │
└─────────────────────────────────┘
```

- 下载：`<a download>` 直接触发浏览器下载
- 分享图片（仅图片文件）：Web Share API（`navigator.share({files})`）
  - 手机端：呼起系统分享面板（保存到相册/发微信/短信）
  - 不支持时 fallback 到下载
- 复制：`navigator.clipboard.writeText`，✓ 反馈 2s
- QR：内联 modal 弹窗（不是新页面）
- X：新标签打开 Twitter 分享

### 4.5 语言包规范

所有工具相关文案走 `t()` 函数，数据源统一到 i18n.json。

| key 来源 | 说明 |
|---|---|
| `t(tool_id)` | 工具显示名 |
| `t(tool_id + "_desc")` | 工具描述 |
| `t(preset_id)` | 模式名（如 `t("aurora")`） |
| `t("intensity")` | 浮层"处理强度"标签 |
| `t("confirm")` / `t("cancel")` | 浮层按钮 |
| `t("cancelTool")` | 取消工具按钮 |
| `t("processing")` | 处理中文案 |
| `t("downloadFile")` | 下载按钮（注意驼峰） |
| `t("shareImg")` | 分享图片按钮 |

**禁止**用服务端 key（如 `download_file`）在客户端 `t()` 里查。
客户端 i18n.json 用驼峰命名（`downloadFile`），服务端 i18n.py 用下划线（`download_file`）。

### 4.6 localStorage 记忆

| key | 值 | 时机 |
|---|---|---|
| `flashshare_tool` | tool_id | 确认工具时写入，取消时清除 |
| `flashshare_tool_preset` | preset_id | 确认工具时写入，取消时清除 |

页面加载时恢复上次选择。

---

## 五、编排层（_execute_and_share）

```python
def _execute_and_share(self, tool, upload, params, user):
    # 1. storage.read → 可 seek 的 BytesIO
    # 2. 构造 ToolInput
    # 3. tool["handler"](tool_input, params) → tool_output
    # 4. 解析生命周期参数（expiry_days, allowed_downloads）
    # 5. storage.write(tool_output) → gokapi 上传
    # 6. DB INSERT → 新 uploads 记录
    # 7. 返回 share_link + contentType + filename + size + orig_size
```

返回结构必须包含 `contentType` 字段，前端据此判断是否显示图片分享按钮。

---

## 六、图标体系

### 6.1 单一数据源

`icons.py` 是所有 SVG path 的唯一来源。

```
icons.py
  ├── Python: icon_svg("download") → <svg>...</svg>
  ├── Python: icon_path("download") → <path>...</path>
  └── JS: window.__ICONS__["download"] → <path>...</path>
```

### 6.2 调用方式

| 场景 | 调用 |
|---|---|
| views.py 服务端渲染 | `icon_svg("download")` |
| views.py 取 path | `_icon_path("download")` |
| utils.py 旧函数 | 委托到 `icons.py`（`download_icon_svg() → icon_svg("download")`） |
| app.js 客户端 | `svgIcon("download")` 或 `window.__ICONS__["download"]` |

### 6.3 图标语义对照

| 名称 | 语义 | 用在哪里 |
|---|---|---|
| upload | 云+向上箭头 | 输入框、提交按钮、share 工具 |
| download | 向下箭头+底线 | 下载按钮 |
| download_count | 托盘+向下箭头 | 额度：最大下载次数 |
| upload_count | 托盘+向上箭头 | 额度：每日上传次数 |
| bandwidth | 双向箭头（上↑下↓） | 额度：每日流量 |
| file_size | 文件+折角 | 额度：最大文件大小 |
| expiry | 时钟 | 额度：最大有效期 |
| share | 三圆点连线 | 分享按钮 |
| copy | 双卡片 | 复制按钮 |
| qr | QR 图案 | QR 按钮 |
| check | 对勾 ✓ | 成功反馈 |
| close | X | 关闭按钮 |
| wand | 魔法棒 | 截图美化工具 |
| image | 图片+山+太阳 | 图片净化工具 |
| brand | 闪电 | 品牌标识 |

### 6.4 禁止事项

- 禁止在 app.js、views.py、tools_registry.py 里手绘 SVG path
- 禁止引入外部图标库（Heroicons/Lucide 等）
- 禁止同一个图标有多个版本的 path
- 新增图标只需在 `icons.py` 的 `_ICONS` 字典加一行

---

## 七、现有工具

### 7.1 share（默认 / 兜底）

| 属性 | 值 |
|---|---|
| tool_id | `share` |
| 显示名 | 无损分享 / Lossless Share |
| icon | `upload` |
| accept | None（全部格式） |
| presets | 无 |
| handler | identity（原样返回） |
| 特殊 | 点击不弹浮层，直接选中；取消其他工具回落到此 |

### 7.2 privacy（图片净化）

| 属性 | 值 |
|---|---|
| tool_id | `privacy` |
| 显示名 | 图片净化 / Clean |
| icon | `shield` |
| accept | jpeg, png, webp, gif, bmp, tiff |
| presets | light / standard(默认) / strong / max |
| handler | noise → blur → usm → resample → clean JPEG |

### 7.3 beautify（截图美化）

| 属性 | 值 |
|---|---|
| tool_id | `beautify` |
| 显示名 | 截图美化 / Frame |
| icon | `wand` |
| accept | jpeg, png, webp, gif, bmp, tiff |
| presets | clean / dark / sunset / ocean / aurora(默认) / noir / royal / grid / wallpaper |
| handler | 圆角 + 阴影 + 背景 = 输出 PNG |

### 7.4 compress（图片压缩）

| 属性 | 值 |
|---|---|
| tool_id | `compress` |
| 显示名 | 图片压缩 / Compress |
| icon | `compress` |
| accept | jpeg, png, webp, gif, bmp, tiff |
| presets | 1mb / 2mb(默认) / 5mb / 10mb |
| handler | 目标大小驱动：二分搜索 quality + 缩尺寸兜底 |

### 7.5 ico（图标生成）

| 属性 | 值 |
|---|---|
| tool_id | `ico` |
| 显示名 | 图标生成 / ICO Favicon |
| icon | `convert` |
| accept | png, jpeg, gif, bmp, webp, tiff, ico |
| presets | ico_standard(默认) / ico_web / ico_app / ico_single |
| handler | 居中裁正方形 → RGBA → 多尺寸 ICO |

### 7.6 新增工具 checklist

1. `tools_{name}.py`：实现 handler（纯 Pillow，零 I/O）
2. `tools.py` VALID_ICONS：加图标名（如已在则跳过）
3. `icons.py` `_ICONS`：加 SVG path
4. `main.py`：`import tools_{name}`
5. `i18n.py` + `i18n.json`：加 `tool_id`、`tool_id + "_desc"`、各 preset_id
6. `views.py` `_TOOL_SVGS`：已删除，无需改（服务端从 `_icon_path` 取）
7. `app.js`：无需改（浮层和标签从 API + i18n 自动渲染）
8. `sw.js` CACHE_NAME +1，views.py `sw.js?v=N` +1
9. `tools_{name}.py` 文件权限 `chown ubuntu:ubuntu`

---

## 八、性能约束

| 指标 | 限制 |
|---|---|
| 单图处理 | < 5s（2048px 长边） |
| 峰值内存 delta | < 100MB |
| 输入上限 | 2048px 长边（handler 内部 thumbnail） |
| storage.read | 返回 BytesIO（可 seek） |
| 输出格式 | JPEG（净化）或 PNG（美化） |
