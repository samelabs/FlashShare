# FlashShare 工具区设计

## 核心概念

详情页 `/f/<id>` 是工作台。根据文件类型展示可用工具。
工具不是独立功能页——它长在分享链上，输入是分享链上的文件，输出是新的分享链。

---

## 架构

```
分享链 /f/<id>
  ↓
GET /drop-api/tools/<public_id>  → 按文件类型匹配工具列表
  ↓
用户点击工具
  ↓
TYPE_VIEW/PEEK → POST /drop-api/tool → 直接返回结果（预览/信息）
TYPE_PRODUCE   → POST /drop-api/tool → 处理 → upload_to_gokapi → 新 uploads 记录 → 新分享链
```

工具的产物不是"给用户下载一个文件"，而是走完整的上传流程，产出一条新的分享链。
这是工具和分享链的融合：**分享链是工具的底座，工具是分享链上的操作。**

---

## 工具类型

| 类型 | 行为 | 登录 | 额度 | 输出 |
|------|------|------|------|------|
| **TYPE_VIEW** | 读源文件，直接返回数据给前端展示 | 否 | 否 | 预览/信息 |
| **TYPE_PEEK** | 读源文件 + 处理，结果直接输出前端 | 否 | 否 | 预览 |
| **TYPE_PRODUCE** | 处理后产出新文件，走完整上传流程 | 是 | 是（创建者更新除外） | 新分享链 |

### TYPE_PRODUCE 的 mode

- `mode=create`（默认）：新建分享链，原文件不动
- `mode=update`（仅创建者）：替换原文件的 gokapi 记录，分享链不变

---

## 工具注册协议

```python
register(
    tool_id="unique_id",
    tool_type=TYPE_VIEW,       # view | peek | produce
    label={"en": "Name", "zh": "名称"},
    icon="eye",                # 见 VALID_ICONS
    match=lambda upload: ...,  # 根据 upload 记录判断是否匹配
    handler=handler_fn,        # (upload, params, user) -> result dict
)
```

- `match`：接收 upload dict，返回 bool。根据 content_type / 文件扩展名 / size 判断
- `handler`：执行具体操作，返回结果 dict。produce 类型的结果应包含新文件信息，由调用方写入 gokapi + SQLite

---

## 前端交互

- 工具区位于详情页文件卡片下方
- 匹配到的工具横向排列为图标按钮
- 点击后复用 `toolModal` 弹窗展示结果或操作面板
- TYPE_PRODUCE 未登录时弹出登录引导

---

## 新增工具流程

1. 确认场景真实存在（不是"技术能做到"就做）
2. 评估内存安全（1GB VPS）
3. 在 `tools_registry.py` 注册
4. 线上验证

---

## 依赖

- **Pillow >= 10.0**（图片处理，`apt install python3-pil`，当前 10.2.0）
- 基准内存：2048×2048 RGB 全流程（decode + resize + filter + encode）峰值 ~30MB RSS，安全

---

## 场景工具规划

基于 Pillow 10.2 能力 + 用户实际上传画像（94% 图片）。
每个工具解决一个真实场景，不做"技术展示"。

### 第一批：高确定性场景

#### 1. 截图净化（screenshot_clean）

**场景**：AI 生图 / 截图存相册再复用时，需要剥离元数据和隐写信号。

**原理**：像素级 decode → re-encode，等同于数字截图。EXIF、C2PA、GPS、ICC profile 全部丢弃。

**Pillow 能力**：`Image.open()` → `convert("RGB")` → `save("JPEG", exif=b"")`

| 项 | 值 |
|---|---|
| 类型 | TYPE_PRODUCE |
| 匹配 | `content_type` 以 `image/` 开头 |
| 参数 | `quality`（默认 92） |
| mode | create / update |
| 图标 | `image` |

**流水线**：
```
download from gokapi → Image.open (解码到裸像素，元数据丢弃)
  → exif_transpose (按 EXIF 方向旋转，旋转后丢 EXIF)
  → convert("RGB")
  → 限制长边 ≤ 2048px (thumbnail)
  → save JPEG quality=92 (不写 exif，不写 icc_profile)
  → upload_to_gokapi → 新分享链
```

---

#### 2. 格式转换（convert_format）

**场景**：PNG 截图太大需要转 JPEG 压缩；JPEG 需要转 PNG 保透明；转 WebP 兼顾质量和体积。

**Pillow 能力**：`Image.save(format)` 支持 JPEG / PNG / WebP / GIF 互转

