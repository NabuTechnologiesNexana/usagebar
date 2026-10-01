#!/bin/sh
set -eu
LABEL="com.usagebar.app"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -x UsageBar 2>/dev/null || true
remove_cli_link() {
  dest="$1"
  case "$(readlink "$dest" 2>/dev/null || true)" in
    */usage.py)
      if [ -w "$(dirname "$dest")" ]; then
        rm -f "$dest"
      else
        sudo rm -f "$dest"
      fi
      ;;
  esac
}
remove_cli_link "$HOME/.local/bin/usagebar"
remove_cli_link "/usr/local/bin/usagebar"
echo "UsageBar removed (state in ~/Library/Application Support/UsageBar left in place)"
