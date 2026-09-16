# Architecture Unification Deployment — 2026-09-16

One-time production deployment plan. Every step is executed manually on
owner order; nothing here runs automatically from application code.

## 1. Backup

Standard daily backup (backup.sh path) + verify SHA256SUMS before
touching anything:

    sudo /opt/flashshare/scripts/backup.sh
    # or wait for the 04:30 UTC timer and verify the latest stamp exists

## 2. DB cleanup (single transaction)

    sudo -u postgres psql -d flashshare       -f docs/plan/ARCHITECTURE_UNIFICATION_DB_CLEANUP.sql

The script is BEGIN/COMMIT-wrapped with precondition + postcondition
assertions; any drift aborts and rolls back the whole transaction.

## 3. Verify DB invariants (read-only)

    sudo -u postgres psql -d flashshare -c \
      "select code, badge_key, ingress_files_24h from plans order by sort_order;"
    # expect: free/tier_free/100, plus/tier_plus/500, pro/tier_pro/2000
    sudo -u postgres psql -d flashshare -c \
      "select count(*) from platform_tools where accept_json::jsonb ?| ARRAY[]::text[];"
    # expect 0 MIME rows: verify via
    sudo -u postgres psql -d flashshare -c \
      "select count(*) from platform_tools pt where pt.accept_json is not null and exists (select 1 from jsonb_array_elements(pt.accept_json::jsonb) el where el->>0 not like '.%');"
    # expect 0

## 4. Remove dead env key

    sudo sed -i.bak-acct-unify '/^FLASHSHARE_DB=/d' /etc/flashshare.env

## 5. Archive the retired SQLite file (rollback evidence — do NOT rm)

    sudo mv /var/lib/flashshare/flashshare.sqlite       /var/backups/flashshare/retired-$(date -u +%Y%m%dT%H%M%SZ).sqlite

Final deletion only after the new version is confirmed stable.

## 6. Code switch

    cd /opt/flashshare
    git fetch origin
    git rev-parse origin/main   # confirm expected SHA
    git merge --ff-only origin/main

## 7. Restart + health

    sudo systemctl restart flashshare-api
    curl -fsS http://127.0.0.1:8000/healthz

## 8. Logs

    sudo journalctl -u flashshare-api -n 50 --no-pager
    # expect: "catalog rows validated" in the startup sequence

## 9. Smoke

- Home page renders; tool grid loads.
- Tool page for an image tool accepts a .png upload through execution.
- /api/v1/me (Bearer) returns access.membership, no plan key.
- Samelabs backoffice /samelabs loads for a status=samelabs account.
