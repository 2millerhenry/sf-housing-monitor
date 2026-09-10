#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
if [ -x "$APP_ROOT/tools/repair.sh" ]; then
  SF_HOUSING_APP_ROOT="$APP_ROOT" SF_HOUSING_RELEASE_ROOT="$SCRIPT_DIR" "$APP_ROOT/tools/repair.sh"
else
  "$SCRIPT_DIR/payload/install.sh" "$SCRIPT_DIR"
fi
