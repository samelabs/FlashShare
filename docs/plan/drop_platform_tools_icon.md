# Release migration: DROP platform_tools.icon

存量 production 库在 1.6 图标机制剥离批部署时执行**一次**；执行后删除本资产
（仓库规则：docs/plan/ 仅存放尚未执行的 release migration 资产）。

- 背景：工具图标 = 输入类型派生（`governance/catalog._display_icon` 单点），
  `platform_tools.icon` 列自 1.5 起零读者；剥离批已删除全部代码写入源与新库 DDL。
- 列值恒为旧 DDL 默认 `'wand'`（写源只剩默认值填充），无数据需要备份。
- startup `init_db()` 不执行本迁移（只跑 current-schema CREATE IF NOT EXISTS）。

```sql
ALTER TABLE platform_tools DROP COLUMN icon;
```

无需其他前置检查：accept 未声明（=all，全部平台格式）的文件工具由类型映射
渲染 `cloud`（内置仅 share 以此形态发布；operator 表单留空的"全部兼容"工具
同属 all），不存在会让目录失败的存储形态。

