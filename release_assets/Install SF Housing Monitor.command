#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$HOME/Desktop/SF Housing Monitor Install.log"
"$SCRIPT_DIR/payload/install.sh" "$SCRIPT_DIR" 2>&1 | tee "$LOG_FILE"
STATUS=${PIPESTATUS[0]}
if [ "$STATUS" -ne 0 ]; then
  echo
  echo "Installation did not finish. The log is on your Desktop:"
  echo "$LOG_FILE"
  read -r -p "Press Return to close."
fi
exit "$STATUS"
