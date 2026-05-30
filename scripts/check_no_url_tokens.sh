#!/usr/bin/env bash
# VT-253 — CI guard: reject committed URL auth-token VALUES (magic-link / OAuth /
# reset tokens) that gitleaks misses (query-param tokens). Scans TRACKED files for
# `token=` / `access_token=` followed by a real token value (>=20 token-charset
# chars). Value-matching: passes elided `access_token=...` + `error=missing_token`
# narrative; trips only actual tokens. From the VT-252 session-export leak.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2

# token charset: hex / base64url / JWT (. _ -). Require >=20 chars after the '='.
PATTERN='(token|access_token)=[A-Za-z0-9._-]{20,}'

# Scan tracked files only; skip the guard's own files (script + its test, which
# reference the pattern / build sample tokens). Match the shared stem so both
# paths are excluded.
HITS=$(git ls-files \
  | grep -vE 'check_no_url_tokens' \
  | while read -r f; do
      grep -InHE "$PATTERN" "$f" 2>/dev/null || true
    done)

if [ -n "$HITS" ]; then
  echo "::error::URL auth-token value(s) found in tracked files (VT-253 guard):" >&2
  echo "$HITS" >&2
  echo "Scrub the token (token=REDACTED) before committing. Rotate if it was live." >&2
  exit 1
fi
echo "check-no-url-tokens: ok (no URL auth-token values in tracked files)"
exit 0
