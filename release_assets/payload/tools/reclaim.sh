#!/bin/bash
# Give back the disk that installing upgrades leaves behind.
#
# Every upgrade builds a fresh Python environment in runtimes/<version> and
# never removed the older ones, so five versions had accumulated three quarters
# of a gigabyte of environments nobody could run. A runtime holds nothing a
# person typed: it is rebuilt from the wheel and the lock file on any install,
# so the only reason to keep one is to roll back to it if an upgrade goes bad.
# One is enough for that.
#
# What this must never touch, and cannot reach: data/ holds the database, the
# profile and the saved homes; backups/ holds copies of that database made
# before every upgrade. Both are outside the two directories named below, and
# the guards refuse anything that is not a direct child of them.
set -euo pipefail

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
RUNTIMES_DIR="$APP_ROOT/runtimes"
CACHE_DIR="$APP_ROOT/cache"

say() { /bin/echo "  $*"; }

# The environment being served right now. Read through the symlink rather than
# assumed from a version string: if it cannot be resolved, this script has no
# idea what is in use and does nothing at all.
in_use=""
if [ -L "$APP_ROOT/current" ]; then
  resolved="$(cd -P "$APP_ROOT/current" 2>/dev/null && pwd -P || true)"
  [ -n "$resolved" ] && in_use="$(/usr/bin/basename "$resolved")"
fi

if [ -z "$in_use" ] || [ ! -d "$RUNTIMES_DIR/$in_use" ]; then
  say "Skipped: the running version could not be identified, so nothing was removed."
  exit 0
fi

# Newest first, so the one kept for rollback is the most recent that is not
# already in use.
rollback=""
for candidate in $(/bin/ls -1 "$RUNTIMES_DIR" 2>/dev/null | /usr/bin/sort -Vr); do
  [ -d "$RUNTIMES_DIR/$candidate" ] || continue
  [ "$candidate" = "$in_use" ] && continue
  rollback="$candidate"
  break
done

freed=0
for candidate in $(/bin/ls -1 "$RUNTIMES_DIR" 2>/dev/null); do
  target="$RUNTIMES_DIR/$candidate"
  # Every guard has to hold: a directory, directly inside runtimes/, not the
  # one being served, and not the one kept to roll back to.
  [ -d "$target" ] || continue
  [ -L "$target" ] && continue
  case "$candidate" in */*|""|.|..) continue ;; esac
  [ "$candidate" = "$in_use" ] && continue
  [ "$candidate" = "$rollback" ] && continue
  /bin/rm -rf "$target"
  freed=$((freed + 1))
done

# uv's download cache. Everything in it came from the network and goes back to
# the network on the next upgrade, which already needs it.
if [ -d "$CACHE_DIR" ]; then
  /bin/rm -rf "$CACHE_DIR"
  /bin/mkdir -p "$CACHE_DIR"
fi

if [ "$freed" -gt 0 ]; then
  say "Removed $freed old runtime(s); kept $in_use and ${rollback:-no rollback copy}."
else
  say "Nothing to remove; kept $in_use and ${rollback:-no rollback copy}."
fi
