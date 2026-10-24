# Release migration: 1.6 审计收口（查询索引 + trusted 列退役）

**本资产是 1.6 发布窗口的主顺序文档**：同窗口的另外三份 `platform_tools`
资产（`add_platform_tools_format_family.md`、`add_platform_tools_body_columns.md`
、`drop_platform_tools_icon.md`）在本顺序的对应阶段内执行，各自的步骤细节
以资产为准。四份资产保留到**生产执行完成且部署后检查通过**之后一并删除
（仓库规则：docs/plan/ 仅存放尚未执行的 release migration 资产）。

## 1.6 发布窗口统一分阶段顺序（强制，一条线）

**① 只读预检 + 备份恢复验证 → ② 停止旧服务写入 → ③ 首启前迁移（九个
查询索引 + format/body 加列回填 + 切换前终检）→ ④ 切换候选首次启动 →
⑤ 补 FK、删除 icon/trusted → ⑥ 二次启动 + 部署后检查。**

### ① 只读预检 + 备份恢复验证（旧服务仍在运行）

执行本资产的「部署前只读预检」与另外两份加列资产的预检；确认当日备份
存在且 `restore-proof` 通过（备份不可恢复 = 无回滚手段 = 不具备部署条件）。
同时确认 Nginx 已按部署指南第 3 步设置 `proxy_request_buffering on`（对旧服务无影响，可提前做）。

**任一预检不通过 → STOP 交 PM 裁定**；尤其余额快照一致性（预检 c）、
未知 ledger kind（预检 b）、trusted 兼容（预检 a）三项：不通过即停止部署，
**不得自动改账、不得删数据、不得静默回退**——它们说明库与预期形状不符，
需要人工核对后重新决策。

### ② 停止旧服务写入

`systemctl stop flashshare-api`。此后到 ④ 首启完成之间是停写窗口：窗口内
不得再以旧代码写入（社区发布/编辑、运营建改都不会发生）；窗口内确需运维
写入 `platform_tools` 的，写入后必须重跑 ③ 的回填与终检（幂等）。

### ③ 首启前迁移（停写窗口内，新代码首次启动之前）

新代码的 startup 依赖这些形状：`validate_catalog_rows` 校验 tag/body 列、
seed 写 `format_family`；列不存在或未回填则新代码起不来。索引在此预建后，
新启动的 `CREATE INDEX IF NOT EXISTS` 为空操作，不在大表上同步建索引。

本阶段的 Python 脚本先加载生产环境变量再执行（脚本导入 config，缺必填变量会直接报错）：
`cd /opt/flashshare && set -a && . /etc/flashshare.env && set +a && PYTHONPATH=core venv/bin/python …`

1. **九个查询索引**：逐条独立执行（不在事务中；`CONCURRENTLY` 建立不锁写
   ——本窗口无流量，仍按资产原样执行）。见下方「① 建索引」。
2. **format_family 加列 + 全量回填 + tag 预检**：按
   `add_platform_tools_format_family.md` 的 ① 加列 → ② 回填（含 accept
   物化）→ tag 预检。该资产的步骤 ③（补 FK）**不在本阶段**——FK 引用的
   tags 表由新代码首启创建，补 FK 属于阶段 ⑤。
3. **body 两列加列 + 回填 + 切换前终检**：按
   `add_platform_tools_body_columns.md` 的 ①② 与两份终检 SQL（全部须为空）。

### ④ 切换候选首次启动

`git merge --ff-only` 到目标提交 → `venv/bin/pip install -r requirements.txt`（1.6 升级了依赖，失败即停）→ `systemctl start flashshare-api` →
healthz / 启动日志按部署指南第 6 步通过标准（含 seed、recovery、无 ERROR）。
首启同时创建并播种 tags/formats 参考表——这是 ⑤ 补 FK 的前置。

**回滚边界**：④⑤ 之前失败仍可 `checkout 旧 SHA` → 重装旧版依赖 → restart 回旧代码
（列已加未删，旧代码不读新列，兼容）。**阶段 ⑤ 执行后此路径关闭**（见
⑤ 的回滚条件）。

### ⑤ 补 FK、删除 icon/trusted（首启健康之后）

新代码对 `icon`/`trusted` 零引用，删除对运行中的新服务无影响；两列删除
**必须**在旧代码确定不再运行之后：

- **旧代码仍写 `icon`**（seed upsert 的 `EXCLUDED.icon` / NOT NULL
  DEFAULT）——列先删会让旧代码的 seeding 与建工具直接 UndefinedColumn。
- **旧代码读 `trusted`**（目录查询 SELECT 该列）——列删除后旧代码的读
  **直接报 UndefinedColumn 失败**，不是、也不能声称"读空列等价于 FALSE"
  （不存在"空列"：DROP 之后该列对任何读方都是错误）。预检 a 证明全库无
  TRUE 行只保证删除不改变豁免语义（豁免判定唯一归属 `owner_id IS NULL`），
  不保证旧代码可继续运行。

按 `add_platform_tools_format_family.md` 步骤 ③ 补 FK，再执行：

