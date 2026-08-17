"""Shared E2E cleanup — deletes the ephemeral test user from PostgreSQL.

E2E scripts register real users through the public API; the API has no
self-delete, so cleanup goes direct to the DB. After the SQLite→PG
migration the DB is PostgreSQL (DATABASE_URL from /etc/flashshare.env).
"""

import os
import psycopg2


def cleanup_user(username: str) -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        # non-server environments (local dev) — read the canonical env file
        for line in open("/etc/flashshare.env"):
            if line.startswith("DATABASE_URL="):
                url = line.strip().split("=", 1)[1]
                break
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    con = psycopg2.connect(url)
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        if not row:
            return
        uid = row[0]
        cur.execute("DELETE FROM uploads WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))
        con.commit()
    finally:
        con.close()
