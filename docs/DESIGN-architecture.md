# 架构规范：工具原子化 × 分享链解耦

工具和分享链是两个独立的原子系统，通过接口契约连接。
各自可独立演进、替换实现。本文档定义边界和契约。

---

## 一、分层全景

```
┌──────────────────────────────────────────────────┐
│                     前端                          │
│  首页 / 详情页 / API 消费方                        │
├────────────┬─────────────────────────────────────┤
│  工具链     │            分享链                     │
│            │                                     │
│  Tool 对象  │  Upload Pipeline                    │
│  Registry  │  Storage Adapter (gokapi / ...)     │
│  Handler   │  Record Store (SQLite / ...)        │
│            │                                     │
├────────────┴─────────────────────────────────────┤
│                   接口层                           │
│  ToolInput ← Storage.read(gokapi_id)             │
│  Storage.write(result) → ToolOutput              │
├──────────────────────────────────────────────────┤
│                   基础设施                         │
│  Gokapi (存储)  SQLite (记录)  Pillow (处理)      │
└──────────────────────────────────────────────────┘
```

---

## 二、工具原子化

### 2.1 Tool 对象

工具是一个自包含的对象，不知道分享链的存在。
它只负责：拿到文件字节 → 处理 → 交回文件字节。

```python
# 一个工具的完整定义
{
    "id": "privacy",
    "type": "produce",
    "label": {"en": "Privacy", "zh": "隐私"},
    "icon": "image",
    "accept": {"image/png", "image/jpeg", "image/webp"},
    "presets": [...],
    "handler": callable,
}
```

### 2.2 handler 契约（原子接口）

handler 是工具的核心。它的输入输出是**纯数据**，不含任何基础设施耦合：

```python
def handler(input: ToolInput, params: dict) -> ToolOutput:
    """
    input:  源文件的字节流 + 元信息（filename, content_type, size）
    params: 用户选择的参数（preset, expiry_days, allowed_downloads）
    output: 处理结果的字节流 + 元信息
    """
```

**输入（ToolInput）**：

```python
ToolInput = {
    "stream": <readable file-like>,   # 源文件字节流（来自存储层）
    "filename": "photo.png",
    "content_type": "image/png",
    "size": 2003000,
}
```

**输出（ToolOutput）**：

```python
ToolOutput = {
    "stream": <readable file-like>,   # 结果字节流（交给存储层）
    "filename": "photo_clean.jpg",
    "content_type": "image/jpeg",
    "size": 347000,
}
```

**关键**：handler 不接触 gokapi、不接触 SQLite、不接触 HTTP。
它只做 "bytes in → process → bytes out"。
上传结果到存储、写记录、生成分享链——都是分享链的职责。

### 2.3 handler 内部结构

```python
def privacy_handler(input, params):
    # 1. 格式验证（安全边界）
    if not input["content_type"].startswith("image/"):
        raise ToolError("err_format_not_supported")

    # 2. 参数解析（安全边界，静默回退）
    preset = params.get("preset", "standard")
    p = PRESETS.get(preset, PRESETS["standard"])

    # 3. 处理（工具核心逻辑）
    img = Image.open(input["stream"])
    # ... noise → blur → usm → resample → save ...

    # 4. 返回结果字节流（不碰存储）
    output_buf = io.BytesIO()
    img.save(output_buf, "JPEG", quality=90, progressive=True)
    output_buf.seek(0)

    return {
        "stream": output_buf,
        "filename": "privacy_" + input["filename"].rsplit(".", 1)[0] + ".jpg",
        "content_type": "image/jpeg",
        "size": len(output_buf.getvalue()),
    }
```

### 2.4 工具的公开 API 属性

工具注册的信息天然可以暴露为公开 API：

```
GET /api/tools            → 工具清单（声明信息）
GET /api/tools/{id}       → 单个工具详情（声明信息）
POST /api/tools/execute   → 执行工具
```

（注：GET /api/tools 与 /api/tools/{public_id} 已随前端改造移除，工具清单现由服务端渲染注入；execute 为现行唯一工具端点。）

未来开放 REST API 时，第三方可以直接：
1. 拉工具清单
2. 选工具 + 上传文件
3. 拿到处理结果

工具对象的自包含性使这成为可能——不依赖 session、不依赖页面上下文。

---

## 三、分享链解耦

### 3.1 分享链的三个独立环节

```
上传 → 存储 → 记录 → 分享链
  ↑      ↑      ↑
  |      |      |
 接收    存储    持久化
 （HTTP） （Adapter）（Store）
```

每个环节是独立组件，可替换：

### 3.2 存储层抽象（Storage Adapter）

```python
class StorageAdapter:
    """存储抽象。Gokapi 是一个实现，未来可以有 S3 / R2 / 本地。"""

    def write(self, stream, filename, content_type,
              expiry_days, allowed_downloads, size) -> dict:
        """上传文件到存储，返回存储元信息。"""
        raise NotImplementedError

    def read(self, file_id: str) -> tuple:
        """从存储读取文件，返回 (status, headers, stream)。"""
        raise NotImplementedError

    def delete(self, file_id: str) -> None:
        """从存储删除文件。"""
        raise NotImplementedError
```

当前实现：

```python
class GokapiStorage(StorageAdapter):
    def write(self, ...):
        return upload_to_gokapi(...)
    def read(self, ...):
        return open_gokapi_download(...)
    def delete(self, ...):
        return delete_gokapi_file(...)
```

