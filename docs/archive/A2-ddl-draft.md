# A2 DDL 设计稿 — PostgreSQL 全量表结构（待用户定稿）

> 状态：**设计稿，未执行**。定稿后作为 A3/A4 的施工基准。
> 原则：时间戳保持 BIGINT 秒（与现库行为对齐，不漂移）；id 保持整型自增；
> 新表只建结构不接逻辑（批次B/C 再接）；索引/约束一次到位。

## Part 1 — 旧表迁 PG（结构等价迁移）

```sql
-- users: 与 SQLite 版逐列等价，id 改 IDENTITY
CREATE TABLE IF NOT EXISTS users (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  email         TEXT UNIQUE,
  password_hash TEXT,
  display_name  TEXT NOT NULL,
  bio           TEXT,
  avatar_url    TEXT,
  x_user_id     TEXT UNIQUE,
  x_username    TEXT,
  x_display_name TEXT,
  x_avatar_url  TEXT,
  x_profile_url TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    BIGINT NOT NULL,
  last_login_at BIGINT NOT NULL,
  updated_at    BIGINT NOT NULL DEFAULT 0
);

-- sessions: token_hash 主键，语义不变
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at BIGINT NOT NULL,
  created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);

-- oauth_states: 临时态表，随过期清理
CREATE TABLE IF NOT EXISTS oauth_states (
  state        TEXT PRIMARY KEY,
  verifier     TEXT NOT NULL,
  expires_at   BIGINT NOT NULL,
  created_at   BIGINT NOT NULL,
  bind_user_id BIGINT REFERENCES users(id) ON DELETE CASCADE
);

-- uploads: 逐列等价；public_id 唯一索引保持
CREATE TABLE IF NOT EXISTS uploads (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ip                TEXT NOT NULL,
  gokapi_id         TEXT NOT NULL,
  public_id         TEXT,
  file_name         TEXT NOT NULL,
  content_type      TEXT NOT NULL,
  size_bytes        BIGINT NOT NULL,
  expiry_days       INT  NOT NULL,
  allowed_downloads INT  NOT NULL,
  expire_at         BIGINT,
  url_download      TEXT NOT NULL,
  url_hotlink       TEXT,
  created_at        BIGINT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uploads_public_id_idx ON uploads(public_id);
CREATE INDEX IF NOT EXISTS uploads_user_created_idx ON uploads(user_id, created_at);
CREATE INDEX IF NOT EXISTS uploads_ip_created_idx ON uploads(ip, created_at);
```

## Part 2 — 新表（经济内核，批次B 接逻辑）

