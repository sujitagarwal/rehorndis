#!/bin/bash
# Removes rehorndis (install dir + command). Reads the same env overrides as install.sh.
set -euo pipefail

PREFIX="${REHORNDIS_HOME:-$HOME/.rehorndis}"
BINDIR="${REHORNDIS_BIN:-/usr/local/bin}"

if pgrep -f "$PREFIX/src/main.py" >/dev/null 2>&1; then
  echo "rehorndis is running; stop it (Ctrl+C) first so routes/DNS are restored." >&2
  exit 1
fi

AGENT="$HOME/Library/LaunchAgents/com.sujitagarwal.rehorndis.plist"
launchctl bootout "gui/$(id -u)" "$AGENT" >/dev/null 2>&1 || true
rm -f "$AGENT"
LINK="$BINDIR/rehorndis"
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$PREFIX/rehorndis" ]; then
  if [ -w "$BINDIR" ]; then rm -f "$LINK"; else sudo rm -f "$LINK"; fi
fi
rm -rf "$PREFIX"
echo "rehorndis removed. (libusb left installed; remove with: brew uninstall libusb)"
