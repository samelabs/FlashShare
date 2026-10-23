# Release migration: 1.6 审计收口（查询索引 + trusted 列退役）

**前置依赖**：既有三份资产（`add_platform_tools_body_columns.md`、
`add_platform_tools_format_family.md`、`drop_platform_tools_icon.md`）
之后执行；本资产在其后、同一 1.6 部署窗口内执行。

**执行顺序（强制，一条线）：**
**只读预检 → ① 建索引（CONCURRENTLY）→ ② 删 trusted 列 → 部署首启。**

先迁移后部署：新代码的 startup DDL 带 `IF NOT EXISTS`（预建后为空操作），
且不再有任何代码读写 `trusted` 列——列先删掉对运行中的旧服务无影响面
（旧代码只读不写该列，读空列与读 FALSE 同义：全库无 TRUE 行，预检 a
证明）。反向（先部署后迁移）会让新启动在千万行级表上同步建索引。

## 语义

- 9 条查询索引服务大数据量分页与关联（调用记录 / 积分流水 / 市场成功数 /
  入场幂等回执 / 到期清扫）；`CONCURRENTLY` 建立不锁写，不在事务中执行。
- `platform_tools.trusted` 列退役：豁免判定唯一归属 `owner_id IS NULL`
  （运营/一方工具），社区行恒非豁免。

## ⚠ 部署前只读预检（每项均须为空才可继续；任一非空 → STOP 交 PM 裁定）

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

## ① 建索引（逐条独立执行，不在事务中，不锁写）

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

## ② 删列

```sql
ALTER TABLE platform_tools DROP COLUMN IF EXISTS trusted;
```

## 部署首启

新代码 startup 的 `CREATE INDEX IF NOT EXISTS` 对预建索引为空操作；
启动日志确认无 ERROR（seed/recovery/索引段均正常）即迁移收口。
