#!/usr/bin/env bash
# VT-720/725/O11 overnight chain (Fazal 2026-08-04: "let the pack run overnight").
#
# Three stages need the ONE deployed dev and can only run one after another. Chained here so they
# don't spread across two of Fazal's days:
#   1. the pack (already running when this starts — this waits for it)
#   2. the sealed no-O8 baseline bundle   [GATED]
#   3. the VT-725 flip/narrowing canary
#
# THE GATE (Clau, and it is the judgment call, not a formality): chain into stage 2 ONLY if the
# pack produced no NEW failure class. A TIMEOUT is not a new class — those are the known slow tail.
# Any FAIL-type block, or any CONTAMINATED block, stops the chain: generating the baseline costs
# real LLM calls across 12 cases, and a baseline measured on a build we have just flagged is a
# number we would have to throw away. Losing the window beats banking an unusable measurement.
#
# NO PUSHES anywhere in here — a docs push is a deploy and would contaminate the running stage.
set -uo pipefail

# The two dev tenants the flip test compares. Both real, both with an L1 business_profile, neither
# a live-number tenant (dev_send_guard is on and this canary sends nothing regardless).
TENANT_A=222fbb78-8f82-4b7d-80db-7bbe92e6d3e5
TENANT_B=42a47a73-f04b-448e-8fea-da28d061d086

REPO=/Users/fazalkhan/development/viabe-team
APP="$REPO/apps/team-orchestrator"
REPORTS="$APP/canaries/reports"
OUT="$REPORTS/overnight_chain.log"
PACK_LOG="$REPORTS/vt720_fullpack_x3.log"
PACK_JSON="$REPORTS/vt720_fullpack_x3.json"
BASELINE_OUT=/Users/fazalkhan/development/vt-dataset-out/no-o8-baseline.json
INGRESS=https://vt-orchestrator-service-development.up.railway.app

log() { echo "[$(date -u '+%H:%M:%SZ')] $*" | tee -a "$OUT"; }

cd "$REPO" || exit 1
: > "$OUT"
log "chain start"

# --- stage 1: wait for the pack -------------------------------------------------------------
while pgrep -f "run_critical_x3" > /dev/null; do sleep 60; done
log "stage 1: pack process has exited"

# Progress is counted from DISTINCT scenario names, never entries/3 — the append-only bundle trap
# (CL-2026-08-04-report-bundles-are-append-only) is what turned 44 scenarios into a reported 62.
DISTINCT=$(python3 -c "
import json
try:
    d = json.load(open('$PACK_JSON'))
    print(len({r['scenario'] for r in d}))
except Exception:
    print(0)
")
# `grep -c` PRINTS "0" and EXITS 1 when there are no matches, so the previous `|| echo 0` appended a
# SECOND "0" and the variable held the two-line value "0\n0". Clau caught it: the gate stopped for
# the right reason via $FAILS, but the contamination arm was comparing a non-scalar and would not
# have fired reliably on its own. A gate that cannot be trusted to stop is worse than no gate,
# because we sleep through it. `|| true` keeps grep's own "0" and discards only the exit status.
count_in_log() { grep -c "$1" "$PACK_LOG" 2>/dev/null | head -1 || true; }
FAILS=$(count_in_log 'PASS/XFAIL (FAIL)')
TIMEOUTS=$(count_in_log 'PASS/XFAIL (TIMEOUT)')
CONTAM=$(count_in_log 'CONTAMINATED')
COMPLETED=$(count_in_log '=== summary:')
FAILS=${FAILS:-0}; TIMEOUTS=${TIMEOUTS:-0}; CONTAM=${CONTAM:-0}; COMPLETED=${COMPLETED:-0}
log "stage 1 verdict: distinct_scenarios=$DISTINCT fails=$FAILS timeouts=$TIMEOUTS contaminated=$CONTAM ran_to_completion=$COMPLETED"

if [ "$COMPLETED" -eq 0 ]; then
  log "STOP: the pack did not reach its summary — it died rather than finished. Not chaining."
  log "      resume with: run_critical_x3.py --resume --json-report $PACK_JSON"
  exit 2
fi
if [ "$FAILS" -ne 0 ] || [ "$CONTAM" -ne 0 ]; then
  log "STOP: a NEW failure class appeared (fails=$FAILS contaminated=$CONTAM). Not generating the"
  log "      baseline — a number measured on a flagged build is one we would have to discard."
  exit 3
fi
log "gate PASS: only TIMEOUTs ($TIMEOUTS) — the known slow tail, not a new class. Chaining."

# --- stage 2: sealed no-O8 baseline bundle ---------------------------------------------------
mkdir -p "$(dirname "$BASELINE_OUT")"
log "stage 2: generating the sealed baseline bundle (knowledge_mode=off)"
set -a; . "$REPO/.viabe/secrets/anthropic.env" > /dev/null 2>&1; set +a
(cd "$APP" && uv run --no-project python canaries/o11_response_bundle.py \
  --dataset-dir /Users/fazalkhan/development/vt-dataset \
  --split sealed --knowledge-mode off --run-label no-o8-baseline \
  --output "$BASELINE_OUT") >> "$OUT" 2>&1
STAGE2=$?
log "stage 2 exit=$STAGE2"

# --- stage 3: VT-725 flip + narrowing canary -------------------------------------------------
log "stage 3: VT-725 flip/narrowing canary"
set -a; . "$REPO/.viabe/secrets/supabase-dev.env" > /dev/null 2>&1; . "$REPO/.viabe/secrets/voyage.env" > /dev/null 2>&1; set +a
(cd "$APP" && TEAM_KNOWLEDGE_SERVING=shadow uv run --no-project python \
  canaries/vt725_flip_and_narrowing.py --expected-env dev \
  --tenant-a "$TENANT_A" --tenant-b "$TENANT_B") >> "$OUT" 2>&1
STAGE3=$?
log "stage 3 exit=$STAGE3"

log "chain complete: stage2=$STAGE2 stage3=$STAGE3"
