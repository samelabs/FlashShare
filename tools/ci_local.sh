#!/usr/bin/env bash
# Local CI mirror — the single local test entry point.
#   tools/ci_local.sh [--browser] [suite ...]
# Starts an isolated stack (fresh DB, fake Gokapi :53898, app :8100), runs
# the given suites (default: every tests/ci + tests/canonical step of
# .github/workflows/ci.yml, in order), prints one exit-code table, stops.
# Full-suite verification is GitHub CI; locally run only affected suites.
# Env: PG_ADMIN_URL (default postgresql://127.0.0.1:5432/postgres),
#      PYTHON (default venv/bin/python), PLAYWRIGHT_CORE_PATH / CHROMIUM_PATH.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-venv/bin/python}
ADMIN=${PG_ADMIN_URL:-postgresql://127.0.0.1:5432/postgres}
BROWSER=0; [ "${1:-}" = --browser ] && { BROWSER=1; shift; }
DB=ci_local_$$; OUT=/tmp/ci-local-$$; mkdir -p "$OUT/log"
BASE=${ADMIN%/*}

export PYTHONDONTWRITEBYTECODE=1
export DATABASE_URL=${BASE/postgresql:/postgresql+psycopg:}/$DB TEST_DB_URL=$BASE/$DB
export FLASHSHARE_SESSION_SECRET=ci-local FLASHSHARE_GOKAPI_URL=http://127.0.0.1:53898
export FLASHSHARE_GOKAPI_KEY=ci-local-gk FLASHSHARE_BASE_URL=http://127.0.0.1:8100
export FLASHSHARE_TEST_PORT=8100 FLASHSHARE_LOG_DIR=$OUT/log GATE_ARTIFACTS=$OUT/gate
export TOOL_VAULT_KEY=bmHRR7eJv3wjTX590_wESE9SqO7DtyJTU2hObyZF7s0=  # test-only (same as ci.yml)
export FLASHSHARE_REGISTER_LIMIT_PER_HOUR=10000 FLASHSHARE_LOGIN_LIMIT_PER_15MIN=10000

PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
  psql "$ADMIN" -qc "DROP DATABASE IF EXISTS $DB" >/dev/null 2>&1
}
trap cleanup EXIT

for port in 8100 53898; do
  if lsof -tiTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then echo "port $port busy"; exit 2; fi
done
find app core tests tools -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
psql "$ADMIN" -qc "CREATE DATABASE $DB" >/dev/null || { echo "cannot create $DB"; exit 2; }

"$PY" tests/support/fake_gokapi.py --host 127.0.0.1 --port 53898 \
  --key "$FLASHSHARE_GOKAPI_KEY" >"$OUT/gokapi.log" 2>&1 & PIDS+=($!)
"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --workers 1 \
  >"$OUT/app.log" 2>&1 & PIDS+=($!)
for _ in $(seq 60); do
  curl -fsS http://127.0.0.1:8100/healthz >/dev/null 2>&1 && break
  kill -0 "${PIDS[1]}" 2>/dev/null || { tail -30 "$OUT/app.log"; exit 2; }
  sleep 1
done

if [ $# -eq 0 ]; then
  set -- $(grep -oE "run: (python|node) tests/(ci|canonical)/[a-z0-9_]+\.(py|js)" \
    .github/workflows/ci.yml | awk '{print $3}' | awk '!s[$0]++')
fi
[ $BROWSER -eq 1 ] && set -- "$@" tests/browser/test_runtime_gate.js tests/browser/test_s4_chromium.js

FAIL=0
printf '%-4s %s\n' EXIT SUITE
for s in "$@"; do
  case $s in *.js) cmd=(node "$s") ;; *) cmd=("$PY" "$s") ;; esac
  log="$OUT/$(basename "$s").log"
  "${cmd[@]}" >"$log" 2>&1; rc=$?
  printf '%-4s %s\n' "$rc" "$s"
  [ $rc -ne 0 ] && { FAIL=1; grep -m3 -E "✗|Error|FAIL" "$log" | sed 's/^/     /'; }
done
echo "logs: $OUT"
exit $FAIL
