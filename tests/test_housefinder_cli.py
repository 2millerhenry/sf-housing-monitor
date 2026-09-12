"""The terminal command, and the one way it can be installed uselessly.

Somebody who installs with the one-line command never has the folder of
.command shortcuts the ZIP carries, so the only ways back in are the bookmark
and this. It goes to ~/.local/bin because that needs no administrator, which is
what keeps the whole install password-free -- but that directory is not on
everybody's PATH, and a command that cannot be found is worse than no command,
because nothing says so.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "release_assets" / "payload" / "tools" / "housefinder.sh"
INSTALLER = (ROOT / "release_assets" / "payload" / "install.sh").read_text()
UNINSTALLER = (ROOT / "release_assets" / "payload" / "tools" / "uninstall.sh").read_text()


def test_the_command_is_valid_shell() -> None:
    assert subprocess.run(["bash", "-n", str(CLI)], capture_output=True).returncode == 0


def test_it_is_installed_somewhere_that_needs_no_password() -> None:
    """The install advertises that it never asks for one. Writing the command to
    /usr/local/bin or anywhere else root-owned would quietly break that."""
    assert 'CLI_DIR="$HOME/.local/bin"' in INSTALLER
    assert "sudo" not in INSTALLER


def test_a_command_that_will_not_be_found_says_so() -> None:
    """Installing it onto a PATH that does not include it, and saying nothing,
    leaves somebody typing a command that does not exist and concluding the app
    is broken."""
    assert 'case ":$PATH:" in' in INSTALLER
    assert "is not on your PATH yet" in INSTALLER
    # ...and it only claims the command works when it actually will.
    assert '[ "${CLI_READY:-0}" = 1 ] && [ "${CLI_ON_PATH:-0}" = 1 ]' in INSTALLER


def test_uninstalling_takes_the_command_with_it() -> None:
    """It lives outside the app folder, so removing the folder alone would leave
    a command behind pointing at nothing."""
    assert 'CLI_PATH="$HOME/.local/bin/housefinder"' in UNINSTALLER
    assert '/bin/rm -f "$CLI_PATH"' in UNINSTALLER


def test_every_advertised_command_is_handled() -> None:
    """The help is the contract. A verb listed there and missing from the case
    would fail with 'no such command' on something the app told you to type."""
    text = CLI.read_text()
    usage = text[text.index("housefinder -- your San Francisco"):text.index("USAGE")]
    advertised = {
        line.split()[1]
        for line in usage.splitlines()
        if line.strip().startswith("housefinder ") and len(line.split()) > 1
    }
    handled = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(")") and "|" in stripped or stripped.endswith(")"):
            head = stripped.rstrip(")").strip()
            if head and " " not in head and head not in {"fi", "esac", "*"}:
                handled.update(part.strip('"') for part in head.split("|"))
    missing = advertised - handled
    assert not missing, f"advertised but not handled: {sorted(missing)}"


def test_it_reads_json_with_the_app_s_own_python() -> None:
    """macOS ships no python3 of its own on a clean machine, and the app carries
    one. Reaching for a system interpreter would work on this laptop and fail on
    somebody else's."""
    text = CLI.read_text()
    assert 'PY="$APP_ROOT/current/bin/python"' in text
    assert "python3 -c" not in text


def test_checking_presents_the_local_origin() -> None:
    """The app refuses a POST that does not come from its own origin, which is
    what stops a page you happen to have open from driving your dashboard. The
    command is the local dashboard's origin, so it says so rather than having
    the guard relaxed for it -- found by running it and getting a 403.
    """
    text = CLI.read_text()

    assert '-H "Origin: http://127.0.0.1:$PORT"' in text


def test_a_refusal_is_read_rather_than_treated_as_a_failure() -> None:
    """curl -f exits before the reply can be looked at, and the reply is the
    point: "today's check is used up" and "finish your deal first" both arrive
    as redirects the app has already written for a person to read.
    """
    text = CLI.read_text()
    check = text[text.index("  check|scan)"):text.index("  logs)")]

    assert "-f" not in check.split("curl")[1].split("\n")[0], "-f would swallow the reason"
    assert "*message=*|*error=*)" in check, "only one of the two refusal shapes is read"


def test_the_help_names_the_port_it_is_actually_on() -> None:
    """It was a heredoc with quoted delimiter, so the address printed as
    127.0.0.1:8000 on an install that was not on 8000."""
    text = CLI.read_text()

    assert "cat <<USAGE" in text, "a quoted heredoc would print the variable name"
    assert "Your dashboard lives at $URL" in text
