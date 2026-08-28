# A1 DB 触点审计 — 施工图（2026-08-17）

## 1. 表清单（现状 SQLite）

| 表 | 行数级 | 说明 |
|---|---|---|
| users | ~百级 | 本地账号+X OAuth 绑定 |
| sessions | ~百级 | 登录会话（token_hash 主键） |
| oauth_states | 临时态 | OAuth state 暂存，随过期清理 |
| uploads | ~千级 | 文件元数据（public_id 唯一索引） |

posts 表已在上轮重构中删除，无残留。

## 2. db() 工厂

- `sqlite3.connect(DB_PATH)` 每调用建连 + WAL + FK ON + Row factory
- 消费形态统一：25 处 `with db() as con:`（sqlite3 context manager = 事务块，退出提交/回滚）；**零直连形态**
- 无 executemany、无 isolation_level 定制、无手工 BEGIN/COMMIT

## 3. 调用点矩阵（文件 × db() 次数）

| 文件 | with块 | SQL 面向的表 |
|---|---|---|
| core/auth.py | 13 | users, sessions(读) |
| core/uploads.py | 5 | uploads |
| app/routes/auth_route.py | 3 | sessions, oauth_states |
| app/routes/api.py | 2 | uploads |
| app/main.py | 1 | init_db() |
| core/database.py | — | DDL + 列检查 |
| core/utils.py | — | generate_public_id(con) 吃连接查 uploads |

## 4. 方言敏感点（PG 迁移必改清单）

| # | 点 | 位置 | PG 对策 |
|---|---|---|---|
| D1 | `?` 占位符 ×全部语句 | 全部 | psycopg 用 `%s`（迁移时全量替换） |
| D2 | `cur.lastrowid` | auth.py:71,233 | PG: `INSERT ... RETURNING id` |
| D3 | `AUTOINCREMENT` | users/uploads DDL | PG: `GENERATED ALWAYS AS IDENTITY`（或 BIGINT） |
| D4 | `PRAGMA journal_mode/foreign_keys` | database.py:12-13 | PG 无此概念，连接层删除 |
| D5 | `sqlite_master` 表存在检查 | database.py:80,138,146 | PG: `information_schema.tables` / `to_regclass` |
| D6 | `PRAGMA table_info` 列检查 | database.py:84,139,147 | PG: `information_schema.columns` |
| D7 | `executescript` 多语句 | database.py | psycopg 不支持 executescript → 逐条 execute 或 script 模式 |
| D7b | 老库列修复逻辑（users 重建/oauth bind列/public_id 回填） | init_db 大段 | **PG 全新库不需要**——迁移脚本一次性做完，init_db 瘦身为纯 CREATE IF NOT EXISTS |
| D8 | sqlite3.Row 键访问 ×38 处 | 全部调用点 | psycopg RealDictCursor / dict_row |
| D9 | `with con:` 事务语义 | 25 处 | psycopg 事务需显式管理：连接层包装（commit/rollback/close） |
| D10 | INTEGER 时间戳(秒) | 全表 | 保持 BIGINT 秒级不变（不迁 timestamptz，避免行为漂移） |
| D11 | generate_public_id(con) 直吃连接 | utils.py:30 | 保持签名，psycopg 连接同样支持 .execute().fetchone() |

## 5. 行为契约（迁移后必须逐字节一致）

- 注册/登录/会话/X绑定/资料修改 → users/sessions 行为不变
- 上传（含 source_token 校验链）/删除/重命名 → uploads 行为不变
- public_id 生成与唯一性 → 不变
- init_db 幂等（重复启动安全）→ 不变

## 6. A3 连接层改造方案（预告）

database.py 重写为 PG 版：
- `db()` → psycopg 连接工厂（env DATABASE_URL），contextmanager 包装：yield conn；成功 commit / 异常 rollback / finally close
- dict_row 行工厂对应 sqlite3.Row
- `?`→`%s` 全量替换发生在调用点文件（不是运行时翻译——不留翻译层，代码即终态）
- init_db 瘦身：只做 CREATE TABLE IF NOT EXISTS（PG 版 DDL）+ 过期清理，历史修复逻辑删除
