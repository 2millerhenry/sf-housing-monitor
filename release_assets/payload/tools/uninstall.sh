#!/bin/bash
set -euo pipefail

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
LABEL="com.sfhousing.monitor"
PLIST_PATH="${SF_HOUSING_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}/$LABEL.plist"

if [ -f "$PLIST_PATH" ] && [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" != "1" ]; then
  /bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
fi
if [ -f "$APP_ROOT/service.pid" ]; then
  PID="$(/bin/cat "$APP_ROOT/service.pid" 2>/dev/null || true)"
  case "$PID" in
    ''|*[!0-9]*) ;;
    *) /bin/kill "$PID" >/dev/null 2>&1 || true ;;
  esac
fi
/bin/rm -f "$PLIST_PATH"
/bin/rm -rf "$APP_ROOT/current" "$APP_ROOT/runtimes" "$APP_ROOT/releases" "$APP_ROOT/python" "$APP_ROOT/cache" "$APP_ROOT/tools"
/bin/rm -f "$APP_ROOT/service.pid"

printf 'SF Housing Monitor was removed. Your private data remains at:\n%s/data\n' "$APP_ROOT"
printf 'To permanently delete it too, type DELETE now. Press Return to keep it: '
read -r CONFIRMATION || CONFIRMATION=""
if [ "$CONFIRMATION" = "DELETE" ]; then
  /bin/rm -rf "$APP_ROOT/data" "$APP_ROOT/logs" "$APP_ROOT/backups"
  printf 'Private data was permanently deleted.\n'
else
  printf 'Private data was preserved. A future install will restore it.\n'
fi

