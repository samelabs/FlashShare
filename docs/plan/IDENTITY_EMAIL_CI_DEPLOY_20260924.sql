-- Identity: case-insensitive email uniqueness — one-time deploy SQL
--
-- database.py creates users_email_lower_uk at startup (init_db). On an
-- EXISTING database that already holds case-variant duplicate emails the
-- index build fails and the new process refuses to start (fail loud).
-- Run this BEFORE switching code so the deploy stops here instead.
--
-- Rules of engagement (project policy):
--   * Violations ABORT the transaction — no data repair, no silent
--     normalization. Surface the rows for manual owner review.
--   * Run inside ONE transaction.

BEGIN;

-- ── 0. preflight: case-variant duplicate emails (must be empty) ──
DO $$
DECLARE
  dup RECORD;
  n   INT := 0;
BEGIN
  FOR dup IN
    SELECT lower(email) AS email_ci, array_agg(id ORDER BY id) AS user_ids
    FROM users
    WHERE email IS NOT NULL
    GROUP BY lower(email)
    HAVING COUNT(*) > 1
  LOOP
    n := n + 1;
    RAISE NOTICE 'duplicate email %: users %', dup.email_ci, dup.user_ids;
  END LOOP;
  IF n > 0 THEN
    RAISE EXCEPTION 'users_email_lower_uk preflight: % case-variant duplicate email group(s) — owner review required', n;
  END IF;
END $$;

-- ── 1. constraint (same definition as database.py) ──
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uk
  ON users (lower(email)) WHERE email IS NOT NULL;

COMMIT;
