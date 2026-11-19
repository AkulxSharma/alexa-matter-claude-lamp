#!/bin/zsh
# Runs the Matter lamp bridge inside Terminal.app and restarts it if it exits.
# Why Terminal: macOS grants Local Network (multicast/mDNS) and Bluetooth permission per app.
# A bare launchd job gets neither, so the bridge is launched through Terminal, which you approve once.
# caffeinate -i keeps the Mac from idle-sleeping while the bridge runs.
#
# Set NODE_BIN_DIR if node is not on the default PATH (for example when installed with nvm).
SCRIPT_DIR="${0:A:h}"
export PATH="${NODE_BIN_DIR:+$NODE_BIN_DIR:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
LOG="${LAMP_BRIDGE_LOG:-$HOME/Library/Logs/ble-lamp-matter-bridge.log}"
cd "$SCRIPT_DIR/../bridge" || exit 1
while true; do
  echo "$(date '+%F %T') starting lamp bridge" | tee -a "$LOG"
  caffeinate -i node lamp.mjs >> "$LOG" 2>&1
  echo "$(date '+%F %T') bridge exited ($?), restarting in 5s" | tee -a "$LOG"
  sleep 5
done
