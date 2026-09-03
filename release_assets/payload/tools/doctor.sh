#!/bin/bash
set -euo pipefail

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
PORT="${SF_HOUSING_PORT:-8000}"
URL="http://127.0.0.1:$PORT/"
OPEN_TOOL="$APP_ROOT/tools/open.sh"

show_problem() {
  MESSAGE="$1"
  printf '%s\n' "$MESSAGE"
  if [ "${SF_HOUSING_NO_BROWSER:-0}" != "1" ]; then
    /usr/bin/osascript -e "display dialog \"$MESSAGE\" buttons {\"OK\"} default button \"OK\" with icon caution"
  fi
}

if [ ! -x "$OPEN_TOOL" ]; then
  show_problem "SF Housing Monitor is not installed. Run Install SF Housing Monitor first."
  exit 1
fi

# Preserve the isolated-validation mode when Verify invokes Open.  Without
# this, a healthy dashboard deliberately launched without a LaunchAgent is
# mistaken for an unrelated app occupying the selected port.
if ! SF_HOUSING_APP_ROOT="$APP_ROOT" SF_HOUSING_PORT="$PORT" SF_HOUSING_NO_LAUNCH_AGENT="${SF_HOUSING_NO_LAUNCH_AGENT:-0}" SF_HOUSING_NO_BROWSER="${SF_HOUSING_NO_BROWSER:-0}" "$OPEN_TOOL" --no-browser; then
  show_problem "The ready check could not start the local dashboard. Run Repair SF Housing Monitor, then Verify again."
  exit 1
fi

REPORT="$(/usr/bin/curl -fsS --max-time 5 "${URL}support/report.json" 2>/dev/null || true)"
case "$REPORT" in
  *'"overall"'*) ;;
  *)
    show_problem "The local dashboard started, but its ready report did not respond. Run Repair SF Housing Monitor."
    exit 1
    ;;
esac

printf 'Ready check completed. Opening the private support page.\n'
if [ "${SF_HOUSING_NO_BROWSER:-0}" != "1" ]; then
  /usr/bin/open "${URL}support"
fi
