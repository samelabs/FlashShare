"""Database layer — the 518 PostgreSQL DB API.

Connection strategy: one connection per db() call. Callers use
`with db() as con:` — the context manager commits on success, rolls
back on exception, and always closes.

Row access: RealDictCursor → every cursor row is a plain dict with
column-name keys.

init_db(): CREATE TABLE IF NOT EXISTS for the full schema + routine
expired-state cleanup. No repair logic for retired schemas — history
lives in git.
"""

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


def _database_dsn_kwargs() -> dict:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set — add it to /etc/flashshare.env "
            "(postgresql+psycopg://user:pass@127.0.0.1:5432/flashshare; "
            "the '+psycopg' suffix is accepted and stripped here)"
        )
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg2.extensions.parse_dsn(url)


class _Connection:
    """518 PostgreSQL connection API.

    Exposes .execute() (RealDictCursor, dict-style rows) directly on the
    connection so `con.execute(...)` is the single call convention across
    the codebase, plus commit/rollback/close and raw cursor() access.
    """

    def __init__(self, con):
        self._con = con

    def execute(self, sql, params=()):
        cur = self._con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._con.commit()

    def rollback(self):
        self._con.rollback()

    def close(self):
        self._con.close()

    def cursor(self, cursor_factory=psycopg2.extras.RealDictCursor):
        return self._con.cursor(cursor_factory=cursor_factory)


# Connection pool: SCRAM handshake costs ~60ms per fresh connection; the
# upload chain alone opens 3 (auth/quota/save). Pooling removes that fixed
# cost from every request. Thread-safe; dead connections are retried once.
from psycopg2.pool import ThreadedConnectionPool

_pool = None
_pool_lock = __import__("threading").Lock()
def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(
                    1, 8, **_database_dsn_kwargs())
    return _pool


@contextmanager
def db():
    pool = _get_pool()
    try:
        con = pool.getconn()
    except psycopg2.Error:
        # pool exhausted or stale — fall back to a direct connection
        con = psycopg2.connect(**_database_dsn_kwargs())
    wrap = _Connection(con)
    try:
        yield wrap
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        try:
            pool.putconn(con)
        except Exception:
            con.close()


# ── Schema ───────────────────────────────────────────────
# The current schema, stated once. Every column of every table lives in
# its CREATE TABLE statement; boot does CREATE IF NOT EXISTS + current
# bootstrap INSERTs only — it never repairs, renames or backfills a
# retired schema shape.

