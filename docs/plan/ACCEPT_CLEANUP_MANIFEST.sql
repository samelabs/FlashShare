#!/bin/sh
# Production accept_json MIME cleanup manifest — READ-ONLY preview.
# 190 rows total (188 private test fixtures → DELETE; 2 unlisted echo tools
# → UPDATE_TO_CANONICAL_EXT). NOT EXECUTED; awaits owner deploy/data-cleanup order.

# ── Phase 1: delete test-fixture tools (private, zero/near-zero real use) ──
# 188 rows: owner usernames are throwaway test accounts (gate*/smoke/uitest/
# edgecase/acctest/s3test/mtest/fam_* etc.), visibility=private, no public exposure.
DELETE FROM pricing WHERE tool_id IN (SELECT tool_key FROM platform_tools WHERE
  accept_json IS NOT NULL AND accept_json NOT IN ('null','')
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(accept_json::jsonb) el WHERE el->>0 NOT LIKE '.%')
  AND visibility = 'private');
DELETE FROM platform_tools WHERE
  accept_json IS NOT NULL AND accept_json NOT IN ('null','')
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(accept_json::jsonb) el WHERE el->>0 NOT LIKE '.%')
  AND visibility = 'private';

# ── Phase 2: rewrite the 2 real unlisted echo tools to canonical ext tokens ──
UPDATE platform_tools SET
  accept_json = '[".json"]',
  updated_at = EXTRACT(EPOCH FROM now())::BIGINT
WHERE tool_key = 's3test57761/echo_test_s3';
UPDATE platform_tools SET
  accept_json = '[".json",".txt"]',
  updated_at = EXTRACT(EPOCH FROM now())::BIGINT
WHERE tool_key = 's3test57761/echo_demo';

# ── Phase 3: retired-schema artifacts (explicit one-time cleanup) ──
DROP TABLE IF EXISTS user_plans;                                  -- 0 rows
ALTER TABLE packages DROP COLUMN IF EXISTS legacy_creem_product_id; -- 0 non-null
DROP INDEX IF EXISTS tool_calls_client_ref_ok_idx;                -- absent
DROP INDEX IF EXISTS tool_calls_client_ref_idx;                   -- absent

# ── Phase 4: dead files/env (host-level, outside DB) ──
# sed -i '/^FLASHSHARE_DB=/d' /etc/flashshare.env
# mv /var/lib/flashshare/flashshare.sqlite /var/backups/flashshare/retired-$(date -u +%Y%m%d).sqlite
