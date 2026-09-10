#!/bin/bash
set -euo pipefail

RELEASE_ROOT="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
PAYLOAD_DIR="$RELEASE_ROOT/payload"
VERSION="0.4.0"
PYTHON_VERSION="3.12.10"
PORT="${SF_HOUSING_PORT:-8000}"
APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
# Isolated validation must not touch the real account's login services. Writing
# the plist to ~/Library/LaunchAgents regardless of this flag meant installing a
# second copy repointed the first one's login service at a temporary directory,
# and the damage only appeared at the next login.
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
  DEFAULT_LAUNCH_AGENTS_DIR="$APP_ROOT/LaunchAgents"
else
  DEFAULT_LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
fi
LAUNCH_AGENTS_DIR="${SF_HOUSING_LAUNCH_AGENTS_DIR:-$DEFAULT_LAUNCH_AGENTS_DIR}"
LABEL="com.sfhousing.monitor"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
DATA_DIR="$APP_ROOT/data"
LOG_DIR="$APP_ROOT/logs"
TOOLS_DIR="$APP_ROOT/tools"
RUNTIMES_DIR="$APP_ROOT/runtimes"
RELEASES_DIR="$APP_ROOT/releases"
UV_BIN="$PAYLOAD_DIR/uv"
LOCK_FILE="$PAYLOAD_DIR/requirements.lock"
WHEEL_FILE="$PAYLOAD_DIR/sf_housing_monitor-0.4.0-py3-none-any.whl"

say() { printf '%s\n' "$*"; }
fail() { say "Installation stopped: $*"; exit 1; }

if [ "$(uname -s)" != "Darwin" ]; then
  fail "this release supports macOS only."
fi
if [ "$(uname -m)" != "arm64" ]; then
  fail "this build supports Apple Silicon Macs only. Ask for an Intel build instead of forcing this one."
fi
# A ZIP downloaded through a browser marks every extracted file with
# com.apple.quarantine, so macOS would otherwise re-prompt for Open, Repair,
# Verify and Uninstall one at a time. The user has already approved this
# release by opening the installer, so clear the flag once for the whole
# folder. This is never fatal: an unsigned build still installs without it.
/usr/bin/xattr -dr com.apple.quarantine "$RELEASE_ROOT" >/dev/null 2>&1 || true

for required in "$UV_BIN" "$LOCK_FILE" "$WHEEL_FILE" "$PAYLOAD_DIR/checksums.sha256"; do
  [ -f "$required" ] || fail "the release is incomplete ($(basename "$required") is missing). Download the ZIP again."
done
(cd "$PAYLOAD_DIR" && /usr/bin/shasum -a 256 -c checksums.sha256 >/dev/null) || fail "release verification failed. Download the ZIP again."

if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  HEALTH="$(/usr/bin/curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
  case "$HEALTH" in
    *'"app":"sf-housing-monitor"'*|*'"app": "sf-housing-monitor"'*|*'"ok":true'*|*'"ok": true'*) ;;
    *) fail "port $PORT is already used by another app. Close that app, then run Install again. Nothing was stopped." ;;
  esac
fi

say "Installing SF Housing Monitor $VERSION..."
/bin/mkdir -p "$DATA_DIR/config" "$LOG_DIR" "$TOOLS_DIR" "$RUNTIMES_DIR" "$RELEASES_DIR" "$LAUNCH_AGENTS_DIR"
/bin/chmod 700 "$APP_ROOT" "$DATA_DIR" "$LOG_DIR"

STAGE="$(/usr/bin/mktemp -d "$APP_ROOT/.install.XXXXXX")"
cleanup() { /bin/rm -rf "$STAGE"; }
trap cleanup EXIT

say "Preparing the private Python runtime (the first install needs internet access)..."
export UV_CACHE_DIR="$APP_ROOT/cache"
export UV_PYTHON_INSTALL_DIR="$APP_ROOT/python"
"$UV_BIN" python install "$PYTHON_VERSION" --install-dir "$UV_PYTHON_INSTALL_DIR" --no-bin --no-progress
"$UV_BIN" venv "$STAGE/runtime" --python "$PYTHON_VERSION" --managed-python --no-project
"$UV_BIN" pip sync "$LOCK_FILE" --python "$STAGE/runtime/bin/python" --strict --no-progress
"$UV_BIN" pip install "$WHEEL_FILE" --python "$STAGE/runtime/bin/python" --no-deps --no-progress
"$STAGE/runtime/bin/python" -c 'import sf_housing; assert sf_housing.__version__ == "0.4.0"'

