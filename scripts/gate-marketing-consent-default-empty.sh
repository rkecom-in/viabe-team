#!/usr/bin/env bash
# VT-396 step-3 — C2 prod-safety gate (money/PII): the marketing-consent allowlist
# ``MARKETING_CONSENT_VERSIONS`` MUST default to EMPTY in committed code. Its non-empty value lives
# ONLY in the Railway *dev* env var (never in git), guarded at runtime by
# sales_recovery_executor._assert_consent_versions_prod_safe under VIABE_ENV=production. This gate
# closes the CODE path: no committed orchestrator file may seed a NON-EMPTY default, so a non-empty
# allowlist can never be merged into a branch and reach prod by code.
#
# FAILS (exit 1) if any committed orchestrator .py either:
#   (1) seeds a string literal into the constant's collection RHS —
#       MARKETING_CONSENT_VERSIONS = frozenset({"v…"}) / frozenset(("v",)) / frozenset(["v"]) /
#       frozenset("v") / set("v") / {"v"}  ; or
#   (2) gives os.environ.get("MARKETING_CONSENT_VERSIONS", "<non-empty>") a non-empty default.
# A bare frozenset() RHS or a _parse_marketing_consent_versions() RHS PASSES; an empty "" .get
# default PASSES.
#
# Args: search dirs (default the orchestrator src tree). Exit 1 if a non-empty default is found, else 0.
# Mirrors scripts/gate-no-price-literals.sh; self-tested by scripts/test-gate-marketing-consent-default-empty.sh.
set -uo pipefail

dirs=("$@")
[ ${#dirs[@]} -eq 0 ] && dirs=(apps/team-orchestrator/src)

# Scope: orchestrator python only, excluding tests + worktree copies (tests legitimately
# monkeypatch a non-empty value; .claude/worktrees/* are noise per plan §0).
common_filter='/tests/|/test_|test_.*\.py|\.claude/worktrees/'

# (1) A string literal seeded into the constant's collection RHS. The RHS must contain a quote
# AFTER an opening collection bracket/paren — so frozenset(), frozenset(<var>), and
# _parse_marketing_consent_versions() do NOT match, but frozenset({"v"}) / set("v") / {"v"} do.
literal_seed='MARKETING_CONSENT_VERSIONS[[:space:]]*[:=][^=].*(frozenset|set)[[:space:]]*\([[:space:]]*[\[{(]?[[:space:]]*["'"'"']'
literal_brace='MARKETING_CONSENT_VERSIONS[[:space:]]*[:=][^=].*\{[[:space:]]*["'"'"']'

# (2) A non-empty default arg to os.environ.get("MARKETING_CONSENT_VERSIONS", ...).
env_default='os\.environ\.get\([[:space:]]*["'"'"']MARKETING_CONSENT_VERSIONS["'"'"'][[:space:]]*,[[:space:]]*["'"'"'][^"'"'"']'

hit=0
for pat in "$literal_seed" "$literal_brace" "$env_default"; do
  if grep -rnE "$pat" "${dirs[@]}" --include="*.py" | grep -vE "$common_filter"; then
    hit=1
  fi
done

if [ "$hit" -ne 0 ]; then
  echo "::error::MARKETING_CONSENT_VERSIONS seeds a NON-EMPTY default in committed code (VT-396 C2) — the prod/main default MUST stay empty; the value lives ONLY in the Railway dev env var."
  exit 1
fi
echo "gate-marketing-consent-default-empty: ok"
