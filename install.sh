#!/bin/bash
# One-command install for SF Housing Monitor.
#
#   curl -fsSL https://sfhousing.link/install | bash
#
# ...or, without the short link:
#
#   curl -fsSL https://raw.githubusercontent.com/2millerhenry/sf-housing-monitor/main/install.sh | bash
#
# This downloads the current release, checks it, and runs the same installer the
# ZIP contains. Two things it deliberately does not do: ask for your password --
# nothing here needs an administrator, and anything claiming otherwise is not
# this -- and leave anything behind on failure.
#
# It also avoids the Gatekeeper prompt that the ZIP shows, for a real reason
# rather than a trick: macOS marks files a *browser* downloaded, and curl is not
# a browser. The bytes are identical either way, and the installer verifies every
# one of them against a checksum before using it.
set -euo pipefail

REPO="2millerhenry/sf-housing-monitor"
say() { printf '%s\n' "$*"; }
fail() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this app is macOS only."
[ "$(uname -m)" = "arm64" ] || fail "this build needs an Apple Silicon Mac (M1 or later). Intel Macs are not supported yet."

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say "Looking up the latest release..."
ASSET="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
  | grep -o '"browser_download_url": *"[^"]*macOS-arm64\.zip"' \
  | head -1 | sed 's/.*"https/https/; s/"$//')"
[ -n "$ASSET" ] || fail "could not find the download. Check https://github.com/$REPO/releases"

say "Downloading $(basename "$ASSET")..."
curl -fL --progress-bar "$ASSET" -o "$WORK/release.zip" || fail "the download did not finish. Check your internet connection and try again."

say "Unpacking..."
/usr/bin/unzip -q "$WORK/release.zip" -d "$WORK/unpacked" || fail "the download was incomplete. Run the command again."
ROOT="$(find "$WORK/unpacked" -maxdepth 1 -type d -name 'SF-Housing-Monitor-*' | head -1)"
[ -n "$ROOT" ] && [ -f "$ROOT/payload/install.sh" ] || fail "that download does not look like a release. Try again, or download the ZIP by hand."

say ""
exec /bin/bash "$ROOT/payload/install.sh" "$ROOT"
