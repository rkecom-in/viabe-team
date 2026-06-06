#!/usr/bin/env bash
# VT-330 #6: prove gate-no-price-literals.sh FIRES on a synthetic plan-price literal (and
# passes on a clean tree) — regression-protects the gate's own regex against a future edit
# that silently breaks it. Run in CI right after the gate.
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
gate="$here/gate-no-price-literals.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# 1. A synthetic violation → the gate MUST fire (non-zero exit).
printf 'const foundingPaise = %d\n' 249900 > "$tmp/evil.ts"
if bash "$gate" "$tmp" >/dev/null 2>&1; then
  echo "FAIL: gate did NOT fire on a synthetic plan-price literal"
  exit 1
fi

# 2. A clean tree → the gate MUST pass (zero exit).
rm -f "$tmp/evil.ts"
printf 'const ok = 42\n' > "$tmp/clean.ts"
if ! bash "$gate" "$tmp" >/dev/null 2>&1; then
  echo "FAIL: gate fired on a clean tree (false positive)"
  exit 1
fi

echo "gate-no-price-literals self-test: PASS (fires on a literal, passes when clean)"
