#!/bin/bash
# Removes rehorndis (install dir + command). Reads the same env overrides as install.sh.
set -euo pipefail

PREFIX="${REHORNDIS_HOME:-$HOME/.rehorndis}"
BINDIR="${REHORNDIS_BIN:-/usr/local/bin}"

if pgrep -f "$PREFIX/src/main.py" >/dev/null 2>&1; then
  echo "rehorndis is running; stop it (Ctrl+C) first so routes/DNS are restored." >&2
  exit 1
fi

LINK="$BINDIR/rehorndis"
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$PREFIX/rehorndis" ]; then
  if [ -w "$BINDIR" ]; then rm -f "$LINK"; else sudo rm -f "$LINK"; fi
fi
rm -rf "$PREFIX"
echo "rehorndis removed. (libusb left installed; remove with: brew uninstall libusb)"
