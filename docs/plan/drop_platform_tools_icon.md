# Release migration: DROP platform_tools.icon

**执行归属：1.6 发布窗口主顺序 `release_1_6_audit_closure.md` 的阶段 ⑤
（切换候选首次启动并确认健康之后）**。不可先删列——v1.5 代码的 seed
upsert 与建工具 insert 仍写 `icon`（`EXCLUDED.icon` / NOT NULL
DEFAULT），列先消失会让 seeding 与工具创建直接报 UndefinedColumn。
新代码对列零引用，列先在 posterior 无害。

**回滚边界（与主顺序阶段 ⑤ 一致）**：本 DROP 执行后，`checkout 旧
SHA + restart` 不再可用（旧代码写 `icon`、读 `trusted`，两列已删必然
UndefinedColumn）；此后唯一回滚手段是从阶段 ① 已验证可恢复的备份恢复
数据库，是否走由 owner 裁定。

生产执行完成并通过阶段 ⑥ 部署后检查后，随主顺序资产一并删除
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
