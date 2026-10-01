#!/bin/sh
set -eu
LABEL="com.usagebar.app"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -x UsageBar 2>/dev/null || true
LINK="$HOME/.local/bin/usagebar"
case "$(readlink "$LINK" 2>/dev/null || true)" in
  */usage.py) rm -f "$LINK" ;;
esac
echo "UsageBar removed (state in ~/Library/Application Support/UsageBar left in place)"
