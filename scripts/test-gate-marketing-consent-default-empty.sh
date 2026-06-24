#!/usr/bin/env bash
# VT-396 step-3: prove gate-marketing-consent-default-empty.sh FIRES on a synthetic non-empty
# MARKETING_CONSENT_VERSIONS default (and passes on the clean, empty-default forms) —
# regression-protects the gate's own regex against a future edit that silently neuters it.
# Run in CI right after the gate.
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
gate="$here/gate-marketing-consent-default-empty.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# 1. Synthetic violations → the gate MUST fire (non-zero exit) for EACH non-empty-default form.
for evil in \
  'MARKETING_CONSENT_VERSIONS: frozenset[str] = frozenset({"dev-test-v0"})' \
  'MARKETING_CONSENT_VERSIONS = frozenset(["v1"])' \
  'MARKETING_CONSENT_VERSIONS = frozenset(("v1",))' \
  'MARKETING_CONSENT_VERSIONS = frozenset("v1")' \
  'MARKETING_CONSENT_VERSIONS = {"v1"}' \
  'X = os.environ.get("MARKETING_CONSENT_VERSIONS", "dev-test-v0")' ; do
  printf '%s\n' "$evil" > "$tmp/evil.py"
  if bash "$gate" "$tmp" >/dev/null 2>&1; then
    echo "FAIL: gate did NOT fire on a synthetic non-empty default: $evil"
    exit 1
  fi
  rm -f "$tmp/evil.py"
done

# 2. Clean forms → the gate MUST pass (zero exit): the empty-default + env-driven shapes we ship.
for ok in \
  'MARKETING_CONSENT_VERSIONS: frozenset[str] = frozenset()' \
  'MARKETING_CONSENT_VERSIONS: frozenset[str] = _parse_marketing_consent_versions()' \
  'raw = os.environ.get("MARKETING_CONSENT_VERSIONS", "")' ; do
  printf '%s\n' "$ok" > "$tmp/clean.py"
  if ! bash "$gate" "$tmp" >/dev/null 2>&1; then
    echo "FAIL: gate fired on a clean empty-default form (false positive): $ok"
    exit 1
  fi
  rm -f "$tmp/clean.py"
done

echo "gate-marketing-consent-default-empty self-test: PASS (fires on a non-empty default, passes when empty/env-driven)"
