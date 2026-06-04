#!/usr/bin/env bash
# VT-316 — regression test for the pre-push empty-range FAIL-LOUD fix.
#
# The hook must NEVER silently `exit 0` (pass) when the push range computes an
# empty change-set while commits exist — that let unverified code through (the
# heredoc-consumed-stdin bug). This drives the hook's range resolution via
# PREPUSH_RANGE_ONLY (which short-circuits before the suite) with crafted stdin.
#
# Run: bash scripts/test_prepush_range.sh   (from anywhere in the repo)
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK="$REPO_ROOT/scripts/git-hooks/pre-push"
cd "$REPO_ROOT" || exit 1
HEAD_SHA="$(git rev-parse HEAD)"
Z="0000000000000000000000000000000000000000"
fails=0
ok()   { printf '  ok: %s\n' "$1"; }
bad()  { printf '  FAIL: %s\n' "$1"; fails=$((fails+1)); }

# 1. SOURCE GUARD — the silent `exit 0` on empty CHANGED must be gated behind the
#    ahead/fallback logic, not bare. Assert the fail-loud branch + the ahead check exist.
src="$(cat "$HOOK")"
case "$src" in
  *"Refusing to skip the gate silently (VT-316)"*) ok "fail-loud guard present in hook" ;;
  *) bad "fail-loud guard string missing from hook" ;;
esac
case "$src" in
  *"rev-list --count origin/main..HEAD"*) ok "ahead-of-origin check present" ;;
  *) bad "ahead-of-origin check missing" ;;
esac

# 2. DEGENERATE RANGE → RECOVERY (not silent skip): remote_sha == local_sha == HEAD
#    makes the diff empty; the branch is ahead of origin/main with real file
#    changes, so the fallback recovers → RANGE_OK printed.
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)"
if [ "${ahead:-0}" != "0" ]; then
  out="$(printf 'refs/heads/x %s refs/heads/x %s\n' "$HEAD_SHA" "$HEAD_SHA" \
        | PREPUSH_RANGE_ONLY=1 bash "$HOOK" 2>&1)"
  case "$out" in
    *RANGE_OK*) ok "degenerate range recovered via HEAD-vs-origin/main fallback" ;;
    *) bad "degenerate range did NOT recover (silent-skip risk): $out" ;;
  esac
else
  ok "skip recovery test — branch not ahead of origin/main"
fi

# 3. FAIL-LOUD: an empty commit ahead of origin/main + degenerate range = no file
#    diff anywhere while commits exist → MUST exit non-zero (never pass).
git commit --allow-empty -q -m "vt316-test-empty (transient)"
empty_head="$(git rev-parse HEAD)"
set +e
printf 'refs/heads/x %s refs/heads/x %s\n' "$empty_head" "$empty_head" \
  | PREPUSH_RANGE_ONLY=1 bash "$HOOK" >/dev/null 2>&1
rc=$?
set -e 2>/dev/null || true
git reset --hard -q "$HEAD_SHA"   # restore: drop the transient empty commit
if [ "$rc" -ne 0 ]; then
  ok "empty-commit-only range fails loud (exit $rc), does not silently pass"
else
  bad "empty-commit-only range PASSED silently (exit 0) — the VT-316 bug is back"
fi

# 4. GENUINE no-op: HEAD == origin/main → clean exit 0 (no RANGE_OK, no fail).
#    (Best-effort: only when origin/main == HEAD, e.g. right after a sync.)
if [ "$(git rev-parse origin/main 2>/dev/null)" = "$HEAD_SHA" ]; then
  out="$(printf 'refs/heads/x %s refs/heads/x %s\n' "$Z" "$Z" \
        | PREPUSH_RANGE_ONLY=1 bash "$HOOK" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && ! printf '%s' "$out" | grep -q RANGE_OK; then
    ok "genuine no-op passes cleanly"
  else
    bad "genuine no-op did not pass cleanly: rc=$rc out=$out"
  fi
else
  ok "skip no-op test — HEAD != origin/main"
fi

if [ "$fails" -eq 0 ]; then
  printf '\nvt316 pre-push range guard: ALL PASS\n'; exit 0
fi
printf '\nvt316 pre-push range guard: %d FAIL\n' "$fails"; exit 1
