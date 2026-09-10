#!/bin/bash
set -u

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
if [ -x "$APP_ROOT/tools/doctor.sh" ]; then
  SF_HOUSING_APP_ROOT="$APP_ROOT" "$APP_ROOT/tools/doctor.sh"
else
  /usr/bin/osascript -e 'display dialog "SF Home Finder is not installed yet. Run Install SF Home Finder first." buttons {"OK"} default button "OK" with icon caution'
fi