```sql
ALTER TABLE platform_tools DROP COLUMN IF EXISTS icon;
ALTER TABLE platform_tools DROP COLUMN IF EXISTS trusted;
```

**回滚条件（明确）**：本阶段之后，`checkout 旧 SHA + restart` 不再可用
——旧代码读 `trusted`、写 `icon`，两列已删必然 UndefinedColumn。此后唯一
回滚手段是**从备份恢复数据库**（① 已验证可恢复的那份），损失范围 =
**该备份快照之后所有未被该备份覆盖的变更**——包括停写窗口之前该备份
未覆盖的线上变更、以及停写与删列之间发生的任何写入；不限于"删列之后
的数据"。是否走备份恢复由 owner 裁定。新代码在删列前后均可运行，
阶段 ⑥ 的问题用新代码修复或走备份，不存在"切回旧 SHA"选项。

### ⑥ 二次启动 + 部署后检查

`systemctl restart flashshare-api` 再次确认启动通过标准（幂等：current
schema 全部 `IF NOT EXISTS`，二次启动为空操作），然后：

1. **九个索引的存在、定义与有效状态核对**——不能只看 `IF NOT EXISTS`
   没报错：`CONCURRENTLY` 失败会留下 **INVALID** 索引，而 `IF NOT EXISTS`
   会因同名索引存在而永远跳过重建。核对（每行须 `t`、定义与下文一致、
   `indisvalid` 为真）：

```sql
SELECT i.relname, pg_get_indexdef(y.indexrelid) AS def, y.indisvalid
  FROM pg_index y
  JOIN pg_class i ON i.oid = y.indexrelid
 WHERE i.relname IN (
   'ledger_ref_idx','ledger_id_text_idx','ledger_user_id_idx',
   'tool_calls_id_text_idx','tool_calls_tool_ok_idx','tool_calls_user_id_idx',
   'ingress_requests_ref_idx','ingress_requests_running_idx',
   'uploads_expire_idx');
-- 须返回 9 行、indisvalid 全真；存在 INVALID 行 → DROP INDEX 该索引并
-- 重新 CREATE INDEX CONCURRENTLY，不得带着 INVALID 索引收口
```

2. FK 存在 + 行数一致 + 单形状零违反（format_family 资产的「首启后复查」
   两段 SQL）。
3. 部署指南第 7 步冒烟。

## 语义

- 9 条查询索引服务大数据量分页与关联（调用记录 / 积分流水 / 市场成功数 /
  入场幂等回执 / 到期清扫）。
- `platform_tools.trusted` 列退役：豁免判定唯一归属 `owner_id IS NULL`
  （运营/一方工具），社区行恒非豁免。

## 部署前只读预检（每项均须为空才可继续；处置见阶段 ①）

```sql
-- 预检 a：trusted 必须全为 FALSE（存在 TRUE 行 = 删列改变其 SSRF 语义）
SELECT tool_key, owner_id FROM platform_tools WHERE trusted;

-- 预检 b：每个 ledger kind 必须属于且只属于一个池
SELECT DISTINCT kind FROM ledger WHERE kind NOT IN (
  'grant_signup','grant_daily','signup_bonus','daily','bonus','topup','consume','refund',
  'adjust','grant','revoke','manual_topup','grant_plan','grant_package','grant_plan_upgrade',
  'provider_earn','withdraw','platform_commission','platform_revenue');

-- 预检 c：每个（用户，池）的最后一行 balance_after == SUM(delta)
WITH p AS (
  SELECT user_id, id, delta, balance_after,
         CASE WHEN kind IN ('provider_earn','withdraw') THEN 'earn'
              WHEN kind IN ('platform_commission','platform_revenue') THEN 'platform'
              ELSE 'spendable' END AS pool
  FROM ledger)
SELECT user_id, pool, SUM(delta) AS sum_delta,
       (array_agg(balance_after ORDER BY id DESC))[1] AS last_after
FROM p GROUP BY user_id, pool
HAVING SUM(delta) <> (array_agg(balance_after ORDER BY id DESC))[1];

-- 预检 d：content_type 不得含控制字符或非 ASCII
SELECT id, public_id FROM uploads WHERE content_type ~ '[^\x20-\x7e]';
```

## 阶段 ③ 的九个索引（逐条独立执行，不在事务中）

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS ledger_ref_idx ON ledger (ref_type, ref_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ledger_id_text_idx ON ledger ((id::text));
CREATE INDEX CONCURRENTLY IF NOT EXISTS ledger_user_id_idx ON ledger (user_id, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS tool_calls_id_text_idx ON tool_calls ((id::text));
CREATE INDEX CONCURRENTLY IF NOT EXISTS tool_calls_tool_ok_idx ON tool_calls (tool_id) WHERE status = 'ok';
CREATE INDEX CONCURRENTLY IF NOT EXISTS tool_calls_user_id_idx ON tool_calls (user_id, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ingress_requests_ref_idx ON ingress_requests (user_id, scope, request_ref, id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ingress_requests_running_idx ON ingress_requests (started_at) WHERE status = 'running';
CREATE INDEX CONCURRENTLY IF NOT EXISTS uploads_expire_idx ON uploads (expire_at);
```