**关键**：gokapi 是存储的一个原子实现，不是分享链的硬编码依赖。
切换存储后端只需实现新的 Adapter，不改分享链逻辑。

### 3.3 记录层抽象（Record Store）

```python
class RecordStore:
    """记录抽象。SQLite 是一个实现，未来可以有 PostgreSQL / Redis。"""

    def create(self, user_id, ip, public_id, storage_id, filename,
               content_type, size, expiry_days, allowed_downloads,
               expire_at, url_download, created_at) -> None:
        """写入一条分享记录。"""
        raise NotImplementedError

    def get_by_public_id(self, public_id: str) -> dict:
        """按 public_id 查询记录。"""
        raise NotImplementedError

    def list_by_user(self, user_id: int) -> list:
        """列出用户的分享记录。"""
        raise NotImplementedError

    def delete(self, record_id: int) -> None:
        """删除记录。"""
        raise NotImplementedError
```

当前实现：SQLite + database.py 的 db() 连接。

### 3.4 上传流水线（Pipeline）

上传是多步骤的流水线，每步独立可测试：

```python
def upload_pipeline(stream, filename, content_type, size, 
                    expiry_days, allowed_downloads, user, ip):
    """完整上传流水线。每步是独立函数，可单独测试和替换。"""

    # Step 1: 安全校验
    valid, final_type = sniff_allowed(filename, content_type, sample)
    if not valid:
        raise UploadError(final_type)

    # Step 2: 配额检查
    ok, reason = check_quota(user["id"], ip, size)
    if not ok:
        raise UploadError(reason)

    # Step 3: 存储写入（通过 Adapter）
    result = storage.write(stream, filename, final_type,
                           expiry_days, allowed_downloads, size)
    info = result.get("FileInfo", {})

    # Step 4: 记录写入（通过 Store）
    public_id = generate_public_id()
    records.create(user_id=user["id"], ip=ip, public_id=public_id, ...)

    # Step 5: 返回分享链
    return {
        "public_id": public_id,
        "url": public_file_url(public_id),
        "expire_at": info.get("ExpireAt"),
    }
```

未来可以 worker 化：Step 3-4 移到后台 worker，HTTP 只负责接收。
因为流水线每步是独立函数，worker 化只需改调度方式，不改业务逻辑。

---

## 四、接口层：工具 × 分享链

工具和分享链在接口层对接。**工具不知道分享链，分享链不知道工具内部**。

```
分享链持有源文件
  → 调用工具 handler（传入 ToolInput）
  → 工具返回 ToolOutput（结果字节流）
  → 分享链把结果走自己的流水线（存储 → 记录 → 分享链）
```

编排代码（在 main.py 的 execute 层，不在 handler 内）：

```python
def execute_and_share(tool, upload, params, user):
    """工具和分享链的对接点。"""

    # 1. 从存储读取源文件（分享链职责）
    _, _, stream = storage.read(upload["gokapi_id"])

    # 2. 构造 ToolInput（接口层转换）
    tool_input = {
        "stream": stream,
        "filename": upload["file_name"],
        "content_type": upload["content_type"],
        "size": upload["size_bytes"],
    }

    # 3. 调用工具 handler（工具职责，纯计算）
    tool_output = tool["handler"](tool_input, params)

    # 4. 把结果走分享链流水线（分享链职责）
    result = upload_pipeline(
        stream=tool_output["stream"],
        filename=tool_output["filename"],
        content_type=tool_output["content_type"],
        size=tool_output["size"],
        expiry_days=params.get("expiry_days", 1),
        allowed_downloads=params.get("allowed_downloads", 50),
        user=user,
        ip=upload.get("ip", ""),
    )

    # 5. 返回新分享链
    return {
        "type": "share_link",
        "url": result["url"],
        "filename": tool_output["filename"],
        "size": tool_output["size"],
        "orig_size": upload["size_bytes"],
    }
```

**关键**：
- handler 不碰 gokapi，不碰 SQLite
- upload_pipeline 不知道文件来自工具还是用户直传
- 两边通过 ToolInput/ToolOutput 数据结构对接
- 任一方可以独立替换

---

## 五、依赖方向

```
前端 → API 层 → 编排层（execute_and_share）
                    ↓          ↑
              工具 handler    分享链 pipeline
              （纯计算）      （存储 + 记录）
                    ↓          ↑
               ToolOutput    Storage Adapter
                              Record Store
                    ↓          ↓
                （无）      Gokapi    SQLite
```

依赖方向**单向向下**：
- handler 不依赖存储/记录
- 存储不依赖 handler
- 编排层依赖两者，但通过接口

**禁止的依赖**：
- handler import gokapi ❌
- handler import database ❌
- handler import main ❌
- 存储层 import tools ❌

---

## 六、首个工具验证

privacy 工具按此规范实现：

- handler 只接收 ToolInput，返回 ToolOutput
- handler 内部只用 Pillow，不碰任何 I/O 基础设施
- 分享链的 upload_pipeline 负责把结果存入 gokapi + 写 SQLite
- 编排层 execute_and_share 连接两者

验证标准：把 gokapi 换成 mock storage，handler 代码一行不改。
把 SQLite 换成内存 dict，handler 代码一行不改。
