# FlashShare 在线压缩/解压 — 技术设计

## 定位

FlashShare 的衍生能力：用户可以在线压缩和解压 zip。
不是独立工具页，长在现有上传→分享链路上。

---

## 用户场景

### 场景 A：压缩（散文件 → zip）
1. 用户在首页多选文件（照片、文档等）
2. 每个文件独立上传（复用现有上传流程）
3. 上传完成后，页面显示文件列表
4. 用户点「打包下载」或「生成分享链接」
5. 服务端流式生成 zip

### 场景 B：解压（zip → 散文件）
1. 用户上传一个 zip 文件
2. 落地页 `/f/<id>` 检测到 zip 类型，展示文件树
3. 用户可以：
   - 预览单个文件（图片/文本）
   - 下载单个文件
   - 下载全部（整包原文件）

---

## 数据模型

### 现有 uploads 表
已有字段：id, public_id, user_id, file_name, size_bytes, gokapi_id, expire_at, allowed_downloads, download_count, created_at

### 新增字段
```sql
ALTER TABLE uploads ADD COLUMN group_id TEXT DEFAULT NULL;
ALTER TABLE uploads ADD COLUMN is_zip INTEGER DEFAULT 0;
ALTER TABLE uploads ADD COLUMN zip_parent_id INTEGER DEFAULT NULL;
```

- `group_id` — 压缩场景：同一批多文件的标识（UUID）
- `is_zip` — 标记这是一个 zip 文件（解压场景，落地页判断用）
- `zip_parent_id` — 解压后的文件指向原始 zip 的 upload id

### 解压后的文件要不要落库？

**不落库。** 理由：
- 解压是即时行为，用户看完/下完即走
- 落库会产生大量子文件记录，污染 uploads 表
- 过期跟随原始 zip，不需要独立生命周期

替代方案：解压预览纯靠服务端实时读 zip 目录，不存储。

---

## API 设计

### 1. 压缩：多文件上传 + 打包

```
POST /drop-api/upload
```
现有接口不变。新增可选参数 `group_id`：
- 前端生成一个 UUID 作为 group_id
- 同一批次的每个文件都带这个 group_id
- 后端存入 uploads.group_id

```
GET /drop-api/group/<group_id>
```
返回该组所有文件列表（用于上传完成后的文件列表展示）

```
GET /f/<group_id>/download-all
```
流式生成 zip 并返回。后端逻辑：
1. 查 group_id 下所有 uploads
2. 创建 `zipfile.ZipFile` 写入 `io.BytesIO`（小文件）或临时文件（大文件）
3. 逐个从 Gokapi 下载文件流 → 写入 zip
4. 返回 zip

**内存安全策略：**
- 组内文件总大小 < 50MB：内存中 BytesIO
- 组内文件总大小 ≥ 50MB：写临时文件，下载完删除
- 设置硬上限：组内最多 20 个文件，总计不超过 200MB

### 2. 解压：zip 预览 + 单文件提取

```
GET /drop-api/zip-info/<upload_id>
```
返回 zip 内文件列表。后端逻辑：
1. 从 Gokapi 下载 zip 到临时文件
2. `zipfile.ZipFile` 打开，读取 `namelist()` + 每个文件的 `file_size`
3. 返回 JSON：`[{name, size, is_dir}, ...]`
4. 删除临时文件

**注意：不读文件内容，只读 zip 目录头，很快。**

```
GET /f/<id>/extract/<path>
```
提取 zip 内单个文件。后端逻辑：
1. 从 Gokapi 下载 zip 到临时文件
2. `zipfile.read(name)` 读取指定文件
3. 返回文件内容（图片直接展示，文本返回内容，其他触发下载）
4. 删除临时文件

**安全限制：**
- zip 内文件数上限 500（防 zip bomb）
- 单个解压文件上限 50MB（解压后大小）
- 总解压大小上限 200MB
- 拒绝绝对路径和 `..` 路径遍历

---

## 前端改动

### 首页上传区

**多文件选择：**
- `<input type="file" multiple>` 已支持，现有代码只取第一个
- 改为支持多选：遍历 files，逐个上传，显示进度列表
- 每个上传带同一个 group_id

