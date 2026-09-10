#!/bin/bash
# Monthly isolated restore proof: restore the latest daily backup into a
# scratch database + scratch dir, verify row counts and checksums, drop.
set -uo pipefail

ENV_FILE="/etc/flashshare.env"
BACKUP_ROOT="/var/backups/flashshare"
DAILY_DIR="$BACKUP_ROOT/daily"
SCRATCH_DB="fs_restore_proof"
SCRATCH_DIR="$(mktemp -d)"

log() { echo "flashshare-restore-proof: $*"; }
fail() { log "FAILED: $*"; exit 1; }

DB_URL="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
# normalize the +psycopg variant back to plain postgresql://
if [[ "$DB_URL" == postgresql+psycopg://* ]]; then DB_URL="postgresql://${DB_URL#postgresql+psycopg://}"; fi
DB_NAME="${DB_URL##*/}"; DB_BASE="${DB_URL%/*}"   # scheme://user:pw@host:port

LATEST_PG="$(ls -1t "$DAILY_DIR"/pg-*.dump 2>/dev/null | head -1)"
LATEST_GK="$(ls -1t "$DAILY_DIR"/gokapi-*.tar.gz 2>/dev/null | head -1)"
[ -n "$LATEST_PG" ] || fail "no pg backup found"
STAMP="${LATEST_PG##*/pg-}"; STAMP="${STAMP%%.dump}"

# checksum verify
( cd "$DAILY_DIR" && sha256sum -c "SHA256SUMS-$STAMP" ) >/dev/null 2>&1 || fail "checksum mismatch"

# restore into scratch db
sudo -u postgres psql -qc "DROP DATABASE IF EXISTS $SCRATCH_DB" >/dev/null 2>&1
sudo -u postgres psql -qc "CREATE DATABASE $SCRATCH_DB OWNER flashshare" >/dev/null 2>&1 \
  || fail "scratch db create failed"
pg_restore --no-owner --no-privileges -d "$DB_BASE/$SCRATCH_DB" "$LATEST_PG" >/dev/null 2>&1 \
  || fail "pg_restore failed"

USERS=$(sudo -u postgres psql -d "$SCRATCH_DB" -qtAc "SELECT count(*) FROM users")
CALLS=$(sudo -u postgres psql -d "$SCRATCH_DB" -qtAc "SELECT count(*) FROM tool_calls")
LEDGER=$(sudo -u postgres psql -d "$SCRATCH_DB" -qtAc "SELECT count(*) FROM ledger")
[ "$USERS" -gt 0 ] 2>/dev/null || fail "restore proof: users table empty"

# gokapi archive extract + listing check
tar -xzf "$LATEST_GK" -C "$SCRATCH_DIR" || fail "gokapi archive extract failed"
[ -s "$SCRATCH_DIR/gokapi.sqlite" ] || fail "gokapi.sqlite missing in archive"
GK_FILES=$(tar -tzf "$LATEST_GK" | grep -vc '^gokapi.sqlite$')

sudo -u postgres psql -qc "DROP DATABASE $SCRATCH_DB" >/dev/null 2>&1
rm -rf "$SCRATCH_DIR"
log "OK users=$USERS tool_calls=$CALLS ledger=$LEDGER gokapi_objects=$GK_FILES"
