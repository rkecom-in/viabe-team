#!/usr/bin/env bash
# VT-310 — local test-cluster hygiene: reap leftover canary / throwaway DBs that
# hold GRANTs on the shared cluster roles (app_role / app_operator_role /
# rls_tester). When those DBs linger, `DROP ROLE app_role` fails ("objects
# depend on it"), the role keeps stale per-DB grants, and the next DB-job
# pre-push false-fails with `permission denied for table ...` on the RLS tests.
# This bit the L2 + L3 builds repeatedly. Run it before a throwaway-DB run (the
# pre-push hook calls it automatically) or any time the cluster feels polluted.
#
# SAFETY: this cluster is test-only (real dev/prod = remote Supabase). The reaper
# drops ONLY DBs whose name matches a known test pattern AND is not in the
# keep-list. It never touches `postgres`, templates, or a keep-listed name.
#
# Usage:
#   scripts/reap_test_dbs.sh [--dry-run]
#   VIABE_REAP_DSN=postgres://localhost:5432/postgres scripts/reap_test_dbs.sh
#   VIABE_REAP_KEEP="viabe_team,my_local_db" scripts/reap_test_dbs.sh
set -euo pipefail

ADMIN_DSN="${VIABE_REAP_DSN:-postgres://localhost:5432/postgres}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# Names we must NEVER drop (in addition to postgres/templates). Extend via
# VIABE_REAP_KEEP (comma-separated) if a real local DB lives on this cluster.
KEEP_DEFAULT="postgres,template0,template1,viabe,viabe_team,viabe_dev,viabe_prod"
KEEP="${KEEP_DEFAULT},${VIABE_REAP_KEEP:-}"

# Test-DB name patterns (SQL regex). Covers every observed shape: throwaway
# pre-push DBs, per-VT canary DBs, DBOS sidecar DBs, and the *_mig/_full/_canary
# helpers used in this session.
PATTERN='(_dbos_sys$)|(_canary)|(_prepush)|(_mig[0-9]*$)|(_full$)|(^vt[0-9_])|(^viabe_(vt|prb|prc|consent|imptx|shope|shopify|[0-9]))'

if ! command -v psql >/dev/null 2>&1; then
  echo "reap_test_dbs: psql not found — nothing to do" >&2
  exit 0
fi
if ! psql "$ADMIN_DSN" -tAqc "SELECT 1" >/dev/null 2>&1; then
  echo "reap_test_dbs: cannot reach $ADMIN_DSN — nothing to do" >&2
  exit 0
fi

# Build the keep-list as a quoted SQL IN (...) set.
keep_in="$(printf "'%s'," $(echo "$KEEP" | tr ',' ' ') | sed 's/,$//')"

# bash 3.2 (macOS default) has no `mapfile` — read into the array portably.
TARGETS=()
while IFS= read -r _db; do
  [ -n "$_db" ] && TARGETS+=("$_db")
done < <(psql "$ADMIN_DSN" -tAc "
  SELECT datname FROM pg_database
  WHERE datistemplate = false
    AND datname NOT IN ($keep_in)
    AND datname ~ '$PATTERN'
  ORDER BY datname
")

if [ "${#TARGETS[@]}" -eq 0 ]; then
  echo "reap_test_dbs: no test DBs to reap (clean)."
else
  echo "reap_test_dbs: ${#TARGETS[@]} test DB(s) to reap:"
  for db in "${TARGETS[@]}"; do echo "  - $db"; done
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "reap_test_dbs: --dry-run — not dropping."
    exit 0
  fi
  for db in "${TARGETS[@]}"; do
    psql "$ADMIN_DSN" -tAqc "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" >/dev/null 2>&1 \
      && echo "  dropped $db" || echo "  FAILED to drop $db" >&2
  done
fi

# Now the shared roles can be dropped cleanly (no DB still depends on them).
for role in app_role app_operator_role rls_tester; do
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "reap_test_dbs: --dry-run — would drop role $role"
  else
    psql "$ADMIN_DSN" -tAqc "DROP ROLE IF EXISTS $role" >/dev/null 2>&1 \
      && true || echo "  note: role $role not dropped (may still have deps)" >&2
  fi
done

remaining_roles="$(psql "$ADMIN_DSN" -tAc \
  "SELECT count(*) FROM pg_roles WHERE rolname IN ('app_role','app_operator_role','rls_tester')")"
echo "reap_test_dbs: done. shared test roles remaining: $remaining_roles (0 = clean)."
