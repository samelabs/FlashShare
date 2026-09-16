#!/bin/bash
# FlashShare production backup: PostgreSQL dump + Gokapi data/config archive.
# Rotation: keep 7 daily + 4 weekly; checksums; optional off-host upload via
# rclone when FLASHSHARE_BACKUP_REMOTE is configured; failures log to journal.
set -euo pipefail

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DOW="$(date -u +%u)"   # 1=Mon..7=Sun
BACKUP_ROOT="/var/backups/flashshare"
DAILY_DIR="$BACKUP_ROOT/daily"
WEEKLY_DIR="$BACKUP_ROOT/weekly"
KEEP_DAILY=7
KEEP_WEEKLY=4
ENV_FILE="/etc/flashshare.env"
GOKAPI_DATA="/var/lib/gokapi/data"
GOKAPI_CONFIG="/opt/gokapi/data"
REMOTE="${FLASHSHARE_BACKUP_REMOTE:-}"   # rclone remote:path; empty = off-host disabled

log() { echo "flashshare-backup: $*" ; }
fail() { log "FAILED: $*"; exit 1; }

# ── env (root-readable) ──
[ -r "$ENV_FILE" ] || fail "env file not readable"
DB_URL="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
[ -n "$DB_URL" ] || fail "DATABASE_URL missing"
# normalize the +psycopg variant back to plain postgresql://
if [[ "$DB_URL" == postgresql+psycopg://* ]]; then DB_URL="postgresql://${DB_URL#postgresql+psycopg://}"; fi

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"

# ── PostgreSQL dump ──
PG_FILE="$DAILY_DIR/pg-$STAMP.dump"
pg_dump --format=custom --no-owner --no-privileges "$DB_URL" > "$PG_FILE" 2>/tmp/fsb-pg.err \
  || fail "pg_dump failed: $(head -c 200 /tmp/fsb-pg.err)"

# ── Gokapi data + config archive (stop-free: sqlite via sqlite3 .backup) ──
GK_FILE="$DAILY_DIR/gokapi-$STAMP.tar.gz"
TMP_GK="$(mktemp -d)"
python3 - "$GOKAPI_CONFIG/gokapi.sqlite" "$TMP_GK/gokapi.sqlite" <<'PYB' 2>/tmp/fsb-gk.err \
  || fail "gokapi sqlite backup failed: $(head -c 200 /tmp/fsb-gk.err)"
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
con.backup(sqlite3.connect(dst))
con.close()
PYB
tar -czf "$GK_FILE" -C "$TMP_GK" gokapi.sqlite -C "$GOKAPI_DATA" . 2>>/tmp/fsb-gk.err \
  || fail "gokapi tar failed: $(head -c 200 /tmp/fsb-gk.err)"
rm -rf "$TMP_GK"

# ── Checksums ──
( cd "$DAILY_DIR" && sha256sum "pg-$STAMP.dump" "gokapi-$STAMP.tar.gz" > "SHA256SUMS-$STAMP" )

# ── Weekly copy (Sunday) ──
if [ "$DOW" = "7" ]; then
  cp "$PG_FILE" "$WEEKLY_DIR/pg-$STAMP.dump"
  cp "$GK_FILE" "$WEEKLY_DIR/gokapi-$STAMP.tar.gz"
  ( cd "$WEEKLY_DIR" && sha256sum "pg-$STAMP.dump" "gokapi-$STAMP.tar.gz" > "SHA256SUMS-$STAMP" )
fi

# ── Rotation ──
ls -1t "$DAILY_DIR"/pg-*.dump 2>/dev/null | tail -n +$((KEEP_DAILY + 1)) | while read -r f; do
  s="${f##*/pg-}"; s="${s%%.dump}"
  rm -f "$DAILY_DIR/pg-$s.dump" "$DAILY_DIR/gokapi-$s.tar.gz" "$DAILY_DIR/SHA256SUMS-$s"
done
ls -1t "$WEEKLY_DIR"/pg-*.dump 2>/dev/null | tail -n +$((KEEP_WEEKLY + 1)) | while read -r f; do
  s="${f##*/pg-}"; s="${s%%.dump}"
  rm -f "$WEEKLY_DIR/pg-$s.dump" "$WEEKLY_DIR/gokapi-$s.tar.gz" "$WEEKLY_DIR/SHA256SUMS-$s"
done

# ── Off-host upload ──
if [ -n "$REMOTE" ]; then
  if command -v rclone >/dev/null 2>&1; then
    rclone copy "$PG_FILE" "$REMOTE/daily/" --log-file=/tmp/fsb-rclone.log 2>>/tmp/fsb-rclone.log \
      || fail "rclone pg upload failed"
    rclone copy "$GK_FILE" "$REMOTE/daily/" --log-file=/tmp/fsb-rclone.log 2>>/tmp/fsb-rclone.log \
      || fail "rclone gokapi upload failed"
    [ "$DOW" = "7" ] && rclone copy "$WEEKLY_DIR/pg-$STAMP.dump" "$REMOTE/weekly/" 2>>/tmp/fsb-rclone.log || true
    [ "$DOW" = "7" ] && rclone copy "$WEEKLY_DIR/gokapi-$STAMP.tar.gz" "$REMOTE/weekly/" 2>>/tmp/fsb-rclone.log || true
    log "off-host upload OK → $REMOTE"
  else
    log "FAILED: FLASHSHARE_BACKUP_REMOTE set but rclone not installed — off-host copy skipped"
    exit 3
  fi
else
  log "WARNING: FLASHSHARE_BACKUP_REMOTE not set — backups are on-host only"
fi

log "OK pg=$(du -h "$PG_FILE" | cut -f1) gokapi=$(du -h "$GK_FILE" | cut -f1)"
