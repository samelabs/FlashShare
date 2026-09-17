# Architecture Unification Deployment — 2026-09-16

One-time production cutover plan. Every step runs manually on owner order;
nothing here executes automatically from application code.

## 0. Authority preflight (read-only, BEFORE any write)

    cd /opt/flashshare
    git status --short                 # must be empty (production checkout clean)
    git rev-parse HEAD                 # record: currently deployed SHA
    git fetch origin
    TARGET_SHA=<owner-approved merged main SHA>
    test "$(git rev-parse origin/main)" = "$TARGET_SHA" \
      || { echo "origin/main != TARGET_SHA"; exit 1; }
    systemctl is-active flashshare-api # service currently active
    # DATABASE_URL identity check — confirm the env target is the production DB
    grep '^DATABASE_URL=' /etc/flashshare.env   # host/dbname must be the prod pair

Any preflight failure stops the deploy before a single write.

## 1. Target worktree (scripts/artifacts come from the SAME approved SHA)

Do NOT merge target code into /opt/flashshare to obtain scripts.

    git worktree add --detach /tmp/flashshare-deploy-$TARGET_SHA "$TARGET_SHA"

## 2. Backup with the TARGET-version script (app still online)

    sudo /tmp/flashshare-deploy-$TARGET_SHA/scripts/backup.sh

## 3. Verify backup artifacts

    ls -t /var/backups/flashshare/daily/ | head     # new pg-*.dump + gokapi-*.tar.gz
    ( cd /var/backups/flashshare/daily && sha256sum -c "$(ls -t SHA256SUMS-* | head -1)" )

## 4. Quiesced migration window (E→L, short maintenance)

    # E. stop service — no writes to platform_tools during the transaction,
    #    no systemd auto-restart of old code, no old startup recreating
    #    retired artifacts after cleanup.
    sudo systemctl stop flashshare-api
    # F. confirm inactive
    systemctl is-active flashshare-api && exit 1 || echo "service stopped"
    # G. single-transaction DB cleanup (target-version artifact)
    sudo -u postgres psql -d flashshare \
      -f /tmp/flashshare-deploy-$TARGET_SHA/docs/plan/ARCHITECTURE_UNIFICATION_DB_CLEANUP.sql
    # H. read-only postconditions (same vocabulary-aware check as the SQL)
    sudo -u postgres psql -d flashshare -v ON_ERROR_STOP=1 <<'SQL'
    WITH allowed(ext) AS (VALUES ('.3ds'), ('.7z'), ('.aac'), ('.avi'), ('.avif'), ('.blend'), ('.bmp'), ('.bz2'), ('.catpart'), ('.css'), ('.csv'), ('.doc'), ('.docx'), ('.dwg'), ('.dxf'), ('.fbx'), ('.flac'), ('.gif'), ('.glb'), ('.gltf'), ('.gz'), ('.heic'), ('.heif'), ('.htm'), ('.html'), ('.iam'), ('.ico'), ('.iges'), ('.igs'), ('.jpeg'), ('.jpg'), ('.js'), ('.json'), ('.m4a'), ('.md'), ('.mkv'), ('.mov'), ('.mp3'), ('.mp4'), ('.obj'), ('.odp'), ('.ods'), ('.odt'), ('.ogg'), ('.pdf'), ('.png'), ('.ppt'), ('.pptx'), ('.py'), ('.rar'), ('.rtf'), ('.skp'), ('.sldprt'), ('.sql'), ('.step'), ('.stl'), ('.stp'), ('.svg'), ('.tar'), ('.tgz'), ('.tif'), ('.tiff'), ('.ts'), ('.txt'), ('.wav'), ('.webm'), ('.webp'), ('.xls'), ('.xlsx'), ('.xml'), ('.xz'), ('.yaml'), ('.yml'), ('.zip'))
    SELECT count(*) AS bad_accept FROM platform_tools pt
    WHERE pt.accept_json IS NOT NULL AND (
      jsonb_typeof(pt.accept_json::jsonb) <> 'array'
      OR jsonb_array_length(pt.accept_json::jsonb) = 0
      OR EXISTS (SELECT 1 FROM jsonb_array_elements(pt.accept_json::jsonb) el
                 WHERE el #>> '{}' NOT IN (SELECT ext FROM allowed));
    -- expect bad_accept = 0
    SELECT code, badge_key, ingress_files_24h FROM plans ORDER BY sort_order;
    -- expect free/tier_free/100, plus/tier_plus/500, pro/tier_pro/2000
    SELECT to_regclass('user_plans');                 -- expect NULL
    SELECT count(*) FROM information_schema.columns
    WHERE table_name='packages' AND column_name='legacy_creem_product_id';
    -- expect 0
    SQL
    # I. remove dead env key (keep the .bak for rollback)
    sudo sed -i.bak-acct-unify '/^FLASHSHARE_DB=/d' /etc/flashshare.env
    # J. delete the dead application sqlite artifact (PostgreSQL is the only
    #    application DB; the file is not rollback evidence and is not archived)
    sudo rm -f /var/lib/flashshare/flashshare.sqlite
    # K. production checkout ff-only to the approved SHA
    cd /opt/flashshare
    git merge --ff-only "$TARGET_SHA"
    # L. start the new version
    sudo systemctl start flashshare-api

## 5. Post-cutover verification

    # M. health
    curl -fsS http://127.0.0.1:8000/healthz
    # N. logs — expect the startup sequence incl. "catalog rows validated"
    sudo journalctl -u flashshare-api -n 50 --no-pager
    # O. smoke
    #  - home page renders; tool grid loads
    #  - image tool executes a .png end-to-end
    #  - /api/v1/me (Bearer) returns access.membership, no plan key
    #  - /samelabs loads for a status=samelabs account
    # P. remove the temporary target worktree
    git worktree remove /tmp/flashshare-deploy-$TARGET_SHA

## 6. Rollback boundaries

### Migration transaction fails (rollback inside PostgreSQL)

The DB is unchanged — nothing to restore:

    sudo systemctl start flashshare-api   # original version keeps running

Investigate, fix the precondition drift, re-run from step 4-G.

### Failure AFTER migration COMMIT but BEFORE new code starts

The DB now has the unified schema; OLD code must NOT be started against it
(free row present, retired table gone). Choose one:

  a) Repair forward: finish steps 4-I..4-L with the target SHA.
  b) Full restore:
       sudo systemctl stop flashshare-api   # if partially started
       # restore DB from the step-2 backup (pg_restore -c the dump)
       sudo cp /etc/flashshare.env.bak-acct-unify /etc/flashshare.env
       cd /opt/flashshare && git checkout <original deployed SHA>
       sudo systemctl start flashshare-api

### Post-cutover instability

Service is on the new SHA; DB is unified. Roll back only by restoring DB
backup + original SHA (path b above). Do not restart old code on the
unified DB.
