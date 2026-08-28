# 历史文档存档（SQLite 时代，2026-08-15 前后）

以下文档描述的架构（SQLite/双 i18n/本地 docker 三端流程）已被整体取代：
- 数据库 → PostgreSQL（DATABASE_URL / psycopg2）
- i18n 双系统 → 单源 core/i18n.py → 生成 static/i18n.js
- 三端开发 → 服务器直开发

仅作历史参考，**不可作为开发依据**。现行规范见 /AGENTS.md。
