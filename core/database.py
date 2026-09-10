"""Database layer — PostgreSQL (psycopg2) edition.

Connection strategy: one connection per db() call. Callers use
`with db() as con:`; success commits, exceptions roll back, connections return
to the pool. RealDictCursor keeps row access dict-shaped across the codebase.

init_db() is the single schema/bootstrap source for runtime tables and routine
expired-state cleanup.
"""

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from config import DB_PATH  # noqa: F401 (dev tooling may still reference it)


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
    """SQLite-style facade over psycopg2 connection."""

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
    "CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id)",
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
    # ── uploads: durable public/result objects ──
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
      expiry_days       INT NOT NULL,
      allowed_downloads INT NOT NULL,
      expire_at         BIGINT,
      url_download      TEXT NOT NULL,
      url_hotlink       TEXT,
      created_at        BIGINT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uploads_public_id_idx ON uploads(public_id)",
    "CREATE INDEX IF NOT EXISTS uploads_user_created_idx ON uploads(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS uploads_ip_created_idx ON uploads(ip, created_at)",
    # ── upload_usage: ingress facts only (share source + tool source) ──
    # Generated tool results do not land here, so daily quota measures bytes/files
    # entering the platform rather than double-counting transformed output.
    """
    CREATE TABLE IF NOT EXISTS upload_usage (
      id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      ip         TEXT NOT NULL,
      size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
      created_at BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS upload_usage_user_created_idx ON upload_usage(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS upload_usage_ip_created_idx ON upload_usage(ip, created_at)",
    # ── api_tokens ──
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
    "CREATE INDEX IF NOT EXISTS api_tokens_user_idx ON api_tokens(user_id)",
    # ── ledger ──
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
    "CREATE INDEX IF NOT EXISTS ledger_user_created_idx ON ledger(user_id, created_at)",
    # ── tool_calls: durable invocation-attempt facts ──
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
    # client_ref scope is user + tool. A running row owns execution; an ok row
    # owns replay. Failed attempts deliberately leave the key reusable.
    "DROP INDEX IF EXISTS tool_calls_client_ref_ok_idx",
    "DROP INDEX IF EXISTS tool_calls_client_ref_idx",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tool_calls_client_ref_live_idx
      ON tool_calls(user_id, tool_id, client_ref)
      WHERE status IN ('running','ok')
    """,
    "CREATE INDEX IF NOT EXISTS tool_calls_user_time_idx ON tool_calls(user_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS tool_calls_tool_time_idx ON tool_calls(tool_id, started_at DESC)",
    # ── pricing ──
    """
    CREATE TABLE IF NOT EXISTS pricing (
      tool_id    TEXT PRIMARY KEY,
      cost       BIGINT NOT NULL DEFAULT 0,
      enabled    BOOLEAN NOT NULL DEFAULT TRUE,
      updated_at BIGINT NOT NULL,
      updated_by BIGINT
    )
    """,
    # ── plans ──
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
    # ── packages ──
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
    # ── orders ──
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
    "CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id, created_at DESC)",
    # ── settings ──
    """
    CREATE TABLE IF NOT EXISTS settings (
      key        TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      updated_at BIGINT NOT NULL,
      updated_by BIGINT
    )
    """,
    # ── platform_tools ──
    """
    CREATE TABLE IF NOT EXISTS platform_tools (
      id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      tool_key       TEXT NOT NULL,
      owner_id       BIGINT REFERENCES users(id) ON DELETE CASCADE,
      tool_id        TEXT NOT NULL,
      execution      TEXT NOT NULL DEFAULT 'local',
      tool_type      TEXT NOT NULL DEFAULT 'produce',
      label_json     TEXT NOT NULL,
      desc_json      TEXT,
      sort_order     INT NOT NULL DEFAULT 0,
      featured       BOOLEAN NOT NULL DEFAULT FALSE,
      icon           TEXT NOT NULL DEFAULT 'wand',
      accept_json    TEXT,
      presets_json   TEXT,
      multi          BOOLEAN NOT NULL DEFAULT FALSE,
      pass_through   BOOLEAN NOT NULL DEFAULT FALSE,
      visibility     TEXT NOT NULL DEFAULT 'public',
      endpoint_json  TEXT,
      params_json    TEXT,
      enabled        BOOLEAN NOT NULL DEFAULT TRUE,
      trusted        BOOLEAN NOT NULL DEFAULT FALSE,
      created_at     BIGINT NOT NULL,
      updated_at     BIGINT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS platform_tools_key_idx ON platform_tools(tool_key)",
    "CREATE INDEX IF NOT EXISTS platform_tools_owner_idx ON platform_tools(owner_id)",
    # ── withdrawals ──
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
    "CREATE INDEX IF NOT EXISTS withdrawals_user_idx ON withdrawals(user_id)",
    # additive schema convergence
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_channel TEXT",
    "ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS desc_json TEXT",
    "ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS sort_order INT NOT NULL DEFAULT 0",
    "ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS featured BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE platform_tools ADD COLUMN IF NOT EXISTS pass_through BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE platform_tools ALTER COLUMN icon SET DEFAULT 'wand'",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS period_days INT NOT NULL DEFAULT 30",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS period TEXT NOT NULL DEFAULT 'monthly'",
    "ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS cancel_scheduled_at BIGINT",
    "ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS last_grant_period_start BIGINT",
    "ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS updated_at BIGINT",
    "ALTER TABLE user_plans DROP CONSTRAINT IF EXISTS user_plans_status_check",
    """ALTER TABLE user_plans ADD CONSTRAINT user_plans_status_check
       CHECK (status IN ('active','past_due','canceled','expired'))""",
    # PLUS seed
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
        con.execute("DELETE FROM oauth_states WHERE expires_at < %s", (ts,))
        con.execute("DELETE FROM sessions WHERE expires_at < %s", (ts,))
        # Quota facts only need the rolling 24h window; keep a small audit margin.
        con.execute("DELETE FROM upload_usage WHERE created_at < %s", (ts - 7 * 24 * 3600,))