| 项 | 值 |
|---|---|
| 类型 | TYPE_PRODUCE |
| 匹配 | `content_type` 以 `image/` 开头 |
| 参数 | `target_format`（jpeg/png/webp）、`quality`（有损格式用） |
| 图标 | `convert` |

**边界处理**：
- PNG → JPEG：先 `convert("RGB")`（去掉 alpha 通道，白底填充）
- JPEG → PNG：直接转，但文件可能变大
- 任意 → WebP：`quality=90`，通常最小

---

#### 3. 图片压缩（compress_image）

**场景**：手机拍的原图 5MB+，分享前需要缩小到可传输的大小。

**Pillow 能力**：resize + JPEG quality + optimize + progressive

| 项 | 值 |
|---|---|
| 类型 | TYPE_PRODUCE |
| 匹配 | `content_type` 以 `image/` 开头 |
| 参数 | `max_width`（默认 1920）、`quality`（默认 85） |
| 图标 | `compress` |

**流水线**：
```
Image.open → exif_transpose
  → 如果 width > max_width: resize (LANCZOS)
  → save JPEG quality + optimize=True + progressive=True
  → 返回压缩前后体积对比
```

**效果**：典型手机照片 4000×3000 @ 5MB → 1920×1440 @ ~400KB，视觉无损。

---

### 第二批：信息类（TYPE_VIEW，无需登录）

#### 4. 图片信息（image_info）

**场景**：用户想看图片的尺寸、格式、体积、色彩通道、EXIF 概要。

**Pillow 能力**：`Image.size` / `Image.mode` / `Image.format` / `ImageStat` / `_getexif()`

| 项 | 值 |
|---|---|
| 类型 | TYPE_VIEW |
| 匹配 | `content_type` 以 `image/` 开头 |
| 图标 | `info` |

**返回**：尺寸、格式、模式（RGB/RGBA/P/L）、文件体积、色彩统计（mean/extrema）、EXIF 摘要（相机型号、拍摄时间，不含 GPS）。

---

#### 5. 缩略图预览（thumbnail_preview）

**场景**：大图分享链落地页，先看缩略图再决定是否下载。

**Pillow 能力**：`ImageOps.contain` + `thumbnail`

| 项 | 值 |
|---|---|
| 类型 | TYPE_PEEK |
| 匹配 | `content_type` 以 `image/` 开头 |
| 图标 | `eye` |

**返回**：base64 编码的 400px 缩略图，直接在 modal 中展示。

---

### 第三批：调整类（TYPE_PRODUCE，可选）

#### 6. 自动优化（auto_enhance）

**场景**：照片偏暗 / 偏灰，一键优化。

**Pillow 能力**：`ImageOps.autocontrast` + `ImageOps.equalize` + `ImageEnhance`

| 项 | 值 |
|---|---|
| 类型 | TYPE_PRODUCE |
| 匹配 | `content_type` 以 `image/` 开头 |
| 图标 | `edit` |

**流水线**：autocontrast(cutoff=1) → 微增 saturation(1.15) → 微增 sharpness(1.1) → output

---

#### 7. 裁剪旋转（crop_rotate）

**场景**：竖拍的手机照片横过来了；需要裁掉边缘。

**Pillow 能力**：`Image.rotate` / `Image.transpose` / `Image.crop`

| 项 | 值 |
|---|---|
| 类型 | TYPE_PRODUCE |
| 匹配 | `content_type` 以 `image/` 开头 |
| 参数 | `action`（rotate_90 / rotate_270 / flip_h / flip_v）、可选 `crop` box |
| 图标 | `crop` |

---

## 能力 → 场景映射总表

| Pillow 能力 | 对应工具 |
|---|---|
| `Image.open` + `save(exif=b"")` | 截图净化 |
| `Image.save(format=...)` | 格式转换 |
| `resize` + JPEG `quality/optimize` | 图片压缩 |
| `Image.size/mode/format` + `ImageStat` + `_getexif` | 图片信息 |
| `ImageOps.contain` + base64 | 缩略图预览 |
| `autocontrast` + `ImageEnhance` | 自动优化 |
| `rotate` / `transpose` / `crop` | 裁剪旋转 |
| `quantize` / `posterize` | （未规划场景，储备） |
| `ImageFilter` 系列 | （未规划场景，储备） |
| `ImageDraw` / `ImageFont` | （水印/标注，未规划，储备） |
