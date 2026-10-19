# Release migration: DROP platform_tools.icon

**执行顺序（强制）：先部署并重启新代码，确认服务健康后再执行本迁移。**
不可先删列——v1.5 代码的 seed upsert 与建工具 insert 仍写 `icon`
（`EXCLUDED.icon` / NOT NULL DEFAULT），列先消失会让 seeding 与工具创建
直接报 UndefinedColumn。新代码对列零引用，列先在 posterior 无害。

存量 production 库在 1.6 图标机制剥离批部署时执行**一次**；执行后删除本资产
（仓库规则：docs/plan/ 仅存放尚未执行的 release migration 资产）。

- 背景：工具图标 = 声明 tag 的查表（`icons.tool_glyph` 单点，S3 起）；
  `platform_tools.icon` 列自 1.5 起零读者；剥离批已删除全部代码写入源
  与新库 DDL。
- 列值恒为旧 DDL 默认 `'wand'`（写源只剩默认值填充），无数据需要备份。
- startup `init_db()` 不执行本迁移（只跑 current-schema CREATE IF NOT EXISTS）。

```sql
ALTER TABLE platform_tools DROP COLUMN icon;
```

无需其他前置检查：accept 单形状不变量（S4 起）由
`validate_catalog_rows` 在启动时对全部存储行 fail-loud 校验
（文件工具必带非空 accept 列表；纯文本工具无文件位 → NULL）。