if [ -f "$DATA_DIR/housing.sqlite3" ] && [ -x "$APP_ROOT/current/bin/python" ]; then
  /bin/mkdir -p "$APP_ROOT/backups"
  STAMP="$(/bin/date +%Y%m%d-%H%M%S)"
  "$APP_ROOT/current/bin/python" -c 'import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()' "$DATA_DIR/housing.sqlite3" "$APP_ROOT/backups/housing-$STAMP.sqlite3"
fi
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" != "1" ] && [ -f "$PLIST_PATH" ]; then
  /bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
fi

RUNTIME_TARGET="$RUNTIMES_DIR/$VERSION"
if [ -e "$RUNTIME_TARGET" ]; then
  /bin/mv "$RUNTIME_TARGET" "$STAGE/previous-runtime"
fi
/bin/mv "$STAGE/runtime" "$RUNTIME_TARGET"
/bin/ln -sfn "$RUNTIME_TARGET" "$APP_ROOT/current"

/bin/mkdir -p "$RELEASES_DIR/$VERSION"
/bin/cp "$WHEEL_FILE" "$LOCK_FILE" "$RELEASES_DIR/$VERSION/"
/bin/cp "$PAYLOAD_DIR/tools/"*.sh "$TOOLS_DIR/"
/bin/chmod 700 "$TOOLS_DIR/"*.sh
if [ -d "$PAYLOAD_DIR/furnished-finder-bridge" ]; then
  /bin/rm -rf "$APP_ROOT/furnished-finder-bridge.new"
  /bin/cp -R "$PAYLOAD_DIR/furnished-finder-bridge" "$APP_ROOT/furnished-finder-bridge.new"
  /bin/rm -rf "$APP_ROOT/furnished-finder-bridge"
  /bin/mv "$APP_ROOT/furnished-finder-bridge.new" "$APP_ROOT/furnished-finder-bridge"
fi

if [ -f "$PAYLOAD_DIR/gmail-client-secret.json" ] && [ ! -f "$DATA_DIR/gmail-client-secret.json" ]; then
  /bin/cp "$PAYLOAD_DIR/gmail-client-secret.json" "$DATA_DIR/gmail-client-secret.json"
  /bin/chmod 600 "$DATA_DIR/gmail-client-secret.json"
fi

PLIST_TMP="$STAGE/$LABEL.plist"
/bin/cat > "$PLIST_TMP" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_ROOT/current/bin/python</string>
    <string>-m</string><string>sf_housing</string><string>serve</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>SF_HOUSING_DATA_DIR</key><string>$DATA_DIR</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/service.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/service-error.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST
/usr/bin/plutil -lint "$PLIST_TMP" >/dev/null
/bin/cp "$PLIST_TMP" "$PLIST_PATH"
/bin/chmod 600 "$PLIST_PATH"

if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
  say "LaunchAgent installation skipped for isolated validation."
  SF_HOUSING_APP_ROOT="$APP_ROOT" SF_HOUSING_PORT="$PORT" \
    SF_HOUSING_LAUNCH_AGENTS_DIR="$LAUNCH_AGENTS_DIR" "$TOOLS_DIR/open.sh" --no-browser
else
  /bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
  /bin/launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" || fail "macOS could not start the login service. Run Repair and use the Desktop log if it repeats."
  /bin/launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  /bin/launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
fi

say "Waiting for the private dashboard..."
for attempt in $(/usr/bin/seq 1 45); do
  HEALTH="$(/usr/bin/curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
  case "$HEALTH" in
    *'"app":"sf-housing-monitor"'*|*'"app": "sf-housing-monitor"'*|*'"ok":true'*|*'"ok": true'*) break ;;
  esac
  /bin/sleep 1
done
case "${HEALTH:-}" in
  *'"app":"sf-housing-monitor"'*|*'"app": "sf-housing-monitor"'*|*'"ok":true'*|*'"ok": true'*) ;;
  *) fail "the service did not become healthy. Run Repair; details are in $LOG_DIR/service-error.log" ;;
esac

# Deliberately last, and only past the health check above: until the new
# runtime has actually served a request, the old one is the way back.
if [ -x "$TOOLS_DIR/reclaim.sh" ]; then
  SF_HOUSING_APP_ROOT="$APP_ROOT" "$TOOLS_DIR/reclaim.sh" || true
fi

say "Installed. Your profile and history stay in: $DATA_DIR"
if [ "${SF_HOUSING_NO_BROWSER:-0}" != "1" ]; then
  /usr/bin/open "http://127.0.0.1:$PORT/"
fi
