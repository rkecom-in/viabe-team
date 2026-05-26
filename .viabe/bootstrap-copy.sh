#!/bin/bash
# bootstrap-copy.sh — extract the current watch-mode bootstrap from .viabe/BOOTSTRAP.md
# and copy it to the macOS clipboard. Run before opening `claude` interactive session.
#
# USAGE:
#   bash /Users/fazalkhan/development/viabe-team/.viabe/bootstrap-copy.sh
#   claude -c    # or `claude` for fresh session
#   # ⌘V to paste the bootstrap

set -euo pipefail
SRC="/Users/fazalkhan/development/viabe-team/.viabe/BOOTSTRAP.md"

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found"
    exit 1
fi

if ! command -v pbcopy >/dev/null 2>&1; then
    echo "ERROR: pbcopy not in PATH (this script targets macOS)"
    exit 1
fi

# Extract everything from "Read /Users/..." to "Re-read protocol every..." inclusive,
# strip the markdown blockquote prefix (`> ` or `>`), copy to clipboard.
sed -n '/^> Read .*\.viabe\/protocol\.md/,/^> \*\*Re-read protocol every/p' "$SRC" \
    | sed -E 's/^> ?//; s/^>$//' \
    | pbcopy

LINES=$(pbpaste | wc -l | tr -d ' ')
echo "Bootstrap copied to clipboard ($LINES lines)."
echo "Now run: claude -c    (or  claude  for fresh session)"
echo "Then ⌘V to paste, ↵ to send."
