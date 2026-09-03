#!/bin/bash
set -u

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
if [ -x "$APP_ROOT/tools/open.sh" ]; then
  SF_HOUSING_APP_ROOT="$APP_ROOT" "$APP_ROOT/tools/open.sh"
else
  osascript -e 'display dialog "SF Housing Monitor is not installed yet. Run Install SF Housing Monitor first." buttons {"OK"} default button "OK" with icon caution'
fi
