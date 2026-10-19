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
      username       TEXT NOT NULL,
      email          TEXT UNIQUE,
      password_hash  TEXT,
      display_name   TEXT NOT NULL,
      bio            TEXT,
      avatar_url     TEXT,
      x_user_id      TEXT UNIQUE,
      x_username     TEXT,
      x_display_name TEXT,
      x_profile_url  TEXT,
      status         TEXT NOT NULL DEFAULT 'active',
      payout_channel TEXT,
      created_at     BIGINT NOT NULL,
      last_login_at  BIGINT NOT NULL,
      updated_at     BIGINT NOT NULL DEFAULT 0
    )
    """,
    # one email per identity regardless of case ; NULL emails
    # (X-only accounts, platform row) never collide under a partial index
    """CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uk
       ON users (lower(email)) WHERE email IS NOT NULL""",
    # one handle per folded name — registration, X auto-naming and the
    # author page all resolve usernames case-insensitively
    """CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_uk
       ON users (lower(username))""",
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
    # ── OAuth 2.1 authorization server (main /mcp; CIMD clients) ──
    """
    CREATE TABLE IF NOT EXISTS oauth_mcp_codes (
      code_hash      TEXT PRIMARY KEY,
      user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      client_id      TEXT NOT NULL,
      redirect_uri   TEXT NOT NULL,
      code_challenge TEXT NOT NULL,
      scope           TEXT NOT NULL,
      resource        TEXT NOT NULL,
      expires_at     BIGINT NOT NULL,
      created_at     BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS oauth_mcp_codes_expiry_idx
      ON oauth_mcp_codes(expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_mcp_tokens (
      id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      access_token_hash   TEXT NOT NULL UNIQUE,
      refresh_token_hash  TEXT NOT NULL UNIQUE,
      user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      client_id           TEXT NOT NULL,
      scope               TEXT NOT NULL,
      resource            TEXT NOT NULL,
      access_expires_at   BIGINT NOT NULL,
      refresh_expires_at  BIGINT NOT NULL,
      revoked_at          BIGINT,
      last_used_at        BIGINT,
      created_at          BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS oauth_mcp_tokens_expiry_idx
      ON oauth_mcp_tokens(refresh_expires_at)
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
      scope            TEXT NOT NULL DEFAULT '',
                         -- client_ref uniqueness partition: the selected
                         -- tool_key (web upload / MCP inline call) or ''
                         -- for the tool-agnostic ticket PUT
      request_ref      TEXT NOT NULL,
      request_hash     TEXT NOT NULL,
      status           TEXT NOT NULL,
      usage_ids        BIGINT[] NOT NULL,
      result_json      TEXT,
      started_at       BIGINT NOT NULL,
      finished_at      BIGINT,
      oauth_client_id  TEXT,

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
      ON ingress_requests(user_id, scope, request_ref)
      WHERE status IN ('running','ok')
    """,
    # ── platform: api_keys (MCP/agent credential; parallel to sessions) ──
    # key_hash authenticates; key_sealed is the encrypted copy that lets
    # the owner view the full key again (secrets_vault, TOOL_VAULT_KEY).
    # Historical rows migrated without a sealed copy have key_sealed
    # NULL — the page then shows the prefix only (a data state, not a
    # compatibility branch).
    """
    CREATE TABLE IF NOT EXISTS api_keys (
      id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      name         TEXT NOT NULL,
      key_hash     TEXT NOT NULL UNIQUE,
      key_sealed   TEXT,
      prefix       TEXT NOT NULL,
      last_used_at BIGINT,
      created_at   BIGINT NOT NULL,
      scopes       TEXT NOT NULL DEFAULT 'tools:call files:read files:delete'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS api_keys_user_idx ON api_keys(user_id)
    """,
    # ── platform: ledger (append-only; balance == sum(delta), auditable) ──
    """
    CREATE TABLE IF NOT EXISTS ledger (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
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
      user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      tool_id          TEXT NOT NULL,
      status           TEXT NOT NULL,
      cost_snapshot    BIGINT NOT NULL DEFAULT 0,
      result_public_id TEXT,
      error            TEXT,
      params_json      TEXT,
      result_json      TEXT,
      started_at       BIGINT NOT NULL,
      finished_at      BIGINT,
      oauth_client_id  TEXT,
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
               OR (result_public_id IS NULL AND result_json IS NULL))
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
    # /r/{id} lookup authority — one live result row per public id
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tool_calls_result_public_id_idx
      ON tool_calls(result_public_id)
      WHERE result_public_id IS NOT NULL
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
      user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      kind              TEXT NOT NULL,
      item_id           BIGINT NOT NULL,
      provider          TEXT NOT NULL,
      provider_order_id TEXT UNIQUE,
      status            TEXT NOT NULL DEFAULT 'pending',
      amount_cents      BIGINT NOT NULL,
      currency          TEXT NOT NULL DEFAULT 'USD',
      credits_snapshot  BIGINT,
      granted           BOOLEAN NOT NULL DEFAULT FALSE,
      created_at        BIGINT NOT NULL,
      paid_at           BIGINT,
      refunded_at       BIGINT,
      CONSTRAINT orders_status_ck
        CHECK (status IN ('pending','paid','failed','refunded','disputed')),
      CONSTRAINT orders_amount_ck CHECK (amount_cents >= 0),
      CONSTRAINT orders_credits_snapshot_ck
        CHECK (credits_snapshot IS NULL OR credits_snapshot >= 0),
      CONSTRAINT orders_kind_ck
        CHECK (kind IN ('package','plan')),
      CONSTRAINT orders_kind_snapshot_ck
        CHECK ((kind = 'package' AND credits_snapshot IS NOT NULL
                AND credits_snapshot > 0)
            OR (kind = 'plan' AND credits_snapshot IS NULL)),
      CONSTRAINT orders_plan_status_ck
        CHECK (kind <> 'plan' OR status IN ('pending','paid','failed')),
      CONSTRAINT orders_plan_not_granted_ck
        CHECK (kind <> 'plan' OR granted = FALSE),
      CONSTRAINT orders_plan_not_reversed_ck
        CHECK (kind <> 'plan' OR status NOT IN ('refunded','disputed')),
      CONSTRAINT orders_pending_failed_fields_ck
        CHECK (status NOT IN ('pending','failed')
               OR (granted = FALSE AND paid_at IS NULL
                   AND refunded_at IS NULL)),
      CONSTRAINT orders_pending_identity_ns_ck
        CHECK (status IN ('pending','failed')
               OR provider_order_id IS NOT NULL),
      CONSTRAINT orders_pending_identity_ch_ck
        CHECK (status NOT IN ('pending','failed')
               OR provider_order_id IS NULL
               OR provider_order_id LIKE 'ch:%%'),
      CONSTRAINT orders_package_paid_fields_ck
        CHECK (kind <> 'package' OR status <> 'paid'
               OR (granted = TRUE AND paid_at IS NOT NULL
                   AND refunded_at IS NULL
                   AND provider_order_id IS NOT NULL)),
      CONSTRAINT orders_package_terminal_identity_ck
        CHECK (kind <> 'package'
               OR status NOT IN ('paid','refunded','disputed')
               OR provider_order_id IS NULL
               OR provider_order_id NOT LIKE 'ch:%%'),
      CONSTRAINT orders_package_reversal_fields_ck
        CHECK (kind <> 'package' OR status NOT IN ('refunded','disputed')
               OR (granted = TRUE AND paid_at IS NOT NULL
                   AND refunded_at IS NOT NULL
                   AND provider_order_id IS NOT NULL)),
      CONSTRAINT orders_plan_paid_fields_ck
        CHECK (kind <> 'plan' OR status <> 'paid'
               OR (granted = FALSE AND paid_at IS NOT NULL
                   AND refunded_at IS NULL
                   AND provider_order_id LIKE 'sub:%%')),
      CONSTRAINT orders_paid_fields_ck
        CHECK (status <> 'paid' OR paid_at IS NOT NULL),
      CONSTRAINT orders_reversal_fields_ck
        CHECK (status NOT IN ('refunded','disputed')
               OR refunded_at IS NOT NULL),
      CONSTRAINT orders_non_reversal_refund_ts_ck
        CHECK (status IN ('refunded','disputed') OR refunded_at IS NULL),
      CONSTRAINT orders_granted_kind_ck
        CHECK (granted = FALSE OR kind = 'package')
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id, created_at DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS orders_user_pending_plan_uk
      ON orders(user_id)
      WHERE kind = 'plan' AND status = 'pending'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS orders_user_item_pending_pkg_uk
      ON orders(user_id, item_id)
      WHERE kind = 'package' AND status = 'pending'
    """,
    # ── platform: operator_audit (append-only governance fact) ──
    """
    CREATE TABLE IF NOT EXISTS operator_audit (
      id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      actor_user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
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
    # commission rate bootstrap (fail-closed): the metering gate
    # refuses third-party metered calls when the rate is missing — the
    # platform ALWAYS ships a legal initial value (operator-editable via
    # the audited backoffice write; ON CONFLICT keeps configured values).
    """INSERT INTO settings (key, value, updated_at)
       VALUES ('commission_rate', '30', EXTRACT(EPOCH FROM now())::BIGINT)
       ON CONFLICT (key) DO NOTHING""",
    # ── 格式与 tag 参考表 (表驱动, 2026-10-19 工具链理顺) ──
    # formats = 平台格式边界 (每扩展名一行, 归属唯一场景 tag);
    # tags = 应用场景的格式组合 (八场景 + all; all = 全边界并集视图,
    # 无直属 formats 行, 恒 sort 末位)。首装种子来自
    # governance/formats.py 字面量; 运行态内存结构由表装载 (见
    # formats.sync_reference_tables — 装载点在本 init_db 内)。
    """
    CREATE TABLE IF NOT EXISTS tags (
      key        TEXT PRIMARY KEY,
      chip       TEXT NOT NULL,
      sort_order INT  NOT NULL,
      created_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS formats (
      ext        TEXT PRIMARY KEY,
      mime       TEXT NOT NULL,
      tag        TEXT NOT NULL REFERENCES tags(key),
      created_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS formats_tag_idx ON formats(tag)",
        # ── platform: platform_tools (tool registry — data-state rows) ──
    # The runtime source of truth for the tool catalog. First-party tools are
    # seeded here at startup from register() declarations (code = creation
    # format, DB = runtime truth). Remote (http) execution and namespaced
    # user tools (key = username_toolid) are current data rows —
    # resolve() is execution-class-agnostic and origin-agnostic.
    # trusted: operator-adoption flag. User rows default FALSE (SSRF guard
    # enforces https + public IPs); TRUE only via /samelabs (institutional
    # supply the operator vouches for). First-party rows are trusted by
    # definition (owner IS NULL → resolve treats them as trusted).
    """
    CREATE TABLE IF NOT EXISTS platform_tools (
      id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      tool_key       TEXT NOT NULL,          -- 'beautify' | 'owner/tool'
      owner_id       BIGINT REFERENCES users(id) ON DELETE RESTRICT,
      -- operator takedown (governance): developer enabled and operator
      -- delisted are INDEPENDENT switches; public visibility reads both.
      delisted      BOOLEAN NOT NULL DEFAULT FALSE,
                                           -- NULL = first-party seed
      tool_id        TEXT NOT NULL,          -- bare id within namespace
      execution      TEXT NOT NULL DEFAULT 'local',
                                           -- local | http (current execution classes)
                                           -- mcp is a northbound surface, NOT an execution class
                                           -- CHECK below pins the current vocabulary
                                           -- (startup catalog invariant enforces it for
                                           --  pre-CHECK stored rows; no migration/backfill)
      label_json     TEXT NOT NULL,          -- {lang: display_name}
      desc_json      TEXT,                   -- 简介 {lang: str} | str (可空)
      sort_order     INT NOT NULL DEFAULT 0, -- 后台遴选排序 (首页/市场呈现序)
      featured       BOOLEAN NOT NULL DEFAULT FALSE, -- 推荐标记
      accept_json    TEXT,                   -- 非空 JSON 列表; NULL 仅 = 无文件位 (files_max=0)
      format_family  TEXT REFERENCES tags(key),
                                           -- 声明的 tag (作者表达 + 图标来源);
                                           -- 纯文本工具 (files_max=0) 不带
      files_min      INT NOT NULL DEFAULT 1, -- input files lower bound (0 = text-only tools)
      files_max      INT NOT NULL DEFAULT 1, -- input files upper bound (<= 20)
      outputs_json   TEXT NOT NULL DEFAULT '["file"]',
                                           -- item kinds the tool may produce (text/json/file)
      visibility     TEXT NOT NULL DEFAULT 'public',
                                           -- public | unlisted | private
      endpoint_json  TEXT,                   -- http execution binding (url/timeout/auth)
      params_json    TEXT,                   -- typed params declaration
      CONSTRAINT platform_tools_execution_ck CHECK (execution IN ('local','http')),
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
    # activities 表已移除 — 每日登录额度在 settings 的 daily_login_amount
    # (新库不写该键 = 0 = 停用, 运营后台开启), 无表无 seed;
    # 发放流水在 ledger (note='activity:daily_login')。
    # ── platform: withdrawals (provider payout requests) ──
    # S4 final authority: review fields (reviewed_*) + paid fields
    # (paid_*, payout_ref) + request-time payout_channel_snapshot.
    # Retired names processed_at/processed_by/txn_ref are gone from the
    # fresh schema; the consolidated production migration happens at the
    # Final Release Gate (no startup ALTER/backfill here).
    # amount = USD-millis (whole-cent enforced by the economy command layer).
    """
    CREATE TABLE IF NOT EXISTS withdrawals (
      id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id                 BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      amount                  BIGINT NOT NULL CHECK (amount > 0),
      status                  TEXT NOT NULL DEFAULT 'pending',
      note                    TEXT,
      payout_channel_snapshot TEXT NOT NULL
                              CHECK (length(btrim(payout_channel_snapshot)) > 0),
      created_at              BIGINT NOT NULL,
      reviewed_at             BIGINT,
      reviewed_by             BIGINT,
      paid_at                 BIGINT,
      paid_by                 BIGINT,
      payout_ref              TEXT,
      CONSTRAINT withdrawals_status_ck
        CHECK (status IN ('pending','approved','rejected','paid',
                          'payout_failed')),
      CONSTRAINT withdrawals_pending_ck CHECK (
        status <> 'pending'
        OR (reviewed_at IS NULL AND reviewed_by IS NULL
            AND paid_at IS NULL AND paid_by IS NULL
            AND payout_ref IS NULL)),
      CONSTRAINT withdrawals_approved_ck CHECK (
        status <> 'approved'
        OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL
            AND paid_at IS NULL AND paid_by IS NULL
            AND payout_ref IS NULL)),
      CONSTRAINT withdrawals_rejected_ck CHECK (
        status <> 'rejected'
        OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL
            AND paid_at IS NULL AND paid_by IS NULL
            AND payout_ref IS NULL)),
      CONSTRAINT withdrawals_payout_failed_ck CHECK (
        status <> 'payout_failed'
        OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL
            AND paid_at IS NULL AND paid_by IS NULL
            AND payout_ref IS NULL)),
      CONSTRAINT withdrawals_paid_ck CHECK (
        status <> 'paid'
        OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL
            AND paid_at IS NOT NULL AND paid_by IS NOT NULL
            AND payout_ref IS NOT NULL
            AND length(btrim(payout_ref)) > 0))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS withdrawals_user_idx ON withdrawals(user_id)
    """,
    # ── platform: payment_reviews (partial-refund review queue) ──
    # No existing table models an INBOUND webhook adjudication fact: orders
    # rows are purchase intents (kind/status CHECKs a partial refund event
    # cannot satisfy), pending_downgrades is a per-user single-row intent.
    # One row per provider EVENT (unique provider+event_id → replay inserts
    # converge); resolution is an operator status flip — the queue never
    # moves money.
    """
    CREATE TABLE IF NOT EXISTS payment_reviews (
      id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      provider     TEXT NOT NULL DEFAULT 'creem',
      event_type   TEXT NOT NULL,
      event_id     TEXT NOT NULL,
      txn_id       TEXT NOT NULL,
      -- financial adjudication fact (b2b2): user deletion is restricted
      user_id      BIGINT REFERENCES users(id) ON DELETE RESTRICT,
      status       TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','resolved')),
      detail_json  JSONB,
      created_at   BIGINT NOT NULL,
      resolved_at  BIGINT,
      resolved_by  BIGINT,
      CONSTRAINT payment_reviews_resolved_ck CHECK (
        status <> 'resolved'
        OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS payment_reviews_event_idx
      ON payment_reviews (provider, event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS payment_reviews_open_idx
      ON payment_reviews (created_at DESC, id DESC) WHERE status = 'open'
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
    # ── per-period BASE payment record (transaction-record basis) ──
    # Which provider transaction funds a period's base cycle is an
    # economic fact that must NOT depend on ledger grant rows (a paid
    # plan with zero monthly credits mints none). apply_paid_period
    # inserts ON CONFLICT DO NOTHING for EVERY subscription.paid: the
    # FIRST paid transaction to arrive for (sub, period) IS the base
    # payment; later same-period charges (upgrades) never overwrite it.
    """
    CREATE TABLE IF NOT EXISTS subscription_period_payments (
      sub_id        TEXT NOT NULL,
      period_start  BIGINT NOT NULL,
      user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      base_txn_id   TEXT NOT NULL,
      created_at    BIGINT NOT NULL,
      PRIMARY KEY (sub_id, period_start)
    )
    """,
    # ── membership 单行权威 (governance closure): 一个 user 最多一个
    # current Membership; provider subscription 是唯一绑定。
    """
    CREATE TABLE IF NOT EXISTS memberships (
      user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
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
    # ── MCP two-stage upload: one-time PUT credentials (B4b) ──
    # The ticket IS the credential: bound to user + declared size + a
    # short expiry, consumed exactly once (used_at flips in the same
    # atomic UPDATE that authorizes the PUT). Replays hit used_at.
    """
    CREATE TABLE IF NOT EXISTS mcp_upload_tickets (
      id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      ticket_hash   TEXT NOT NULL UNIQUE,   -- sha256(plaintext); value never stored
      user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      filename      TEXT NOT NULL,
      content_type  TEXT,
      declared_size BIGINT NOT NULL CHECK (declared_size > 0),
      client_ref    TEXT NOT NULL,
      expires_at    BIGINT NOT NULL,
      used_at       BIGINT,
      created_at    BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS mcp_upload_tickets_user_ref_idx"
    " ON mcp_upload_tickets (user_id, client_ref, id DESC)",
    # one LIVE (unused, unexpired-at-issue) ticket per (user, client_ref):
    # used_at IS NULL marks unused; the issue() user-row lock makes the
    # lookup+insert pair race-free, this index is the durable backstop
    """
    CREATE UNIQUE INDEX IF NOT EXISTS mcp_upload_tickets_live_one_idx
      ON mcp_upload_tickets (user_id, client_ref)
      WHERE used_at IS NULL
    """,
    # ── tool_favorites: 我的收藏 ──
    # A user's pinned tools. Drives the web favorites list and the MCP
    # tools/list selection (meta + first-party + favorites). Rows for a
    # tool that later stops being visible simply drop out of every
    # projection (the catalog visibility predicate filters them).
    """
    CREATE TABLE IF NOT EXISTS tool_favorites (
      user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      tool_key   TEXT   NOT NULL,
      created_at BIGINT NOT NULL,
      PRIMARY KEY (user_id, tool_key)
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
        # ── 格式/tag 参考表装载 (表驱动): 首装种子补齐 + 运行态回装 ──
        # (幂等; 计数仅供测试断言, 静默)
        from governance.formats import sync_reference_tables
        sync_reference_tables(con, ts)
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
        # Routine cleanup — persistence mutation authority is the repo
        # primitives (same ones the hourly housekeeping consumes); local
        # imports keep the schema module decoupled at load time.
        from repo.sessions import delete_expired_oauth, delete_expired
        from repo.upload_usage import purge_before
        delete_expired_oauth(con, ts)
        delete_expired(con, ts)
        # Quota facts only serve the rolling 24h window (queries read
        # created_at > now-24h); older rows have no business value.
        # purge_before uses created_at <= cutoff — the canonical window
        # complement of created_at > now-24h.
        purge_before(con, ts - 24 * 3600)