**上传完成后的结果区：**
- 单文件：现有行为不变（链接 + 二维码）
- 多文件：显示文件列表 + 两个按钮：
  - 「打包下载」→ 跳转 `/f/<group_id>/download-all`
  - 「生成分享链接」→ 返回组链接（可选，第一期不做）

### 落地页 `/f/<id>`

**检测到 zip 文件时：**
- 保留现有下载按钮
- 下方新增「在线预览」区域
- JS 调 `/drop-api/zip-info/<id>` 拿文件列表
- 渲染文件树（文件夹折叠 + 文件图标）
- 点击文件：
  - 图片：弹窗预览（`/f/<id>/extract/<path>`）
  - 文本：弹窗显示内容
  - 其他：直接下载

**文件树组件（纯 CSS/JS）：**
```
📁 folder/
  📄 file1.jpg    2.1 MB
  📄 file2.txt    1.2 KB
📄 readme.md      0.8 KB
```

- 路径按 `/` 分割生成树结构
- 文件夹默认折叠，点击展开
- 文件按类型显示图标（复用 EXT_TO_TYPE）

### 文件树渲染逻辑（伪码）
```js
function buildTree(paths) {
  const root = {};
  paths.forEach(p => {
    const parts = p.name.split('/');
    let node = root;
    parts.forEach((part, i) => {
      const isLast = i === parts.length - 1;
      if (isLast && !p.is_dir) {
        node[part] = { file: p };
      } else {
        node[part] = node[part] || {};
        node = node[part];
      }
    });
  });
  return root;
}
```

---

## 安全考虑

1. **Zip bomb 防护**
   - 读取 zip 前检查 `infolist()`，累加所有 `file_size`
   - 总大小超 200MB 拒绝预览
   - 单文件超 50MB 拒绝提取
   - 文件数超 500 拒绝预览

2. **路径遍历防护**
   - 拒绝 `..` 路径
   - 拒绝绝对路径（`/etc/passwd`）
   - `os.path.normpath` + 检查是否在 zip 根目录下

3. **资源限制**
   - 压缩：最多 20 文件，总计 200MB
   - 解压预览：zip 最大 200MB（压缩后）
   - 单次提取：50MB
   - 临时文件用完即删，加 `atexit` 兜底清理

4. **认证**
   - 压缩：需要登录（消耗每日配额）
   - 解压预览：公开（跟现有落地页一样，有链接就能看）

---

## 内存评估（1GB 服务器）

| 操作 | 峰值内存 | 安全 |
|------|----------|------|
| zip 目录预览 | ~5MB（读 namelist） | ✅ |
| 单文件提取 | ~50MB（读单个文件） | ⚠️ 需监控 |
| 多文件打包 | ~50MB（流式，逐个写） | ✅ |
| 同时 2 个用户解压 | ~100MB | ⚠️ |

**结论：** 单用户场景安全。并发 2-3 个解压操作会吃紧。
应对：限制并发解压请求数为 2，超出排队。

---

## 实施计划

### 第一步：多文件上传 + 压缩下载
1. 数据库加 `group_id` 字段
2. 前端改为多文件选择 + 进度列表
3. 后端加 `/f/<group_id>/download-all` 流式打包
4. 上传结果区显示文件列表

### 第二步：zip 解压预览
1. 落地页检测 zip 类型
2. 后端加 `/drop-api/zip-info/<id>` 返回目录
3. 前端渲染文件树
4. 单文件提取 `/f/<id>/extract/<path>`

### 第三步：体验打磨
1. 文件预览（图片/文本）
2. 进度条优化
3. 移动端适配文件树
4. 错误提示完善

---

## 不做的事

- ❌ 不做独立工具页
- ❌ 不做 zip 以外的格式（rar, 7z 需要二进制依赖）
- ❌ 不做压缩级别选择（默认 standard）
- ❌ 不做加密 zip
- ❌ 解压后的文件不独立存储/分享

---

## 风险点

1. **Gokapi 下载 zip 到临时文件** — 每次预览/提取都要从 Gokapi 拉一次完整 zip。大 zip 会慢。可以考虑缓存到 /tmp，设 TTL 5 分钟自动删。
2. **并发内存** — 1GB 机器，需要限制并发数。
3. **group_id 链接** — 需要确认 Gokapi 是否支持按组查询，还是纯靠 SQLite 关联。
