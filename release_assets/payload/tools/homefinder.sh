#!/bin/bash
# homefinder -- open and look after SF Home Finder from a terminal.
#
# Installed to ~/.local/bin/homefinder. Everything here delegates to the tools
# beside the app itself, so this stays a way in rather than a second
# implementation of anything.
set -euo pipefail

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
PORT="${SF_HOUSING_PORT:-8000}"
URL="http://127.0.0.1:$PORT/"
TOOLS="$APP_ROOT/tools"
LABEL="com.sfhousing.monitor"
# The app carries its own Python, so reading a JSON field never depends on what
# the machine happens to have.
PY="$APP_ROOT/current/bin/python"

say() { printf '%s\n' "$*"; }
health() { /usr/bin/curl -fsS --max-time 4 "${URL}health" 2>/dev/null || true; }

field() {
  [ -x "$PY" ] || return 1
  "$PY" -c 'import json,sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
for key in sys.argv[1].split("."):
    d = d.get(key) if isinstance(d, dict) else None
print("" if d is None else d)' "$1" 2>/dev/null
}

usage() {
  cat <<USAGE
homefinder -- your San Francisco housing search

  homefinder           open your dashboard
  homefinder help      show this

Rarely needed:

  homefinder status    is it running, and when does it check next
  homefinder check     look for new homes now (once a day)
  homefinder logs      what it has been doing lately
  homefinder restart   restart it, keeping everything
  homefinder repair    fix it without losing your deal or your homes
  homefinder uninstall remove it (your homes are kept unless you say otherwise)

It runs on its own at $URL. You do not have to start it.
USAGE
}

case "${1:-open}" in
  open|"")
    if [ -x "$TOOLS/open.sh" ]; then exec "$TOOLS/open.sh" "$@"; fi
    /usr/bin/open "$URL"
    ;;

  status)
    body="$(health)"
    if [ -z "$body" ]; then
      say "Not answering yet at $URL"
      say "  It may still be starting. Try 'homefinder restart' if it stays quiet."
      exit 1
    fi
    version="$(printf '%s' "$body" | field version || true)"
    running="$(printf '%s' "$body" | field scan_running || true)"
    state="$(printf '%s' "$body" | field scheduled_checking.state || true)"
    summary="$(printf '%s' "$body" | field scheduled_checking.summary || true)"
    say "Running${version:+ $version} at $URL"
    [ -n "$summary" ] && say "  $summary"
    [ "$running" = "True" ] && say "  Checking right now."
    [ -n "$state" ] && [ "$state" != "current" ] && say "  Schedule: $state"
    ;;

  check|scan)
    # The app refuses a POST that does not come from its own origin, which is
    # what stops a web page you happen to have open from telling your dashboard
    # to do things. This request genuinely is from the local dashboard's origin,
    # so it says so; the browser protection is untouched either way.
    #
    # -f is deliberately absent: a refusal here is an answer worth reading
    # rather than a failure, and with it curl would exit before the reply could
    # be looked at.
    location="$(/usr/bin/curl -sS -o /dev/null -D - --max-time 20 \
      -H "Origin: http://127.0.0.1:$PORT" \
      -X POST -d "return_to=/" "${URL}scan" 2>/dev/null \
      | /usr/bin/sed -n 's/^[Ll]ocation: //p' | tr -d '\r' || true)"
    # A refusal comes back as a redirect carrying its reason: "message" when it
    # is a normal no (already running, or today's check is spent) and "error"
    # when something has to be done first. Both are worth repeating verbatim --
    # they are already written for a person.
    case "$location" in
      *scan=starting*) say "Checking now. Watch it at $URL" ;;
      *message=*|*error=*)
        note="${location#*=}"
        case "$location" in
          *message=*) note="${location#*message=}" ;;
          *error=*) note="${location#*error=}" ;;
        esac
        note="${note%%&*}"; note="${note//+/ }"
        printf '%b\n' "${note//%/\\x}"
        ;;
      "") say "Could not reach it at $URL. Is it running? Try 'homefinder status'." ; exit 1 ;;
      *) say "Asked it to check. Watch it at $URL" ;;
    esac
    ;;

  logs)
    log="$APP_ROOT/logs/service.log"
    [ -f "$log" ] || { say "No log yet at $log"; exit 1; }
    exec /usr/bin/tail -n "${2:-40}" "$log"
    ;;

  restart)
    if [ -f "$HOME/Library/LaunchAgents/$LABEL.plist" ]; then
      /bin/launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
      say "Restarting. Give it a few seconds, then: homefinder status"
    else
      say "No login service is installed. Try 'homefinder repair'."
      exit 1
    fi
    ;;

  repair)
    [ -x "$TOOLS/repair.sh" ] || { say "Repair is not installed. Re-run the installer."; exit 1; }
    exec "$TOOLS/repair.sh"
    ;;

  uninstall)
    [ -x "$TOOLS/uninstall.sh" ] || { say "Uninstall is not installed."; exit 1; }
    exec "$TOOLS/uninstall.sh"
    ;;

  -h|--help|help) usage ;;
  *) say "homefinder: no such command '$1'"; say ""; usage; exit 1 ;;
esac
