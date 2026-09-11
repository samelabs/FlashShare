"""Database layer — PostgreSQL (psycopg2) edition.

Connection strategy: one connection per db() call (mirrors the SQLite
edition's pattern). Callers use `with db() as con:` — the context manager
commits on success, rolls back on exception, and always closes.

Row access: RealDictCursor → every cursor row is a plain dict with
column-name keys, matching the old sqlite3.Row usage.

init_db(): CREATE TABLE IF NOT EXISTS for the full schema + routine
expired-state cleanup. No legacy repair logic — history lives in git and
in the SQLite→PG migration script (scripts/migrate_sqlite_to_pg.py).
"""

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from config import DB_PATH  # noqa: F401  (kept: dev tooling may still reference it)


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
    """SQLite-style facade over psycopg2 connection.

    sqlite3.Connection exposes .execute() directly; psycopg2 requires a
    cursor. Callers throughout the codebase use `con.execute(...)` with
    dict-style row access — this facade preserves both, so the 25 call
    sites need zero structural change.
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
# Full PostgreSQL schema: 4 legacy tables (structure-equivalent ports of
# the SQLite originals) + 8 platform tables (economics core). All
# statements are CREATE ... IF NOT EXISTS — safe on every boot.
# Column-by-column rationale: docs/plan/A2-ddl-draft.md (definitive).

_SCHEMA_SQL = (
    # ── legacy: users ──
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
      created_at     BIGINT NOT NULL,
      last_login_at  BIGINT NOT NULL,
      updated_at     BIGINT NOT NULL DEFAULT 0
    )
    """,
    # ── legacy: sessions ──
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
    # ── legacy: oauth_states ──
    """
    CREATE TABLE IF NOT EXISTS oauth_states (
      state        TEXT PRIMARY KEY,
      verifier     TEXT NOT NULL,
      expires_at   BIGINT NOT NULL,
      created_at   BIGINT NOT NULL,
      bind_user_id BIGINT REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    # ── legacy: uploads ──
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
      finished_at      BIGINT
    )
    """,
    # Invocation identity is user + tool + client_ref. A running row owns the
    # execution; an ok row owns replay. Failed attempts leave the key reusable.
    "DROP INDEX IF EXISTS tool_calls_client_ref_ok_idx",
    "DROP INDEX IF EXISTS tool_calls_client_ref_idx",
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
      updated_by BIGINT
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
      creem_price_id   TEXT,
      wechat_price_id  TEXT,
      sort_order       INT NOT NULL DEFAULT 0,
      active           BOOLEAN NOT NULL DEFAULT TRUE,
      created_at       BIGINT NOT NULL,
      updated_at       BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_plans (
      user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      plan_id    BIGINT NOT NULL REFERENCES plans(id),
      started_at BIGINT NOT NULL,
      renews_at  BIGINT,
      status     TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','past_due','canceled','expired')),
      cancel_scheduled_at     BIGINT,
      last_grant_period_start BIGINT,
      updated_at BIGINT,
      PRIMARY KEY (user_id, plan_id)
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
      creem_price_id   TEXT,
      wechat_price_id  TEXT,
      sort_order       INT NOT NULL DEFAULT 0,
      active           BOOLEAN NOT NULL DEFAULT TRUE,
      created_at       BIGINT NOT NULL,
      updated_at       BIGINT NOT NULL
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
      granted           BOOLEAN NOT NULL DEFAULT FALSE,
      created_at        BIGINT NOT NULL,
      paid_at           BIGINT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id, created_at DESC)
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
      txn_ref      TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS withdrawals_user_idx ON withdrawals(user_id)
    """,
    # users.payout_channel (withdrawal destination, operator-read) — additive
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_channel TEXT",
    # ── subscription (plans/user_plans v3, 2026-09-02) ──
    # 工具简介 (tool modes v3 补: 名称/简介/模式/使用 — desc 与 label 同形 str|dict)
    "ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS desc_json TEXT",
    # legacy presets 通道退场: params_json 是唯一参数声明 (存量行已确认全空)
    "ALTER TABLE platform_tools DROP COLUMN IF EXISTS presets_json",

    # 推荐与排序 (管理后台遴选的权威全局工具呈现顺序)
    "ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS sort_order INT NOT NULL DEFAULT 0",
    "ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS featured BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS pass_through BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE platform_tools ALTER COLUMN icon SET DEFAULT 'wand'",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS period_days INT NOT NULL DEFAULT 30",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS period TEXT NOT NULL DEFAULT 'monthly'",
    "ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS cancel_scheduled_at BIGINT",
    "ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS last_grant_period_start BIGINT",
    "ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS updated_at BIGINT",
    # 状态约束: 旧表无 CHECK; DROP IF NOT EXISTS 语义 → 先删后加(幂等)
    "ALTER TABLE user_plans DROP CONSTRAINT IF EXISTS user_plans_status_check",
    """ALTER TABLE user_plans ADD CONSTRAINT user_plans_status_check
       CHECK (status IN ('active','past_due','canceled','expired'))""",
    # PLUS 档种子 (幂等; creem_price_id 留空待运营绑定)
    """INSERT INTO plans (code, name, monthly_credits, period_days, period,
                         price_cents, sort_order, active, created_at, updated_at)
       VALUES ('plus', 'PLUS', 10000, 30, 'monthly', 0, 0, TRUE,
               EXTRACT(EPOCH FROM now())::BIGINT, EXTRACT(EPOCH FROM now())::BIGINT)
       ON CONFLICT (code) DO NOTHING""",
)


def init_db() -> None:
    from utils import now
    ts = now()
    with db() as con:
        for stmt in _SCHEMA_SQL:
            con.execute(stmt)
        # Routine cleanup — same behaviour as the SQLite edition
        con.execute("DELETE FROM oauth_states WHERE expires_at < %s", (ts,))
        con.execute("DELETE FROM sessions WHERE expires_at < %s", (ts,))
        # Quota facts only serve the rolling 24h window (queries read
        # created_at > now-24h); older rows have no business value.
        con.execute("DELETE FROM upload_usage WHERE created_at < %s",
                    (ts - 24 * 3600,))
