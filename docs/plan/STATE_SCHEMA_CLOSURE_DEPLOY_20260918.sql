-- State schema closure — one-time deploy SQL (manual, NOT startup migration)
--
-- Applies the DB CHECK invariants introduced by
-- refactor/state-schema-writer-closure to an EXISTING deployed database.
-- database.py carries the same constraints for fresh creates; this script
-- is the explicit deploy-side counterpart.
--
-- Execute EXACTLY ONCE against the target deployed DB.
--
-- Rules of engagement (project policy):
--   * Violations ABORT the transaction — fail loud, no data repair,
--     no silent normalization, no compatibility columns.
--   * The project is not live: on violation, STOP the deploy and surface
--     the rows for manual owner review.
--   * Run inside ONE transaction; every constraint is named and stable.

BEGIN;

-- ── 1. pre-flight: existing data must satisfy every new invariant ──
DO $$
DECLARE violators BIGINT;
BEGIN
  SELECT COUNT(*) INTO violators FROM plans
    WHERE period_days <= 0 OR monthly_credits < 0 OR price_cents < 0
       OR sort_order < 0 OR ingress_files_24h < 0 OR ingress_bytes_24h < 0;
  IF violators > 0 THEN
    RAISE EXCEPTION 'plans invariant violations: % row(s)', violators;
  END IF;

  SELECT COUNT(*) INTO violators FROM packages
    WHERE credits <= 0 OR price_cents < 0;
  IF violators > 0 THEN
    RAISE EXCEPTION 'packages invariant violations: % row(s)', violators;
  END IF;

  SELECT COUNT(*) INTO violators FROM pricing WHERE cost < 0;
  IF violators > 0 THEN
    RAISE EXCEPTION 'pricing invariant violations: % row(s)', violators;
  END IF;

  SELECT COUNT(*) INTO violators FROM tool_calls
    WHERE status NOT IN ('running','ok','failed','insufficient')
       OR cost_snapshot < 0
       OR (status = 'running' AND finished_at IS NOT NULL)
       OR (status <> 'running' AND finished_at IS NULL)
       OR (status <> 'ok'
           AND (upload_id IS NOT NULL OR result_public_id IS NOT NULL
                OR result_json IS NOT NULL));
  IF violators > 0 THEN
    RAISE EXCEPTION 'tool_calls invariant violations: % row(s)', violators;
  END IF;

  SELECT COUNT(*) INTO violators FROM withdrawals
    WHERE status NOT IN ('pending','approved','rejected','paid');
  IF violators > 0 THEN
    RAISE EXCEPTION 'withdrawals invariant violations: % row(s)', violators;
  END IF;

  SELECT COUNT(*) INTO violators FROM orders
    WHERE status NOT IN ('pending','paid','failed','refunded','disputed')
       OR amount_cents < 0
       OR (credits_snapshot IS NOT NULL AND credits_snapshot < 0);
  IF violators > 0 THEN
    RAISE EXCEPTION 'orders invariant violations: % row(s)', violators;
  END IF;
END $$;

-- ── 2. add constraints — ONE-TIME deploy only ──
-- This block is intentionally NOT idempotent.
-- Re-running after a successful deploy fails loud on existing constraint
-- names; that is the deployment guard, not an error to suppress.
ALTER TABLE plans
  ADD CONSTRAINT plans_period_days_ck CHECK (period_days > 0),
  ADD CONSTRAINT plans_monthly_credits_ck CHECK (monthly_credits >= 0),
  ADD CONSTRAINT plans_price_cents_ck CHECK (price_cents >= 0),
  ADD CONSTRAINT plans_sort_order_ck CHECK (sort_order >= 0),
  ADD CONSTRAINT plans_ingress_files_ck CHECK (ingress_files_24h >= 0),
  ADD CONSTRAINT plans_ingress_bytes_ck CHECK (ingress_bytes_24h >= 0);

ALTER TABLE packages
  ADD CONSTRAINT packages_credits_ck CHECK (credits > 0),
  ADD CONSTRAINT packages_price_cents_ck CHECK (price_cents >= 0);

ALTER TABLE pricing
  ADD CONSTRAINT pricing_cost_ck CHECK (cost >= 0);

ALTER TABLE tool_calls
  ADD CONSTRAINT tool_calls_status_ck
    CHECK (status IN ('running','ok','failed','insufficient')),
  ADD CONSTRAINT tool_calls_cost_snapshot_ck CHECK (cost_snapshot >= 0),
  ADD CONSTRAINT tool_calls_running_open_ck
    CHECK (status != 'running' OR finished_at IS NULL),
  ADD CONSTRAINT tool_calls_terminal_closed_ck
    CHECK (status = 'running' OR finished_at IS NOT NULL),
  ADD CONSTRAINT tool_calls_non_ok_no_result_ck
    CHECK (status = 'ok'
           OR (upload_id IS NULL AND result_public_id IS NULL
               AND result_json IS NULL));

ALTER TABLE withdrawals
  ADD CONSTRAINT withdrawals_status_ck
    CHECK (status IN ('pending','approved','rejected','paid'));

ALTER TABLE orders
  ADD CONSTRAINT orders_status_ck
    CHECK (status IN ('pending','paid','failed','refunded','disputed')),
  ADD CONSTRAINT orders_amount_ck CHECK (amount_cents >= 0),
  ADD CONSTRAINT orders_credits_snapshot_ck
    CHECK (credits_snapshot IS NULL OR credits_snapshot >= 0);

COMMIT;
