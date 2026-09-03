#!/bin/bash
set -euo pipefail

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
RELEASE_ROOT="$(cd "$(dirname "$0")/../.." && pwd 2>/dev/null || true)"

if [ -x "$RELEASE_ROOT/payload/install.sh" ]; then
  INSTALLER="$RELEASE_ROOT/payload/install.sh"
elif [ -n "${SF_HOUSING_RELEASE_ROOT:-}" ] && [ -x "$SF_HOUSING_RELEASE_ROOT/payload/install.sh" ]; then
  RELEASE_ROOT="$SF_HOUSING_RELEASE_ROOT"
  INSTALLER="$SF_HOUSING_RELEASE_ROOT/payload/install.sh"
else
  printf 'Repair needs the downloaded SF Housing Monitor folder. Open it and double-click Repair there.\n'
  exit 1
fi

if [ -f "$APP_ROOT/data/housing.sqlite3" ]; then
  /bin/mkdir -p "$APP_ROOT/backups"
  STAMP="$(/bin/date +%Y%m%d-%H%M%S)"
  /bin/cp "$APP_ROOT/data/housing.sqlite3" "$APP_ROOT/backups/housing-$STAMP.sqlite3"
  /usr/bin/find "$APP_ROOT/backups" -type f -name 'housing-*.sqlite3' -mtime +30 -delete
fi
printf 'Repairing application files. Your profile, listings, and connectors will be preserved.\n'
"$INSTALLER" "$(cd "$RELEASE_ROOT" && pwd)"
