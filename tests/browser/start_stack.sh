#!/usr/bin/env bash
# Browser runtime gate isolated stack (mirrors .github/workflows/ci.yml
# `canonical` job services): throwaway DB fs_browser_gate + fake Gokapi
# (53898) + real uvicorn app (8100). Local harness for the browser gate.
set -uo pipefail
cd /home/ubuntu/prod-closure

export FLASHSHARE_SESSION_SECRET=ci-session-secret-not-production
export DATABASE_URL="postgresql+psycopg://fs_test:fs-test-pw@127.0.0.1:5432/fs_browser_gate"
export TEST_DB_URL="postgresql://fs_test:fs-test-pw@127.0.0.1:5432/fs_browser_gate"
export FLASHSHARE_TEST_PORT=8100
export FLASHSHARE_GOKAPI_URL="http://127.0.0.1:53898"
export FLASHSHARE_GOKAPI_KEY=ci-gokapi-key
export FLASHSHARE_BASE_URL="http://127.0.0.1:8100"
export FLASHSHARE_LOG_DIR=/tmp/fs-browser-gate-log

PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
  for cand in /opt/flashshare/venv/bin/python .venv/bin/python; do
    [ -x "$cand" ] && PY="$cand" && break
  done
fi
PY="${PY:-python3}"
mkdir -p "$FLASHSHARE_LOG_DIR"

# fake gokapi
$PY tests/support/fake_gokapi.py --host 127.0.0.1 --port 53898 --key "$FLASHSHARE_GOKAPI_KEY" \
  >/tmp/fs-browser-gate-gokapi.log 2>&1 &
echo $! >/tmp/fs-browser-gate-gokapi.pid

# real app
$PY -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --workers 1 \
  >/tmp/fs-browser-gate-app.log 2>&1 &
echo $! >/tmp/fs-browser-gate-app.pid

for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8100/healthz >/dev/null 2>&1 && { echo "app ready"; exit 0; }
  if ! kill -0 "$(cat /tmp/fs-browser-gate-app.pid)" 2>/dev/null; then
    cat /tmp/fs-browser-gate-app.log; exit 1
  fi
  sleep 1
done
cat /tmp/fs-browser-gate-app.log
exit 1
