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

-- ── -1. create the ingress_requests side-effect receipt table (new
--        fact — no history exists, nothing to backfill) ──
CREATE TABLE ingress_requests (
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
);

CREATE UNIQUE INDEX ingress_requests_ref_live_idx
  ON ingress_requests(user_id, request_ref)
  WHERE status IN ('running','ok');

CREATE UNIQUE INDEX orders_user_pending_plan_uk
  ON orders(user_id)
  WHERE kind = 'plan' AND status = 'pending';

CREATE UNIQUE INDEX orders_user_item_pending_pkg_uk
  ON orders(user_id, item_id)
  WHERE kind = 'package' AND status = 'pending';

-- ── -1b. order field-combination invariants (kind/snapshot/status/
--         timestamps/granted). Historical rows violating these are
--         deployment-blocking facts, not auto-repairs. ──
ALTER TABLE orders
  ADD CONSTRAINT orders_kind_ck
    CHECK (kind IN ('package','plan')),
  ADD CONSTRAINT orders_kind_snapshot_ck
    CHECK ((kind = 'package' AND credits_snapshot IS NOT NULL
            AND credits_snapshot > 0)
        OR (kind = 'plan' AND credits_snapshot IS NULL)),
  ADD CONSTRAINT orders_plan_status_ck
    CHECK (kind <> 'plan' OR status IN ('pending','paid','failed')),
  ADD CONSTRAINT orders_plan_not_granted_ck
    CHECK (kind <> 'plan' OR granted = FALSE),
  ADD CONSTRAINT orders_plan_not_reversed_ck
    CHECK (kind <> 'plan' OR status NOT IN ('refunded','disputed')),
  ADD CONSTRAINT orders_pending_failed_fields_ck
    CHECK (status NOT IN ('pending','failed')
           OR (granted = FALSE AND paid_at IS NULL AND refunded_at IS NULL)),
  ADD CONSTRAINT orders_pending_identity_ns_ck
    CHECK (status IN ('pending','failed') OR provider_order_id IS NOT NULL),
  ADD CONSTRAINT orders_pending_identity_ch_ck
    CHECK (status NOT IN ('pending','failed') OR provider_order_id IS NULL
           OR provider_order_id LIKE 'ch:%'),
  ADD CONSTRAINT orders_package_paid_fields_ck
    CHECK (kind <> 'package' OR status <> 'paid'
           OR (granted = TRUE AND paid_at IS NOT NULL AND refunded_at IS NULL
               AND provider_order_id IS NOT NULL)),
  ADD CONSTRAINT orders_package_reversal_fields_ck
    CHECK (kind <> 'package' OR status NOT IN ('refunded','disputed')
           OR (granted = TRUE AND paid_at IS NOT NULL AND refunded_at IS NOT NULL
               AND provider_order_id IS NOT NULL)),
  ADD CONSTRAINT orders_plan_paid_fields_ck
    CHECK (kind <> 'plan' OR status <> 'paid'
           OR (granted = FALSE AND paid_at IS NOT NULL AND refunded_at IS NULL
               AND provider_order_id LIKE 'sub:%')),
  ADD CONSTRAINT orders_paid_fields_ck
    CHECK (status <> 'paid' OR paid_at IS NOT NULL),
  ADD CONSTRAINT orders_reversal_fields_ck
    CHECK (status NOT IN ('refunded','disputed') OR refunded_at IS NOT NULL),
  ADD CONSTRAINT orders_non_reversal_refund_ts_ck
    CHECK (status IN ('refunded','disputed') OR refunded_at IS NULL),
  ADD CONSTRAINT orders_granted_kind_ck
    CHECK (granted = FALSE OR kind = 'package');

-- ── 0. add the new nullable column (transactional DDL: a later preflight
--        failure rolls this back together with everything else) ──
ALTER TABLE tool_calls
  ADD COLUMN commission_rate_snapshot INTEGER,
  ADD COLUMN invocation_hash TEXT;

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

  SELECT COUNT(*) INTO violators FROM orders
    WHERE (kind NOT IN ('package','plan'))
       OR (kind = 'package' AND (credits_snapshot IS NULL
                                 OR credits_snapshot <= 0))
       OR (kind = 'plan' AND credits_snapshot IS NOT NULL)
       OR (kind = 'plan' AND status NOT IN ('pending','paid','failed'))
       OR (kind = 'plan' AND granted)
       OR (status IN ('pending','failed')
           AND (granted OR paid_at IS NOT NULL OR refunded_at IS NOT NULL))
       OR (status = 'paid' AND paid_at IS NULL)
       OR (status IN ('refunded','disputed') AND refunded_at IS NULL)
       OR (status NOT IN ('refunded','disputed') AND refunded_at IS NOT NULL)
       OR (granted AND kind <> 'package')
       OR (kind = 'plan' AND status IN ('refunded','disputed'))
       OR (status IN ('paid','refunded','disputed')
           AND provider_order_id IS NULL)
       OR (status = 'pending' AND provider_order_id IS NOT NULL
           AND provider_order_id NOT LIKE 'ch:%')
       OR (kind = 'package' AND status = 'paid'
           AND (NOT granted OR paid_at IS NULL OR refunded_at IS NOT NULL
                OR provider_order_id IS NULL))
       OR (kind = 'package' AND status IN ('refunded','disputed')
           AND (NOT granted OR paid_at IS NULL OR refunded_at IS NULL
                OR provider_order_id IS NULL))
       OR (kind = 'plan' AND status = 'paid'
           AND (granted OR paid_at IS NULL OR refunded_at IS NOT NULL
                OR provider_order_id IS NULL
                OR provider_order_id NOT LIKE 'sub:%'));
  IF violators > 0 THEN
    RAISE EXCEPTION 'order invariant violators: % rows', violators;
  END IF;

  -- duplicate pending intents are deployment-blocking facts
  SELECT COUNT(*) INTO violators FROM (
    SELECT user_id FROM orders
      WHERE kind='plan' AND status='pending'
      GROUP BY user_id HAVING COUNT(*) > 1
    UNION ALL
    SELECT user_id FROM orders
      WHERE kind='package' AND status='pending'
      GROUP BY user_id, item_id HAVING COUNT(*) > 1
  ) d;
  IF violators > 0 THEN
    RAISE EXCEPTION 'duplicate pending checkout intents: % groups', violators;
  END IF;

  SELECT COUNT(*) INTO violators FROM tool_calls
    WHERE status NOT IN ('running','ok','failed','insufficient')
       OR cost_snapshot < 0
       OR (status = 'running' AND finished_at IS NOT NULL)
       OR (status <> 'running' AND finished_at IS NULL)
       OR (status <> 'ok'
           AND (upload_id IS NOT NULL OR result_public_id IS NOT NULL
                OR result_json IS NOT NULL))
       OR (status = 'running' AND commission_rate_snapshot IS NULL)
       OR (status IN ('running','ok') AND invocation_hash IS NULL)
       OR (commission_rate_snapshot IS NOT NULL
           AND (commission_rate_snapshot < 0
                OR commission_rate_snapshot > 100));
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
  ADD CONSTRAINT tool_calls_live_invocation_hash_ck
    CHECK (status NOT IN ('running','ok') OR invocation_hash IS NOT NULL),
  ADD CONSTRAINT tool_calls_commission_snapshot_range_ck
    CHECK (commission_rate_snapshot IS NULL
           OR commission_rate_snapshot BETWEEN 0 AND 100),
  ADD CONSTRAINT tool_calls_running_commission_snapshot_ck
    CHECK (status <> 'running'
           OR commission_rate_snapshot IS NOT NULL),
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
