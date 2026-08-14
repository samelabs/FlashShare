import os
import sqlite3

from config import DB_PATH
from utils import now, generate_public_id


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


# New users schema: local account (username/email/password) + optional X binding
_NEW_USERS_SQL = """
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL UNIQUE,
      email TEXT UNIQUE,
      password_hash TEXT,
      display_name TEXT NOT NULL,
      bio TEXT,
      avatar_url TEXT,
      x_user_id TEXT UNIQUE,
      x_username TEXT,
      x_display_name TEXT,
      x_avatar_url TEXT,
      x_profile_url TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      created_at INTEGER NOT NULL,
      last_login_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
"""

_OTHER_TABLES_SQL = """
    CREATE TABLE IF NOT EXISTS sessions (
      token_hash TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      expires_at INTEGER NOT NULL,
      created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS oauth_states (
      state TEXT PRIMARY KEY,
      verifier TEXT NOT NULL,
      expires_at INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      bind_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS uploads (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      ip TEXT NOT NULL,
      gokapi_id TEXT NOT NULL,
      public_id TEXT,
      file_name TEXT NOT NULL,
      content_type TEXT NOT NULL,
      size_bytes INTEGER NOT NULL,
      expiry_days INTEGER NOT NULL,
      allowed_downloads INTEGER NOT NULL,
      expire_at INTEGER,
      url_download TEXT NOT NULL,
      url_hotlink TEXT,
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS uploads_user_created_idx ON uploads(user_id, created_at);
    CREATE INDEX IF NOT EXISTS uploads_ip_created_idx ON uploads(ip, created_at);
"""

# posts table (legacy, zero references since the tools refactor) is dropped at
# init: its DDL was here and its indexes lived inside _OTHER_TABLES_SQL.


def init_db() -> None:
    with db() as con:
        users_exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()

        if users_exists:
            cols = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
        else:
            cols = set()

        if not cols:
            # Fresh database — create with new schema
            con.executescript(_NEW_USERS_SQL)
        elif "email" not in cols:
            # Old schema — rebuild table with new columns, preserve data + IDs
            # Save profile_url mapping before rebuild (column may not exist in all old schemas)
            profile_urls = {}
            if "profile_url" in cols:
                for row in con.execute("SELECT id, profile_url FROM users WHERE profile_url IS NOT NULL"):
                    profile_urls[row["id"]] = row["profile_url"]

            con.executescript("""
                CREATE TABLE _users_new (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT NOT NULL UNIQUE,
                  email TEXT UNIQUE,
                  password_hash TEXT,
                  display_name TEXT NOT NULL,
                  bio TEXT,
                  avatar_url TEXT,
                  x_user_id TEXT UNIQUE,
                  x_username TEXT,
                  x_display_name TEXT,
                  x_avatar_url TEXT,
                  x_profile_url TEXT,
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at INTEGER NOT NULL,
                  last_login_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO _users_new
                  (id, x_user_id, username, display_name, avatar_url, status, created_at, last_login_at, updated_at)
                SELECT
                  id, x_user_id, username, display_name, avatar_url, status, created_at, last_login_at, updated_at
                FROM users;
            """)
            # Populate X fields from copied data + saved profile_urls
            for row in con.execute("SELECT id, username, display_name, avatar_url, x_user_id FROM _users_new WHERE x_user_id IS NOT NULL"):
                pid = profile_urls.get(row["id"], f"https://x.com/{row['username']}" if row["username"] else "")
                con.execute(
                    "UPDATE _users_new SET x_username=?, x_display_name=?, x_avatar_url=?, x_profile_url=? WHERE id=?",
                    (row["username"], row["display_name"], row["avatar_url"], pid, row["id"]),
                )
            con.executescript("DROP TABLE users; ALTER TABLE _users_new RENAME TO users;")
        # else: already new schema, nothing to do

        # Create remaining tables
        con.executescript(_OTHER_TABLES_SQL)

        # Ensure oauth_states has bind_user_id (for old tables without it)
        if con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='oauth_states'").fetchone():
            oauth_cols = {row["name"] for row in con.execute("PRAGMA table_info(oauth_states)").fetchall()}
            if "bind_user_id" not in oauth_cols:
                con.execute(
                    "ALTER TABLE oauth_states ADD COLUMN bind_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"
                )

        # Uploads: ensure public_id column + backfill
        if con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='uploads'").fetchone():
            upload_cols = {row["name"] for row in con.execute("PRAGMA table_info(uploads)").fetchall()}
            if "public_id" not in upload_cols:
                con.execute("ALTER TABLE uploads ADD COLUMN public_id TEXT")
            missing = con.execute(
                "SELECT id FROM uploads WHERE public_id IS NULL OR public_id = ''"
            ).fetchall()
            for row in missing:
                con.execute(
                    "UPDATE uploads SET public_id = ? WHERE id = ?",
                    (generate_public_id(con), row["id"]),
                )
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uploads_public_id_idx ON uploads(public_id)")

        # Cleanup expired
        con.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now(),))
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))

        # Drop legacy posts table (zero references since tools refactor)
        con.execute("DROP TABLE IF EXISTS posts")
