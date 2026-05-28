#!/usr/bin/env bash
# VT-218 dev-env smoke test.
#
# Runs against deployed Railway + Vercel dev URLs. Fail-not-skip on
# any HTTP non-2xx; exit non-zero so CI flags the deploy red.
#
# Required env:
#   RAILWAY_DEV_URL = https://<railway-domain>
#   VERCEL_DEV_URL  = https://<vercel-domain>
#
# Optional env (for the full auth-gate test):
#   OPERATOR_JWT_SECRET = same as deployed; used to mint a test cookie
#   FAZAL_OWNER_UUID    = same as deployed; used as JWT sub
#
# Per Rule #15: this script IS the canary for VT-218.

set -euo pipefail

: "${RAILWAY_DEV_URL:?RAILWAY_DEV_URL env required}"
: "${VERCEL_DEV_URL:?VERCEL_DEV_URL env required}"

echo "[VT-218 smoke] RAILWAY_DEV_URL=$RAILWAY_DEV_URL"
echo "[VT-218 smoke] VERCEL_DEV_URL=$VERCEL_DEV_URL"

fail=0

# --- A1: orchestrator health -------------------------------------------------
echo "[A1] GET $RAILWAY_DEV_URL/health"
code=$(curl -s -o /tmp/vt218-health.body -w '%{http_code}' --max-time 15 "$RAILWAY_DEV_URL/health" || echo "000")
if [ "$code" = "200" ]; then
    echo "[A1] PASS — orchestrator /health 200"
else
    echo "[A1] FAIL — got $code; body:"
    cat /tmp/vt218-health.body || true
    fail=1
fi

# --- A2: team-web onboard redirects unauth → /login --------------------------
echo "[A2] GET $VERCEL_DEV_URL/team/onboard (no cookie)"
status_line=$(curl -s -o /tmp/vt218-onboard.body -w '%{http_code} %{redirect_url}' --max-time 15 "$VERCEL_DEV_URL/team/onboard" || echo "000 ")
code=$(echo "$status_line" | awk '{print $1}')
redirect=$(echo "$status_line" | awk '{print $2}')
if [ "$code" = "302" ] || [ "$code" = "307" ] || [ "$code" = "303" ]; then
    case "$redirect" in
        *"/login"*) echo "[A2] PASS — /team/onboard $code → $redirect" ;;
        *)
            echo "[A2] FAIL — got $code but redirect=$redirect (not /login)"
            fail=1
            ;;
    esac
else
    echo "[A2] FAIL — expected 3xx redirect, got $code"
    fail=1
fi

# --- A3: team-web ops/stream redirects unauth → /login -----------------------
echo "[A3] GET $VERCEL_DEV_URL/team/ops/stream (no cookie)"
status_line=$(curl -s -o /tmp/vt218-ops.body -w '%{http_code} %{redirect_url}' --max-time 15 "$VERCEL_DEV_URL/team/ops/stream" || echo "000 ")
code=$(echo "$status_line" | awk '{print $1}')
redirect=$(echo "$status_line" | awk '{print $2}')
if [ "$code" = "302" ] || [ "$code" = "307" ] || [ "$code" = "303" ]; then
    case "$redirect" in
        *"/login"*) echo "[A3] PASS — /team/ops/stream $code → $redirect" ;;
        *)
            echo "[A3] FAIL — got $code but redirect=$redirect"
            fail=1
            ;;
    esac
else
    echo "[A3] FAIL — expected 3xx, got $code"
    fail=1
fi

# --- A4: internal orchestrator endpoint rejects missing secret ---------------
echo "[A4] POST $RAILWAY_DEV_URL/api/orchestrator/integrations/onboard-step (no secret)"
code=$(curl -s -o /tmp/vt218-internal.body -w '%{http_code}' --max-time 15 \
    -X POST \
    -H "Content-Type: application/json" \
    -d '{"tenant_id":"00000000-0000-4000-8000-000000aaaaaa","answer":"smoke"}' \
    "$RAILWAY_DEV_URL/api/orchestrator/integrations/onboard-step" || echo "000")
if [ "$code" = "401" ]; then
    echo "[A4] PASS — internal endpoint 401 without secret"
else
    echo "[A4] FAIL — expected 401, got $code"
    fail=1
fi

# --- Summary -----------------------------------------------------------------
if [ "$fail" -eq 0 ]; then
    echo "[VT-218 smoke] ALL PASS"
    exit 0
fi
echo "[VT-218 smoke] FAILED — $fail assertion(s) red"
exit 1
