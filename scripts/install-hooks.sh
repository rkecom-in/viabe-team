#!/usr/bin/env bash
# VT-245 — install the versioned git hooks into .git/hooks/.
#
# Symlinks scripts/git-hooks/* into .git/hooks/ so the tracked hooks run.
# Idempotent: re-running re-points the symlink. Run once after cloning:
#   ./scripts/install-hooks.sh
#
# The pre-push hook runs the fast CI-equivalent suite and aborts on
# failure (bypass: git push --no-verify). See scripts/git-hooks/pre-push.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SRC_DIR="$REPO_ROOT/scripts/git-hooks"
DST_DIR="$REPO_ROOT/.git/hooks"

mkdir -p "$DST_DIR"

for hook in "$SRC_DIR"/*; do
  name="$(basename "$hook")"
  chmod +x "$hook"
  ln -sf "../../scripts/git-hooks/$name" "$DST_DIR/$name"
  printf 'installed: .git/hooks/%s -> scripts/git-hooks/%s\n' "$name" "$name"
done

echo "git hooks installed. Bypass any hook with --no-verify."
