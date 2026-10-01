#!/bin/sh
# Builds UsageBar and registers it as a launchd agent (starts at login, restarts if it dies).
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
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/usagebar.err</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -x UsageBar 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

mkdir -p "$HOME/.local/bin"
ln -sfn "$DIR/usage.py" "$HOME/.local/bin/usagebar"
echo "UsageBar installed: $PLIST"
echo "CLI: $HOME/.local/bin/usagebar"
