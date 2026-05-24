#!/bin/bash
# install-launchd.sh
#
# One-shot installer for the Viabe Team agent-loop daemon.
#
# Steps:
#   1. Create an isolated venv at .viabe/daemon/.venv
#   2. Install claude-agent-sdk into the venv
#   3. Copy the plist into ~/Library/LaunchAgents/
#   4. (Re)load via launchctl
#
# Re-running this script is safe — it unloads any prior plist before reloading.
#
# Uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.viabe.team.agent-loop.plist
#   rm        ~/Library/LaunchAgents/com.viabe.team.agent-loop.plist
#
# Manual fallback if pip fails:
#   .venv/bin/pip install claude-agent-sdk==0.2.87
set -euo pipefail

REPO="/Users/fazalkhan/development/viabe-team"
DAEMON_DIR="$REPO/.viabe/daemon"
PLIST_NAME="com.viabe.team.agent-loop"
PLIST_SRC="$DAEMON_DIR/$PLIST_NAME.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
VENV="$DAEMON_DIR/.venv"

cd "$DAEMON_DIR"

if [ ! -d "$VENV" ]; then
    echo "[+] Creating venv at $VENV"
    python3 -m venv "$VENV"
fi

echo "[+] Upgrading pip in venv"
"$VENV/bin/pip" install --upgrade pip >/dev/null

echo "[+] Installing claude-agent-sdk"
if ! "$VENV/bin/pip" install claude-agent-sdk; then
    echo "[!] pip install failed. Try the pinned manual fallback:"
    echo "    $VENV/bin/pip install claude-agent-sdk==0.2.87"
    exit 1
fi

echo "[+] Verifying import"
"$VENV/bin/python" -c "import claude_agent_sdk; print('claude_agent_sdk', claude_agent_sdk.__version__ if hasattr(claude_agent_sdk, '__version__') else 'imported OK')"

if [ ! -f "$PLIST_SRC" ]; then
    echo "[!] Missing $PLIST_SRC — aborting" >&2
    exit 1
fi

mkdir -p "$(dirname "$PLIST_DEST")"
cp "$PLIST_SRC" "$PLIST_DEST"
echo "[+] Plist copied to $PLIST_DEST"

if launchctl list | grep -q "$PLIST_NAME"; then
    echo "[+] Unloading existing $PLIST_NAME"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi
echo "[+] Loading $PLIST_NAME"
launchctl load "$PLIST_DEST"

echo
echo "Verify with:  launchctl list | grep $PLIST_NAME"
echo "Stop:         touch $DAEMON_DIR/STOP    (graceful — exits next iteration)"
echo "Hard stop:    launchctl unload $PLIST_DEST"
echo "Logs:         $DAEMON_DIR/agent-loop.log"
echo "Launchd log:  $DAEMON_DIR/launchd.log + .err"
