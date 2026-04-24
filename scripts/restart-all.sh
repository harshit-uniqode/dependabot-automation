#!/usr/bin/env bash
# Restart everything from a clean slate:
#   1. Stop wizard server (port 8787)
#   2. Stop Floci / LocalStack emulator
#   3. Start Floci emulator + wait for health
#   4. Regenerate Lambda + Angular dashboards
#   5. Start wizard server in the background
#   6. Print dashboard URLs
#
# Usage:
#   ./scripts/restart-all.sh             — full restart (default)
#   ./scripts/restart-all.sh --no-regen  — skip dashboard regen (faster)
#   ./scripts/restart-all.sh --no-floci  — skip emulator (wizard only)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

WIZARD_PORT=8787
FLOCI_URL="http://localhost:4566"
FLOCI_COMPOSE="docker/floci-emulator.compose.yml"
LS_COMPOSE="docker/localstack-emulator.compose.yml"
LOG_DIR="$REPO_ROOT/.uniqode"
mkdir -p "$LOG_DIR"
WIZARD_LOG="$LOG_DIR/wizard.log"

REGEN=1
START_FLOCI=1
for arg in "$@"; do
  case "$arg" in
    --no-regen) REGEN=0 ;;
    --no-floci) START_FLOCI=0 ;;
    -h|--help)
      sed -n '2,13p' "$0"; exit 0 ;;
  esac
done

say()  { printf '\033[1;34m[restart]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m    %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m      %s\n' "$*"; }

# Cross-platform timeout wrapper (macOS has no `timeout` by default).
# Usage: with_timeout <seconds> <cmd...>
with_timeout() {
  local secs=$1; shift
  ( "$@" ) & local pid=$!
  ( sleep "$secs" && kill -9 "$pid" 2>/dev/null ) & local watchdog=$!
  wait "$pid" 2>/dev/null; local rc=$?
  kill "$watchdog" 2>/dev/null
  return $rc
}

# ── 0. Check Docker daemon is responsive ───────────────────────────
say "Checking Docker daemon…"
if ! with_timeout 10 docker info >/dev/null 2>&1; then
  warn "Docker daemon is not responsive."
  warn "Open Docker Desktop (or run: open -a Docker), wait for the whale icon to settle, then re-run: make restart"
  exit 1
fi
ok "Docker daemon responsive."

# ── 1. Kill wizard ──────────────────────────────────────────────────
say "Stopping wizard on :$WIZARD_PORT…"
PIDS="$(lsof -ti tcp:$WIZARD_PORT 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  echo "$PIDS" | xargs kill -TERM 2>/dev/null || true
  sleep 1
  echo "$PIDS" | xargs kill -KILL 2>/dev/null || true
  ok "Wizard killed (pids: $PIDS)"
else
  ok "No wizard running."
fi

pkill -f "python3 -m wizard_server" 2>/dev/null || true

# ── 2. Stop emulators ───────────────────────────────────────────────
say "Stopping emulators (Floci + LocalStack)…"
with_timeout 45 docker compose -f "$FLOCI_COMPOSE" down --remove-orphans >/dev/null 2>&1 \
  || warn "Floci compose-down timed out/failed — continuing."
with_timeout 45 docker compose -f "$LS_COMPOSE" down --remove-orphans >/dev/null 2>&1 \
  || warn "LocalStack compose-down timed out/failed — continuing."

# Kill any leftover Lambda sub-containers
LAMBDA_CIDS="$(with_timeout 10 docker ps -q --filter 'name=localstack-lambda-' 2>/dev/null || true)"
if [ -n "$LAMBDA_CIDS" ]; then
  say "Removing leftover Lambda sub-containers…"
  echo "$LAMBDA_CIDS" | xargs -r docker rm -f 2>/dev/null || true
fi
ok "Emulators stopped."

# ── 3. Start Floci ──────────────────────────────────────────────────
if [ "$START_FLOCI" -eq 1 ]; then
  say "Starting Floci emulator…"
  docker compose -f "$FLOCI_COMPOSE" up -d

  say "Waiting for Floci health ($FLOCI_URL)…"
  for i in $(seq 1 60); do
    if curl -sf "$FLOCI_URL/_floci/health"     >/dev/null 2>&1 \
    || curl -sf "$FLOCI_URL/_localstack/health" >/dev/null 2>&1; then
      ok "Floci healthy."
      break
    fi
    printf '.'
    sleep 1
    if [ "$i" -eq 60 ]; then
      warn "Floci did not become healthy in 60s. Check: docker compose -f $FLOCI_COMPOSE logs"
    fi
  done
  echo
else
  warn "Skipping Floci (--no-floci)."
fi

# ── 4. Regenerate dashboards ────────────────────────────────────────
if [ "$REGEN" -eq 1 ]; then
  say "Regenerating Lambda dashboard…"
  ./scripts/refresh-lambda-dashboard.sh >/dev/null 2>&1 \
    || warn "Lambda dashboard regen failed (non-fatal)."
  say "Regenerating Angular dashboard…"
  ./scripts/refresh-angular-dashboard.sh >/dev/null 2>&1 \
    || warn "Angular dashboard regen failed (non-fatal)."
  ok "Dashboards regenerated."
else
  warn "Skipping dashboard regen (--no-regen)."
fi

# ── 5. Start wizard ────────────────────────────────────────────────
say "Starting wizard server…"
nohup python3 -m wizard_server >"$WIZARD_LOG" 2>&1 &
WIZARD_PID=$!
sleep 2

if ! kill -0 "$WIZARD_PID" 2>/dev/null; then
  warn "Wizard failed to start. Last log lines:"
  tail -20 "$WIZARD_LOG"
  exit 1
fi
ok "Wizard running (pid: $WIZARD_PID, log: $WIZARD_LOG)"

# ── 6. URLs ────────────────────────────────────────────────────────
cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 All services ready
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Dashboards:
    Lambda   http://127.0.0.1:$WIZARD_PORT/vulnerability-dashboards/lambda-dashboard.html
    Angular  http://127.0.0.1:$WIZARD_PORT/vulnerability-dashboards/angular-dashboard.html

  API:       http://127.0.0.1:$WIZARD_PORT/api/
  Floci:     $FLOCI_URL
  Log:       tail -f $WIZARD_LOG

  Stop everything:  make stop-all

EOF