```sql
-- ── 令牌（agent/MCP/API 通道，与 sessions 平行）──
CREATE TABLE IF NOT EXISTS api_tokens (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,                    -- 用户起的别名, 如 "claude-agent"
  token_hash  TEXT NOT NULL UNIQUE,             -- sha256(明文), 明文仅创建时展示一次
  prefix      TEXT NOT NULL,                    -- 前8字符, 列表页识别用 fsk_XXXX…
  status      TEXT NOT NULL DEFAULT 'active',   -- active | revoked
  last_used_at BIGINT,
  expires_at  BIGINT,                           -- NULL=永不过期
  created_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS api_tokens_user_idx ON api_tokens(user_id);
CREATE INDEX IF NOT EXISTS api_tokens_hash_idx ON api_tokens(token_hash);

-- ── 积分账本（只追加，禁止 UPDATE/DELETE —— 审计要求）──
CREATE TABLE IF NOT EXISTS ledger (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  delta         BIGINT NOT NULL,                -- 正=发放 负=消耗 (积分=最小单位整数)
  balance_after BIGINT NOT NULL,                -- 该笔落地后余额快照
  kind          TEXT NOT NULL,                  -- grant_signup | grant_plan | grant_package |
                                                -- consume | refund | adjust | grant_referral
  ref_type      TEXT,                           -- 关联对象类型: tool_call | order | plan | admin
  ref_id        TEXT,                           -- 关联对象 id (tool_calls.client_ref 等)
  idem_key      TEXT,                           -- 幂等键 (kind+ref 唯一时防重放)
  note          TEXT,
  created_at    BIGINT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_idem_idx ON ledger(user_id, idem_key)
  WHERE idem_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ledger_user_created_idx ON ledger(user_id, created_at);

-- ── 工具调用（计量事实表，批次C 接入）──
CREATE TABLE IF NOT EXISTS tool_calls (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  client_ref    TEXT NOT NULL,                  -- 调用方幂等键 (agent 必传, web 选传)
  user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tool_id       TEXT NOT NULL,                  -- doc2pdf / img2pdf / …
  status        TEXT NOT NULL,                  -- succeeded | failed | refunded
  cost_snapshot BIGINT NOT NULL DEFAULT 0,      -- 扣费时定价快照(非引用, 定价事后改不影响)
  upload_id     BIGINT REFERENCES uploads(id) ON DELETE SET NULL,
  result_public_id TEXT,                        -- 产物 public_id (失败=NULL)
  error         TEXT,
  params_json   TEXT,                           -- 请求参数快照
  result_json   TEXT,                           -- 响应快照(响应重构可重建)
  started_at    BIGINT NOT NULL,
  finished_at   BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS tool_calls_client_ref_idx ON tool_calls(user_id, client_ref);
CREATE INDEX IF NOT EXISTS tool_calls_user_time_idx ON tool_calls(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS tool_calls_tool_time_idx ON tool_calls(tool_id, started_at DESC);

-- ── 定价（/samelabs 后台可改，register() 无 cost 的对应面）──
CREATE TABLE IF NOT EXISTS pricing (
  tool_id    TEXT PRIMARY KEY,
  cost       BIGINT NOT NULL DEFAULT 0,         -- 单次积分 (0=免费)
  enabled    BOOLEAN NOT NULL DEFAULT TRUE,     -- 后台停用开关
  updated_at BIGINT NOT NULL,
  updated_by BIGINT
);

-- ┐ 订阅档位（plans → 订阅周期 top-up 积分; Creem+微信支付仅接口位）
CREATE TABLE IF NOT EXISTS plans (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE,           -- free / pro / team
  name          TEXT NOT NULL,                  -- 展示名(后台自由填)
  monthly_credits BIGINT NOT NULL DEFAULT 0,    -- 周期发放
  price_cents   BIGINT NOT NULL DEFAULT 0,
  currency      TEXT NOT NULL DEFAULT 'USD',
  creem_price_id TEXT,                          -- Creem 接口位
  wechat_price_id TEXT,                         -- 微信支付接口位
  sort_order    INT NOT NULL DEFAULT 0,
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    BIGINT NOT NULL,
  updated_at    BIGINT NOT NULL
);

-- 用户当前档位（谁在哪个档）
CREATE TABLE IF NOT EXISTS user_plans (
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan_id     BIGINT NOT NULL REFERENCES plans(id),
  started_at  BIGINT NOT NULL,
  renews_at   BIGINT,                           -- NULL=永不续期(free)
  status      TEXT NOT NULL DEFAULT 'active',
  PRIMARY KEY (user_id, plan_id)
);

-- ── 积分包（一次性购买）──
CREATE TABLE IF NOT EXISTS packages (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  credits       BIGINT NOT NULL,
  price_cents   BIGINT NOT NULL,
  currency      TEXT NOT NULL DEFAULT 'USD',
  creem_price_id TEXT,
  wechat_price_id TEXT,
  sort_order    INT NOT NULL DEFAULT 0,
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    BIGINT NOT NULL,
  updated_at    BIGINT NOT NULL
);

-- ── 订单（支付事实，Creem/微信回调落此）──
CREATE TABLE IF NOT EXISTS orders (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,                  -- plan | package
  item_id       BIGINT NOT NULL,                -- plans.id 或 packages.id
  provider      TEXT NOT NULL,                  -- creem | wechat | manual
  provider_order_id TEXT UNIQUE,                -- 外部订单号(回调幂等锚)
  status        TEXT NOT NULL DEFAULT 'pending',-- pending | paid | failed | refunded
  amount_cents  BIGINT NOT NULL,
  currency      TEXT NOT NULL DEFAULT 'USD',
  granted       BOOLEAN NOT NULL DEFAULT FALSE, -- 发放完成标记(防重复发放)
  created_at    BIGINT NOT NULL,
  paid_at       BIGINT
);
CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id, created_at DESC);

-- ── 后台键值（/samelabs 运行时配置）──
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at BIGINT NOT NULL,
  updated_by BIGINT
);
```

## 关键设计决策（请确认）

| # | 决策 | 理由 |
|---|---|---|
| 1 | 时间戳全 BIGINT 秒 | 与现库一致，零行为漂移；不引入 timestamptz 换算 |
| 2 | id 全 `GENERATED ALWAYS AS IDENTITY` | 禁止手工插 id（迁移数据用 OVERRIDING SYSTEM VALUE） |
| 3 | ledger 只追加 | 审计要求：余额 = sum(delta) 恒等可验；refund 是新行不是 UPDATE |
| 4 | cost_snapshot 在 tool_calls | 定价快照落调用事实，pricing 事后改不追溯 |
| 5 | 幂等键两级 | ledger.idem_key（发放幂等）+ tool_calls.client_ref（调用幂等），均有唯一索引 |
| 6 | plans/user_plans/packages/orders 独立 | 订阅 top-up 与一次性包分离，Creem/微信只存接口位不实现 |
| 7 | **不加** users.credits 冗余列 | 余额走 ledger 派生（可加缓存表，批次B 视性能再定） |
| 8 | api_tokens 与 sessions 平行不合并 | 会话(浏览器)与令牌(agent)生命周期/撤销语义不同 |

## 迁移注意（A4 预告）

- 现库 47 users / 211 sessions / 0 oauth_states / 394 uploads 全量搬
- users.id/uploads.id 用 `OVERRIDING SYSTEM VALUE` 保号
- 迁移后 setval 对齐 identity 序列
- 对账：行数 + 每表抽样哈希
