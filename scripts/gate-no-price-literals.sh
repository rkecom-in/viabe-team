#!/usr/bin/env bash
# VT-331 / VT-330 Pillar 7 gate: the PLAN prices are NEVER hardcoded — they live in
# config/plans.yaml (the orchestrator resolves them authoritatively from there + env). Reject
# the specific plan paise literals in non-config TS/PY so a price can't drift into code where a
# bug could charge/subscribe at the wrong amount. (Bare `₹` is intentionally NOT matched — used
# legitimately for cost budgets, display, and rupee parsing.)
#
# Args: search dirs (default the two app trees). Exit 1 if a literal is found, else 0.
# Extracted from ci.yml (VT-330 #6) so the self-test exercises the REAL gate, no drift.
set -uo pipefail

dirs=("$@")
[ ${#dirs[@]} -eq 0 ] && dirs=(apps/team-web apps/team-orchestrator/src)

if grep -rnE '\b(249900|499900|1499900)\b' "${dirs[@]}" \
    --include="*.ts" --include="*.tsx" --include="*.py" \
    | grep -vE '/config/|\.test\.|/tests/|test_'; then
  echo "::error::hardcoded plan-price literal (Pillar 7) — use config/plans.yaml + env"
  exit 1
fi
echo "gate-no-price-literals: ok"
