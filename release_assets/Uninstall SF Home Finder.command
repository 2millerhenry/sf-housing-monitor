#!/bin/bash
set -u

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
if [ -x "$APP_ROOT/tools/uninstall.sh" ]; then
  SF_HOUSING_APP_ROOT="$APP_ROOT" "$APP_ROOT/tools/uninstall.sh"
else
  osascript -e 'display dialog "SF Home Finder is not installed on this Mac." buttons {"OK"} default button "OK"'
fi
