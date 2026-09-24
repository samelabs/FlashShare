-- Withdrawal settlement — one-time deploy SQL (manual, NOT startup migration)
--
-- database.py carries the same shape for fresh creates. On an EXISTING
-- database run this BEFORE switching code: the new code writes
-- withdrawals.payout_channel at request time and the 'failed' status at
-- settlement; without this script withdrawal requests fail.
--
-- Changes (no data rewrite):
--   * withdrawals.payout_channel  — payout target frozen at request time
--     (legacy rows stay NULL; the console shows the current profile
--     value for them, labelled as such).
--   * withdrawals_status_ck gains 'failed' (approved → paid | failed;
--     failed returns the approved debit to the earn pool).
--
-- Rules of engagement: ONE transaction; any violation aborts.

BEGIN;

ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS payout_channel TEXT;

ALTER TABLE withdrawals DROP CONSTRAINT IF EXISTS withdrawals_status_ck;
ALTER TABLE withdrawals ADD CONSTRAINT withdrawals_status_ck
  CHECK (status IN ('pending','approved','rejected','paid','failed'));

COMMIT;
