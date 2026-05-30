#!/usr/bin/env bash
# run-e2e-smoke.sh — single-command Sprint 1 E2E smoke test runner.
#
# What it does:
#   1. Sources the dev secrets (.viabe/secrets/supabase-dev.env + anthropic.env)
#   2. Checks if orchestrator is reachable on :8001
#   3. If NOT reachable, boots it in the background (logs → /tmp/orchestrator.log)
#   4. Waits for orchestrator to become healthy
#   5. Runs the canary
#   6. Tees output to /tmp/sprint1-e2e-evidence.log
#
# Usage:
#   bash scripts/run-e2e-smoke.sh
#
# Flags:
#   --reuse-orchestrator   Skip boot; expect orchestrator already running on :8001
#   --keep-running         Do NOT shut down orchestrator after canary completes
#
# Cost: one real Anthropic call ≈ ₹1.30-2.00 per run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── flags ──
REUSE_ORCHESTRATOR=0
KEEP_RUNNING=0
for arg in "$@"; do
  case "$arg" in
    --reuse-orchestrator) REUSE_ORCHESTRATOR=1 ;;
    --keep-running)       KEEP_RUNNING=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# ── colors ──
if [[ -t 1 ]]; then
  C_BOLD=$'\e[1m'; C_GREEN=$'\e[32m'; C_YELLOW=$'\e[33m'; C_RED=$'\e[31m'; C_RESET=$'\e[0m'
else
  C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

log() { echo "${C_BOLD}[e2e-smoke]${C_RESET} $*"; }
ok()  { echo "${C_GREEN}✓${C_RESET} $*"; }
warn(){ echo "${C_YELLOW}⚠${C_RESET} $*"; }
err() { echo "${C_RED}✗${C_RESET} $*" >&2; }

# ── 1. source env ──
log "loading env files…"
[[ -f .viabe/secrets/supabase-dev.env ]] || { err ".viabe/secrets/supabase-dev.env missing"; exit 1; }
[[ -f .viabe/secrets/anthropic.env ]]    || { err ".viabe/secrets/anthropic.env missing";    exit 1; }
set -a
# shellcheck disable=SC1091
source .viabe/secrets/supabase-dev.env
# shellcheck disable=SC1091
source .viabe/secrets/anthropic.env
set +a
ok "env loaded (supabase + anthropic)"

# ── 2. check orchestrator ──
ORCH_PID=""
ORCH_LOG="/tmp/orchestrator.log"
ORCH_RUNNING=0

if curl -sf -o /dev/null --max-time 2 http://localhost:8001/ 2>/dev/null \
   || curl -sf -o /dev/null --max-time 2 http://localhost:8001/docs 2>/dev/null; then
  ok "orchestrator already running on :8001"
  ORCH_RUNNING=1
elif [[ $REUSE_ORCHESTRATOR -eq 1 ]]; then
  err "--reuse-orchestrator set but orchestrator not reachable on :8001"
  exit 2
fi

# ── 3. boot if needed ──
if [[ $ORCH_RUNNING -eq 0 ]]; then
  log "booting orchestrator (logs → $ORCH_LOG)…"
  (
    cd apps/team-orchestrator
    nohup ./.venv/bin/python -m uvicorn main:app --app-dir src --port 8001 \
      > "$ORCH_LOG" 2>&1 &
    echo $! > /tmp/orchestrator.pid
  )
  ORCH_PID="$(cat /tmp/orchestrator.pid)"
  log "orchestrator pid=$ORCH_PID"

  # ── 4. wait for healthy ──
  log "waiting for orchestrator boot…"
  WAITED=0
  MAX_WAIT=30
  while [[ $WAITED -lt $MAX_WAIT ]]; do
    if curl -sf -o /dev/null --max-time 2 http://localhost:8001/ 2>/dev/null \
       || curl -sf -o /dev/null --max-time 2 http://localhost:8001/docs 2>/dev/null; then
      ok "orchestrator healthy (after ${WAITED}s)"
      break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
    if (( WAITED % 5 == 0 )); then
      log "still waiting… (${WAITED}s / ${MAX_WAIT}s)"
    fi
  done
  if [[ $WAITED -ge $MAX_WAIT ]]; then
    err "orchestrator failed to boot within ${MAX_WAIT}s — check $ORCH_LOG"
    [[ -n "$ORCH_PID" ]] && kill "$ORCH_PID" 2>/dev/null || true
    exit 3
  fi
fi

# ── 5. run canary ──
log "running canary (≈30s; one real Anthropic call ≈ ₹1-2)…"
echo "─── canary output ──────────────────────────────────────────"
set +e
(
  cd apps/team-orchestrator
  time ./.venv/bin/python canaries/sprint1_e2e_smoke.py 2>&1 \
    | tee /tmp/sprint1-e2e-evidence.log
)
RESULT=$?
set -e
echo "────────────────────────────────────────────────────────────"

# ── 6. shutdown if we booted it ──
if [[ $ORCH_RUNNING -eq 0 && $KEEP_RUNNING -eq 0 && -n "$ORCH_PID" ]]; then
  log "shutting down orchestrator pid=$ORCH_PID…"
  kill "$ORCH_PID" 2>/dev/null || true
  sleep 1
  ok "orchestrator stopped"
elif [[ $KEEP_RUNNING -eq 1 ]]; then
  warn "orchestrator left running (pid=$ORCH_PID). To stop: kill $ORCH_PID"
fi

# ── 7. summary ──
echo ""
if [[ $RESULT -eq 0 ]]; then
  ok "CANARY PASSED (full output: /tmp/sprint1-e2e-evidence.log)"
elif [[ $RESULT -eq 1 ]]; then
  warn "CANARY had FAILed assertions (expected — review output above)"
  echo "    Common known fails: A8 budget mismatch (canary budget pre-dates VT-194 caching)"
  echo "    Full output: /tmp/sprint1-e2e-evidence.log"
else
  err "CANARY exit code $RESULT — check $ORCH_LOG + /tmp/sprint1-e2e-evidence.log"
fi
exit $RESULT
