#!/usr/bin/env python3
"""SQLite → PostgreSQL one-shot migration with reconciliation.

Usage (server, root env for DATABASE_URL):
    sudo sh -c 'set -a; . /etc/flashshare.env; set +a; \
        /usr/bin/python3 scripts/migrate_sqlite_to_pg.py'

Safety:
  - Target tables are TRUNCATED first (idempotent re-run; PG is now the
    source of truth so old SQLite data is re-copyable at any time).
  - Preserves user/upload IDs via OVERRIDING SYSTEM VALUE.
  - Re-aligns identity sequences after copy.
  - Reconciles: row counts + full-content sample hash per table.

Source of truth for what "migrated" means: docs/plan/A2-ddl-draft.md.
"""

import hashlib
import json
import os
import sqlite3
import sys

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
from database import db  # noqa: E402

SQLITE_PATH = "/var/lib/flashshare/flashshare.sqlite"

# (table, pk, identity-holding) — order matters (FK dependencies)
TABLES = [
    ("users", "id", True),
    ("sessions", "token_hash", False),
    ("oauth_states", "state", False),
    ("uploads", "id", True),
]


def row_hash(rows: list, pk: str) -> str:
    """Stable content hash: sorted by pk, canonical JSON of each row."""
    def norm(v):
        if v is None:
            return None
        if isinstance(v, bytes):
            return v.hex()
        return v
    canon = [json.dumps({k: norm(v) for k, v in dict(r).items()}, sort_keys=True,
                        ensure_ascii=False) for r in
             sorted(rows, key=lambda r: str(r[pk]))]
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()


def main() -> None:
    lite = sqlite3.connect(SQLITE_PATH)
    lite.row_factory = sqlite3.Row

    with db() as con:
        cur = con.cursor()
        # 0. Truncate target (children first)
        cur.execute("TRUNCATE uploads, oauth_states, sessions, users RESTART IDENTITY CASCADE")
        print("target truncated")

        for table, pk, has_identity in TABLES:
            if table == "sessions":
                # SQLite (no FK enforcement) accumulated 59 orphan sessions
                # pointing at deleted users — dead tokens that can never
                # authenticate. Drop them at migration; PG FK makes this
                # class of dirt impossible going forward.
                rows = lite.execute(
                    "SELECT s.* FROM sessions s JOIN users u ON u.id = s.user_id").fetchall()
                dropped = lite.execute(
                    "SELECT COUNT(*) FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
                    "WHERE u.id IS NULL").fetchone()[0]
                if dropped:
                    print(f"sessions: dropping {dropped} orphan rows (user deleted)")
            else:
                rows = lite.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"{table}: 0 rows (nothing to copy)")
                continue
            cols = [d[0] for d in lite.execute(f"SELECT * FROM {table} LIMIT 1").description]
            collist = ",".join(f'"{c}"' for c in cols)
            placeholders = ",".join(["%s"] * len(cols))
            override = "OVERRIDING SYSTEM VALUE" if has_identity else ""
            sql = f'INSERT INTO {table} ({collist}) OVERRIDING SYSTEM VALUE VALUES ({placeholders})' if has_identity else f'INSERT INTO {table} ({collist}) VALUES ({placeholders})'
            for r in rows:
                cur.execute(sql, tuple(r))
            if has_identity:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{pk}'), "
                    f"COALESCE((SELECT MAX({pk}) FROM {table}), 1))")
            # Reconcile
            pg_rows = con.execute(f"SELECT * FROM {table}").fetchall()
            src_h, dst_h = row_hash(rows, pk), row_hash(pg_rows, pk)
            status = "OK " if (len(rows) == len(pg_rows) and src_h == dst_h) else "FAIL"
            print(f"{table}: {len(rows)} rows -> PG {len(pg_rows)} rows  hash {status}")
            if status == "FAIL":
                raise SystemExit(f"RECONCILIATION FAILED for {table}")

    lite.close()
    print("MIGRATION COMPLETE — all tables reconciled (count + full hash)")


if __name__ == "__main__":
    main()
