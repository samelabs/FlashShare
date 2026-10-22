#!/usr/bin/env bash
# The single test runner — local and the canonical-integration CI job.
#   tools/ci_local.sh [--browser] [suite ...]
# Proves every canonical suite fails closed (exit 2 without an isolated
# port / TEST_DB_URL), starts an isolated stack (fresh DB, fake Gokapi
# :53898, app :8100), runs the given suites (default: every tests/ci and
# tests/canonical suite), prints one exit-code table, stops.
# Env: PG_ADMIN_URL (default postgresql://127.0.0.1:5432/postgres),
#      PYTHON (default venv/bin/python), PLAYWRIGHT_CORE_PATH / CHROMIUM_PATH,
#      APP_PORT (default 8100) / GOKAPI_PORT (default 53898) — pick free
#      isolated ports whenever a default is already taken.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-venv/bin/python}
# Browser suites spawn python3 subprocesses (PWA offline probe) — pin
# them to the SAME interpreter instead of whatever `python3` is on PATH.
export PYTHON="$PY"
ADMIN=${PG_ADMIN_URL:-postgresql://127.0.0.1:5432/postgres}
APP_PORT=${APP_PORT:-8100}
GOKAPI_PORT=${GOKAPI_PORT:-53898}
BROWSER=0; [ "${1:-}" = --browser ] && { BROWSER=1; shift; }
DB=ci_local_$$; OUT=/tmp/ci-local-$$; mkdir -p "$OUT/log"
BASE=${ADMIN%/*}

export PYTHONDONTWRITEBYTECODE=1
export DATABASE_URL=${BASE/postgresql:/postgresql+psycopg:}/$DB TEST_DB_URL=$BASE/$DB
export FLASHSHARE_SESSION_SECRET=ci-local FLASHSHARE_GOKAPI_URL=http://127.0.0.1:$GOKAPI_PORT
export FLASHSHARE_GOKAPI_KEY=ci-local-gk FLASHSHARE_BASE_URL=http://127.0.0.1:$APP_PORT
export FLASHSHARE_TEST_PORT=$APP_PORT FLASHSHARE_LOG_DIR=$OUT/log GATE_ARTIFACTS=$OUT/gate
export TOOL_VAULT_KEY=bmHRR7eJv3wjTX590_wESE9SqO7DtyJTU2hObyZF7s0=  # test-only (same as ci.yml)
export FLASHSHARE_REGISTER_LIMIT_PER_HOUR=10000 FLASHSHARE_LOGIN_LIMIT_PER_15MIN=10000
export FLASHSHARE_MCP_REGISTER_LIMIT_PER_HOUR=10000

if [ $# -eq 0 ]; then
  set -- tests/ci/test_*.py tests/ci/test_*.js tests/canonical/test_*.py
fi
[ $BROWSER -eq 1 ] && set -- "$@" tests/browser/test_runtime_gate.js tests/browser/test_s4_chromium.js

# fail-closed gate: no isolated port / production port / non-integer port /
# no TEST_DB_URL → exit 2 before any request or DB write
GUARD=0
for s in "$@"; do
  case $s in tests/canonical/*.py) ;; *) continue ;; esac
  for bad in "-u FLASHSHARE_TEST_PORT" "FLASHSHARE_TEST_PORT=8000" \
             "FLASHSHARE_TEST_PORT=8x" "-u TEST_DB_URL"; do
    env $bad "$PY" "$s" >/dev/null 2>&1; rc=$?
    [ $rc -ne 2 ] && { echo "fail-closed: $s with $bad exited $rc (want 2)"; GUARD=1; }
  done
done
[ $GUARD -eq 0 ] || exit 1

PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
  psql "$ADMIN" -qc "DROP DATABASE IF EXISTS $DB" >/dev/null 2>&1
}
trap cleanup EXIT
trap 'exit 2' INT TERM

for port in "$APP_PORT" "$GOKAPI_PORT"; do
  if lsof -tiTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then echo "port $port busy"; exit 2; fi
done
find app core tests tools -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
psql "$ADMIN" -qc "CREATE DATABASE $DB" >/dev/null || { echo "cannot create $DB"; exit 2; }

"$PY" tests/support/fake_gokapi.py --host 127.0.0.1 --port "$GOKAPI_PORT" \
  --key "$FLASHSHARE_GOKAPI_KEY" >"$OUT/gokapi.log" 2>&1 & PIDS+=($!)
"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$APP_PORT" --workers 1 \
  >"$OUT/app.log" 2>&1 & PIDS+=($!)
# startup budget is wall-clock (60s); each probe carries its own connect and
# total timeouts, so a port that accepts connections but never answers — or
# never appears — cannot stretch the budget past one probe + one sleep
T0=$SECONDS; HEALTHY=0
while [ $((SECONDS - T0)) -lt 60 ]; do
  if curl -fsS --connect-timeout 2 --max-time 3 \
      "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then HEALTHY=1; break; fi
  kill -0 "${PIDS[1]}" 2>/dev/null || { tail -30 "$OUT/app.log"; exit 2; }
  sleep 1
done
# a live app that never answers healthz within the budget is a startup
# failure, not a stack to silently run the suites against
if [ "$HEALTHY" -ne 1 ]; then
  echo "app not healthy on :$APP_PORT after $((SECONDS - T0))s"
  tail -30 "$OUT/app.log"; exit 2
fi
STARTUP=$((SECONDS - T0))

FAIL=0
printf '%-4s %-6s %s\n' EXIT TIME SUITE
for s in "$@"; do
  case $s in *.js) cmd=(node "$s") ;; *) cmd=("$PY" "$s") ;; esac
  log="$OUT/$(basename "$s").log"
  t0=$SECONDS
  "${cmd[@]}" >"$log" 2>&1; rc=$?
  printf '%-4s %-6s %s\n' "$rc" "$((SECONDS - t0))s" "$s"
  grep -o '[0-9]*/[0-9]* checks passed' "$log" | tail -1 | sed 's/^/     /'
  [ $rc -ne 0 ] && { FAIL=1; grep -m3 -E "✗|Error|FAIL" "$log" | sed 's/^/     /'; }
done
echo "startup ${STARTUP}s + suites $((SECONDS - T0 - STARTUP))s · logs: $OUT"
exit $FAIL
