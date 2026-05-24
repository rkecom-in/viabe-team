#!/bin/bash
# watch-claude-inbox.sh
# Foreground watcher: polls .running/to-claudecode/ every 30s, fires Claude Code
# when a signal appears. Closes the Cowork→Claude Code direction of the queue
# protocol (the other direction is handled by the Cowork-side scheduled task
# `viabe-team-queue-poller` firing every 10 min).
#
# RUN: from a terminal, in this repo:
#     bash .viabe/daemon/watch-claude-inbox.sh
#
# STOP: Ctrl-C in the terminal.
#
# REQUIREMENTS:
#   - `claude` CLI in PATH (test with `which claude`)
#   - Bash 4+ (for nullglob); macOS default /bin/bash is 3.2 — use `/opt/homebrew/bin/bash`
#     or `/usr/local/bin/bash` if Homebrew bash is installed, OR ignore the shopt and accept
#     a literal "*.md" iteration when empty (handled below with explicit existence check).

set -euo pipefail

# --- config ---
REPO_ROOT="/Users/fazalkhan/development/viabe-team"
INBOX="$REPO_ROOT/.running/to-claudecode"
PROCESSED="$REPO_ROOT/.running/processed"
LOG="$REPO_ROOT/.viabe/daemon/watch.log"
POLL_SECONDS=30
PER_TASK_TIMEOUT_SECONDS=3600   # 60 min hard kill if Claude Code hangs

# --- bootstrap ---
mkdir -p "$INBOX" "$PROCESSED" "$(dirname "$LOG")"
cd "$REPO_ROOT"

log() {
    local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
    echo "$msg" | tee -a "$LOG"
}

# --- preflight ---
if ! command -v claude >/dev/null 2>&1; then
    log "ERROR: 'claude' CLI not in PATH. Install Claude Code or fix PATH. Exiting."
    exit 1
fi

if ! command -v timeout >/dev/null 2>&1; then
    # macOS: gtimeout from coreutils, or skip the timeout wrapping
    if command -v gtimeout >/dev/null 2>&1; then
        TIMEOUT_CMD="gtimeout"
    else
        log "WARN: neither 'timeout' nor 'gtimeout' found — Claude Code runs will not be time-bounded. brew install coreutils to enable."
        TIMEOUT_CMD=""
    fi
else
    TIMEOUT_CMD="timeout"
fi

log "watch-claude-inbox.sh started — polling every ${POLL_SECONDS}s"
log "  watching: $INBOX"
log "  processed → $PROCESSED"
log "  log → $LOG"
log "  per-task timeout: ${PER_TASK_TIMEOUT_SECONDS}s"
log "  Ctrl-C to stop."

# --- main loop ---
while true; do
    # Glob safely with an explicit existence check (bash 3.2 compatible)
    if ls "$INBOX"/*.md >/dev/null 2>&1; then
        for sig in "$INBOX"/*.md; do
            [ -e "$sig" ] || continue
            sig_name=$(basename "$sig")
            log "FOUND signal: $sig_name"

            # Build the prompt — Claude Code reads protocol + signal, processes per protocol
            prompt="You are Claude Code. Read /Users/fazalkhan/development/viabe-team/.viabe/protocol.md first — that's your operating contract.

Then process this Cowork signal at: $sig

Per the protocol:
1. Parse the frontmatter (from, to, task, type, ts).
2. Dispatch by type (review → continue/revise; question → answer or escalate; etc.).
3. After acting on the signal, MOVE it to /Users/fazalkhan/development/viabe-team/.running/processed/ (not delete).
4. Exit cleanly when done.

Do NOT process other signals in the inbox — only the one named above. The watcher fires once per signal."

            log "Firing Claude Code (timeout ${PER_TASK_TIMEOUT_SECONDS}s)..."

            # NOTE on flags:
            # --print           : headless mode (no interactive UI)
            # --dangerously-skip-permissions : skip approval prompts (required for unattended)
            # If your installed `claude` uses different flags, edit the line below.
            if [ -n "$TIMEOUT_CMD" ]; then
                $TIMEOUT_CMD "$PER_TASK_TIMEOUT_SECONDS" \
                    claude --print --dangerously-skip-permissions "$prompt" \
                    >> "$LOG" 2>&1 || log "WARN: claude returned non-zero or timed out for $sig_name"
            else
                claude --print --dangerously-skip-permissions "$prompt" \
                    >> "$LOG" 2>&1 || log "WARN: claude returned non-zero for $sig_name"
            fi

            # Safety net: if Claude Code didn't move the signal, do it ourselves
            # so the watcher doesn't re-fire on the same signal forever.
            if [ -e "$sig" ]; then
                log "WARN: signal $sig_name still in inbox after claude run — moving to processed/ defensively"
                mv "$sig" "$PROCESSED/"
            fi

            log "DONE: $sig_name"
        done
    fi

    sleep "$POLL_SECONDS"
done
