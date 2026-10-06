#!/bin/sh
# Builds UsageBar and registers it as a launchd agent (starts at login, restarts if it crashes; Quit keeps it closed).
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.usagebar.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

swiftc -O -o "$DIR/UsageBar" "$DIR/UsageBar.swift"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$DIR/UsageBar</string><string>$DIR/usage.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardErrorPath</key><string>/tmp/usagebar.err</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -x UsageBar 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# ~/.local/bin is not on the default macOS PATH. /usr/local/bin is (/etc/paths).
# That directory is root-owned, so the link there asks for an admin password.
mkdir -p "$HOME/.local/bin"
ln -sfn "$DIR/usage.py" "$HOME/.local/bin/usagebar"

CLI_LINK="/usr/local/bin/usagebar"
current="$(readlink "$CLI_LINK" 2>/dev/null || true)"
if [ -e "$CLI_LINK" ] || [ -L "$CLI_LINK" ]; then
  case "$current" in
    */usage.py) ;;
    *)
      echo "usagebar: $CLI_LINK exists and is not this script; not replacing it." >&2
      exit 1
      ;;
  esac
fi
if [ -w /usr/local/bin ]; then
  ln -sfn "$DIR/usage.py" "$CLI_LINK"
else
  echo "Linking usagebar into /usr/local/bin (admin password)..."
  if [ ! -d /usr/local/bin ]; then
    sudo mkdir -p /usr/local/bin
  fi
  sudo ln -sfn "$DIR/usage.py" "$CLI_LINK"
fi
echo "UsageBar installed: $PLIST"
echo "CLI: $CLI_LINK"