_SCHEMA_SQL = (
    # ── users ──
    """
    CREATE TABLE IF NOT EXISTS users (
      id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      username       TEXT NOT NULL UNIQUE,
      email          TEXT UNIQUE,
      password_hash  TEXT,
      display_name   TEXT NOT NULL,
      bio            TEXT,
      avatar_url     TEXT,
      x_user_id      TEXT UNIQUE,
      x_username     TEXT,
      x_display_name TEXT,
      x_avatar_url   TEXT,
      x_profile_url  TEXT,
      status         TEXT NOT NULL DEFAULT 'active',
      payout_channel TEXT,
      created_at     BIGINT NOT NULL,
      last_login_at  BIGINT NOT NULL,
      updated_at     BIGINT NOT NULL DEFAULT 0
    )
    """,
    # ── sessions ──
    """
    CREATE TABLE IF NOT EXISTS sessions (
      token_hash TEXT PRIMARY KEY,
      user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      expires_at BIGINT NOT NULL,
      created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id)
    """,
    # ── oauth_states ──
    """
    CREATE TABLE IF NOT EXISTS oauth_states (
      state        TEXT PRIMARY KEY,
      verifier     TEXT NOT NULL,
      expires_at   BIGINT NOT NULL,
      created_at   BIGINT NOT NULL,
      bind_user_id BIGINT REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    # ── uploads ──
    """
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
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uploads_public_id_idx ON uploads(public_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS uploads_user_created_idx ON uploads(user_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS uploads_ip_created_idx ON uploads(ip, created_at)
    """,
    # Ingress quota facts are deliberately separate from uploads (which is
    # the durable share/result entity). One row = one source file accepted
    # from a user; generated tool outputs never count a second time.
    """
    CREATE TABLE IF NOT EXISTS upload_usage (
      id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      ip         TEXT NOT NULL,
      size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
      created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS upload_usage_user_created_idx
      ON upload_usage(user_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS upload_usage_ip_created_idx
      ON upload_usage(ip, created_at)
    """,
    # ── platform: ingress_requests (side-effect receipt; not an upload SoT) ──
    """
    CREATE TABLE IF NOT EXISTS ingress_requests (
      id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      request_ref      TEXT NOT NULL,
      request_hash     TEXT NOT NULL,
      status           TEXT NOT NULL,
      usage_ids        BIGINT[] NOT NULL,
      result_json      TEXT,
      started_at       BIGINT NOT NULL,
      finished_at      BIGINT,

      CONSTRAINT ingress_requests_status_ck
        CHECK (status IN ('running','ok','failed')),
      CONSTRAINT ingress_requests_running_open_ck
        CHECK (status != 'running' OR finished_at IS NULL),
      CONSTRAINT ingress_requests_terminal_closed_ck
        CHECK (status = 'running' OR finished_at IS NOT NULL),
      CONSTRAINT ingress_requests_ok_result_ck
        CHECK (status != 'ok' OR result_json IS NOT NULL),
      CONSTRAINT ingress_requests_non_ok_no_result_ck
        CHECK (status = 'ok' OR result_json IS NULL)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ingress_requests_ref_live_idx
      ON ingress_requests(user_id, request_ref)
      WHERE status IN ('running','ok')
    """,
    # ── platform: api_tokens (agent/MCP/API channel; parallel to sessions) ──
    """
    CREATE TABLE IF NOT EXISTS api_tokens (
      id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      name         TEXT NOT NULL,
      token_hash   TEXT NOT NULL UNIQUE,
      prefix       TEXT NOT NULL,
      status       TEXT NOT NULL DEFAULT 'active',
      last_used_at BIGINT,
      expires_at   BIGINT,
      created_at   BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS api_tokens_user_idx ON api_tokens(user_id)
    """,
    # ── platform: ledger (append-only; balance == sum(delta), auditable) ──
    """
    CREATE TABLE IF NOT EXISTS ledger (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      delta         BIGINT NOT NULL,
      balance_after BIGINT NOT NULL,
      kind          TEXT NOT NULL,
      ref_type      TEXT,
      ref_id        TEXT,
      idem_key      TEXT,
      note          TEXT,
      created_at    BIGINT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ledger_idem_idx
      ON ledger(user_id, idem_key) WHERE idem_key IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS ledger_user_created_idx ON ledger(user_id, created_at)
    """,
    # ── platform: tool_calls (metering fact table) ──
    """
    CREATE TABLE IF NOT EXISTS tool_calls (
      id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      client_ref       TEXT NOT NULL,
      user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      tool_id          TEXT NOT NULL,
      status           TEXT NOT NULL,
      cost_snapshot    BIGINT NOT NULL DEFAULT 0,
      upload_id        BIGINT REFERENCES uploads(id) ON DELETE SET NULL,
      result_public_id TEXT,
      error            TEXT,
      params_json      TEXT,
      result_json      TEXT,
      started_at       BIGINT NOT NULL,
      finished_at      BIGINT,
      commission_rate_snapshot INTEGER,
      invocation_hash    TEXT,
      CONSTRAINT tool_calls_live_invocation_hash_ck
        CHECK (status NOT IN ('running','ok')
               OR invocation_hash IS NOT NULL),
      CONSTRAINT tool_calls_status_ck
        CHECK (status IN ('running','ok','failed','insufficient')),
      CONSTRAINT tool_calls_cost_snapshot_ck CHECK (cost_snapshot >= 0),
      CONSTRAINT tool_calls_commission_snapshot_range_ck
        CHECK (commission_rate_snapshot IS NULL
               OR commission_rate_snapshot BETWEEN 0 AND 100),
      CONSTRAINT tool_calls_running_commission_snapshot_ck
        CHECK (status <> 'running'
               OR commission_rate_snapshot IS NOT NULL),
      CONSTRAINT tool_calls_running_open_ck
        CHECK (status != 'running' OR finished_at IS NULL),
      CONSTRAINT tool_calls_terminal_closed_ck
        CHECK (status = 'running' OR finished_at IS NOT NULL),
      CONSTRAINT tool_calls_non_ok_no_result_ck
        CHECK (status = 'ok'
               OR (upload_id IS NULL AND result_public_id IS NULL
                   AND result_json IS NULL))
    )
    """,
    # Invocation identity is user + tool + client_ref. A running row owns the
    # execution; an ok row owns replay. Failed attempts leave the key reusable.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tool_calls_client_ref_live_idx
      ON tool_calls(user_id, tool_id, client_ref)
      WHERE status IN ('running','ok')
    """,
    """
    CREATE INDEX IF NOT EXISTS tool_calls_user_time_idx
      ON tool_calls(user_id, started_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS tool_calls_tool_time_idx
      ON tool_calls(tool_id, started_at DESC)
    """,
    # ── platform: pricing ──
    """
    CREATE TABLE IF NOT EXISTS pricing (
      tool_id    TEXT PRIMARY KEY,
      cost       BIGINT NOT NULL DEFAULT 0,
      enabled    BOOLEAN NOT NULL DEFAULT TRUE,
      updated_at BIGINT NOT NULL,
      updated_by BIGINT,
      CONSTRAINT pricing_cost_ck CHECK (cost >= 0)
    )
    """,
    # ── platform: plans ──
    """
    CREATE TABLE IF NOT EXISTS plans (
      id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      code             TEXT NOT NULL UNIQUE,
      name             TEXT NOT NULL,
      monthly_credits  BIGINT NOT NULL DEFAULT 0,
      period_days      INT NOT NULL DEFAULT 30,
      period           TEXT NOT NULL DEFAULT 'monthly',
      price_cents      BIGINT NOT NULL DEFAULT 0,
      currency         TEXT NOT NULL DEFAULT 'USD',
      creem_product_id  TEXT,
      wechat_price_id  TEXT,
      sort_order       INT NOT NULL DEFAULT 0,
      active           BOOLEAN NOT NULL DEFAULT TRUE,
      badge_key        TEXT,
      ingress_files_24h INTEGER NOT NULL DEFAULT 0,
      ingress_bytes_24h BIGINT NOT NULL DEFAULT 0,
      expiry_days      INTEGER[] NOT NULL DEFAULT ARRAY[]::INTEGER[],
      download_limits  INTEGER[] NOT NULL DEFAULT ARRAY[]::INTEGER[],
      created_at       BIGINT NOT NULL,
      updated_at       BIGINT NOT NULL,
      CONSTRAINT plans_period_days_ck CHECK (period_days > 0),
      CONSTRAINT plans_monthly_credits_ck CHECK (monthly_credits >= 0),
      CONSTRAINT plans_price_cents_ck CHECK (price_cents >= 0),
      CONSTRAINT plans_sort_order_ck CHECK (sort_order >= 0),
      CONSTRAINT plans_ingress_files_ck CHECK (ingress_files_24h >= 0),
      CONSTRAINT plans_ingress_bytes_ck CHECK (ingress_bytes_24h >= 0)
    )
    """,
    # ── platform: packages ──
    """
    CREATE TABLE IF NOT EXISTS packages (
      id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      code             TEXT NOT NULL UNIQUE,
      name             TEXT NOT NULL,
      credits          BIGINT NOT NULL,
      price_cents      BIGINT NOT NULL,
      currency         TEXT NOT NULL DEFAULT 'USD',
      wechat_price_id  TEXT,
      sort_order       INT NOT NULL DEFAULT 0,
      active           BOOLEAN NOT NULL DEFAULT TRUE,
      created_at       BIGINT NOT NULL,
      updated_at       BIGINT NOT NULL,
      CONSTRAINT packages_credits_ck CHECK (credits > 0),
      CONSTRAINT packages_price_cents_ck CHECK (price_cents >= 0)
    )
    """,
    # ── platform: orders ──
    """
    CREATE TABLE IF NOT EXISTS orders (
      id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind              TEXT NOT NULL,
      item_id           BIGINT NOT NULL,
      provider          TEXT NOT NULL,
      provider_order_id TEXT UNIQUE,
      status            TEXT NOT NULL DEFAULT 'pending',
      amount_cents      BIGINT NOT NULL,
      currency          TEXT NOT NULL DEFAULT 'USD',
      credits_snapshot  BIGINT,
      client_ref        TEXT,
      granted           BOOLEAN NOT NULL DEFAULT FALSE,
      created_at        BIGINT NOT NULL,
      paid_at           BIGINT,
      refunded_at       BIGINT,
      CONSTRAINT orders_status_ck
        CHECK (status IN ('pending','paid','failed','refunded','disputed')),
      CONSTRAINT orders_amount_ck CHECK (amount_cents >= 0),
      CONSTRAINT orders_credits_snapshot_ck
        CHECK (credits_snapshot IS NULL OR credits_snapshot >= 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id, created_at DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS orders_user_client_ref_uk
      ON orders(user_id, client_ref)
      WHERE client_ref IS NOT NULL
    """,
    # ── platform: operator_audit (append-only governance fact) ──
    """
    CREATE TABLE IF NOT EXISTS operator_audit (
      id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      actor_user_id  BIGINT NOT NULL REFERENCES users(id),
      action         TEXT NOT NULL,
      target_type    TEXT NOT NULL,
      target_id      TEXT NOT NULL,
      before_json    JSONB,
      after_json     JSONB,
      created_at     BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS operator_audit_created_idx
      ON operator_audit(created_at DESC, id DESC)
    """,
    # ── platform: settings ──
    """
    CREATE TABLE IF NOT EXISTS settings (
      key        TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      updated_at BIGINT NOT NULL,
      updated_by BIGINT
    )
    """,
    # ── platform: platform_tools (tool registry — data-state rows) ──
    # The runtime source of truth for the tool catalog. First-party tools are
    # seeded here at startup from register() declarations (code = creation
    # format, DB = runtime truth). Remote/http/mcp execution classes and
    # namespaced user tools (owner/tool_id) land as data rows later —
    # resolve() is execution-class-agnostic and origin-agnostic.
    # trusted: operator-adoption flag. User rows default FALSE (SSRF guard
    # enforces https + public IPs); TRUE only via /samelabs (institutional
    # supply the operator vouches for). First-party rows are trusted by
    # definition (owner IS NULL → resolve treats them as trusted).
    """
    CREATE TABLE IF NOT EXISTS platform_tools (
      id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      tool_key       TEXT NOT NULL,          -- 'beautify' | 'owner/tool'
      owner_id       BIGINT REFERENCES users(id) ON DELETE CASCADE,
                                           -- NULL = first-party seed
      tool_id        TEXT NOT NULL,          -- bare id within namespace
      execution      TEXT NOT NULL DEFAULT 'local',
                                           -- local | http | mcp (future)
      tool_type      TEXT NOT NULL DEFAULT 'produce',
                                           -- produce | view | peek
      label_json     TEXT NOT NULL,          -- {lang: display_name}
      desc_json      TEXT,                   -- 简介 {lang: str} | str (可空)
      sort_order     INT NOT NULL DEFAULT 0, -- 后台遴选排序 (首页/市场呈现序)
      featured       BOOLEAN NOT NULL DEFAULT FALSE, -- 推荐标记
      icon           TEXT NOT NULL DEFAULT 'wand',
      accept_json    TEXT,                   -- JSON array or NULL = any
      multi          BOOLEAN NOT NULL DEFAULT FALSE,
      pass_through   BOOLEAN NOT NULL DEFAULT FALSE,
                                           -- TRUE = 直传工具: 上传即结果, 无执行步 (share)
      visibility     TEXT NOT NULL DEFAULT 'public',
                                           -- public | unlisted | private
      endpoint_json  TEXT,                   -- remote execution decl (future)
      params_json    TEXT,                   -- typed params schema (future)
      enabled        BOOLEAN NOT NULL DEFAULT TRUE,
      trusted        BOOLEAN NOT NULL DEFAULT FALSE,
      created_at     BIGINT NOT NULL,
      updated_at     BIGINT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS platform_tools_key_idx
      ON platform_tools(tool_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS platform_tools_owner_idx
      ON platform_tools(owner_id)
    """,
    # activities 表已移除 — 每日登录 = economy.daily 模块常量,
    # 无表无 seed; 发放流水在 ledger (note='activity:daily_login')。
    # ── platform: withdrawals (provider payout requests) ──
    """
    CREATE TABLE IF NOT EXISTS withdrawals (
      id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      amount       BIGINT NOT NULL CHECK (amount > 0),
      status       TEXT NOT NULL DEFAULT 'pending',
      note         TEXT,
      created_at   BIGINT NOT NULL,
      processed_at BIGINT,
      processed_by BIGINT,
      txn_ref      TEXT,
      CONSTRAINT withdrawals_status_ck
        CHECK (status IN ('pending','approved','rejected','paid'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS withdrawals_user_idx ON withdrawals(user_id)
    """,
    # ── subscription (plans/memberships) ──
    # tier rows (幂等 INSERT-ONLY: seed 只保证行存在, 数值归运营/迁移,
    # 启动永不覆盖 — 历史环境的已配置值必须存活)。
    # free = 无 paid Membership 的基础档: 不可售 (无 product), credits 0。
    """INSERT INTO plans (code, name, monthly_credits, period_days, period,
                         price_cents, currency, sort_order, active, badge_key,
                         ingress_files_24h, ingress_bytes_24h, expiry_days, download_limits,
                         created_at, updated_at)
       VALUES ('plus', 'Plus', 100, 30, 'monthly', 0, 'USD', 1, TRUE, 'tier_plus',
               500, 5368709120, ARRAY[1,3,7,30], ARRAY[1,10,50,100,200,1000],
               EXTRACT(EPOCH FROM now())::BIGINT, EXTRACT(EPOCH FROM now())::BIGINT),
              ('pro', 'Pro', 500, 30, 'monthly', 0, 'USD', 2, TRUE, 'tier_pro',
               2000, 21474836480, ARRAY[1,3,7,30], ARRAY[1,10,50,100,200,1000],
               EXTRACT(EPOCH FROM now())::BIGINT, EXTRACT(EPOCH FROM now())::BIGINT)
       ON CONFLICT (code) DO NOTHING""",
    # ── Tier 不变量 (race-safe, DB 层兜底) ──
    # 1) paid tier rank 严格全序: 两个不同 paid tier 不得共享 sort_order
    #    (direction = order 差; 同序 = direction 0 = 不可比)。free 与最低
    #    paid 可共用数字 (free 不参与 paid direction)。inactive paid tier
    #    仍可能是 current Membership, 同样不得复用 rank。历史冲突 → 建
    #    索引失败 → 启动 fail loud, 人工裁定。
    """CREATE UNIQUE INDEX IF NOT EXISTS plans_paid_sort_order_uk
       ON plans (sort_order) WHERE code <> 'free'""",
    # 2) provider product → tier 唯一: by_product() 是 webhook/provider
    #    snapshot → Tier 的 authority, 同一 product 绝不能映射两个 tier。
    """CREATE UNIQUE INDEX IF NOT EXISTS plans_product_binding_uk
       ON plans (creem_product_id) WHERE creem_product_id IS NOT NULL""",
    # scheduled downgrade fact (Pro → Plus next renewal): Creem 官方 API
    # 无原生 schedule product change — application-level pending fact,
    # 周期边界由 reconcile/事件收敛时消费。
    """
    CREATE TABLE IF NOT EXISTS pending_downgrades (
      user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      target_plan_id BIGINT NOT NULL REFERENCES plans(id),
      effective_at   BIGINT NOT NULL,
      created_at     BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM now())::BIGINT,
      PRIMARY KEY (user_id)
    )
    """,
    # ── membership 单行权威 (governance closure): 一个 user 最多一个
    # current Membership; provider subscription 是唯一绑定。
    """
    CREATE TABLE IF NOT EXISTS memberships (
      user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      plan_id    BIGINT NOT NULL REFERENCES plans(id),
      provider_subscription_id TEXT UNIQUE NOT NULL,
      status     TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','past_due','canceled','expired')),
      started_at BIGINT NOT NULL,
      renews_at  BIGINT,
      cancel_scheduled_at     BIGINT,
      last_grant_period_start BIGINT,
      updated_at BIGINT,
      PRIMARY KEY (user_id)
    )
    """,
    )


def init_db() -> None:
    from utils import now
    from config import DAILY_UPLOADS, DAILY_BYTES
    ts = now()
    with db() as con:
        for stmt in _SCHEMA_SQL:
            con.execute(stmt)
        # ── Free Tier authority bootstrap ──
        # env (DAILY_UPLOADS/DAILY_BYTES) 只是首次迁移输入: free 行不存在
        # 时按当前 env 值创建; 行一旦存在, plans row 即唯一事实源, 后续
        # 重启/改 env 不覆盖 (ON CONFLICT DO NOTHING + 补齐仅限空壳行)。
        con.execute(
            """
            INSERT INTO plans (code, name, monthly_credits, period_days, period,
                               price_cents, currency, sort_order, active, badge_key,
                               ingress_files_24h, ingress_bytes_24h,
                               expiry_days, download_limits,
                               created_at, updated_at)
            VALUES ('free', 'Free', 0, 30, 'monthly', 0, 'USD', 0, TRUE, 'tier_free',
                    %s, %s, ARRAY[1,3,7], ARRAY[1,10,50,100,200],
                    EXTRACT(EPOCH FROM now())::BIGINT, EXTRACT(EPOCH FROM now())::BIGINT)
            ON CONFLICT (code) DO NOTHING
            """, (DAILY_UPLOADS, DAILY_BYTES))
        # Routine cleanup
        con.execute("DELETE FROM oauth_states WHERE expires_at < %s", (ts,))
        con.execute("DELETE FROM sessions WHERE expires_at < %s", (ts,))
        # Quota facts only serve the rolling 24h window (queries read
        # created_at > now-24h); older rows have no business value.
        con.execute("DELETE FROM upload_usage WHERE created_at < %s",
                    (ts - 24 * 3600,))
