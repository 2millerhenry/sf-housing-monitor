"""The page a stranger lands on when they follow the download link.

The repository's website field points at the latest release, so this page is
the front door for anybody who did not arrive through the README. It has to
lead with what the app is and how to get it, and it has to stay that way
between releases rather than being rewritten by hand each time and drifting.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTES = ROOT / "release_assets" / "RELEASE_NOTES.txt"


def current_version() -> str:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def page() -> str:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "release_page.py"), current_version()],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode:
        pytest.skip(f"release not built: {result.stderr.strip()}")
    return result.stdout


def test_it_leads_with_what_the_app_is_not_with_the_changelog() -> None:
    """Somebody arriving here has usually never seen the app. A version's own
    changes are the least interesting thing on the page to them."""
    text = page()
    first = text.index("Finding a place in San Francisco")
    changed = text.index("What changed in")

    assert first < text.index("## Install") < changed


def test_the_two_ways_back_in_are_both_named() -> None:
    """Somebody who installed with the one-line command has no shortcuts, so the
    command and the address both have to be written down somewhere they will
    see them."""
    text = page()

    assert "homefinder" in text
    assert "http://127.0.0.1:8000" in text
    assert "bookmark" in text


def test_the_changelog_and_checksum_are_folded_away() -> None:
    text = page()

    assert "<details>" in text
    assert text.index("<details>") < text.index("What changed in")


def test_the_changes_come_from_the_notes_file() -> None:
    """Retyping them onto the release page is how the two drift apart."""
    version = current_version()
    entry = NOTES.read_text().split(f"SF Home Finder {version}\n", 1)[1]
    sentence = " ".join(entry.strip().split("\n\n")[0].split())[:60]

    assert sentence in page()


def test_hard_wrapping_does_not_survive_into_the_page() -> None:
    """The notes file is wrapped for a terminal. GitHub honours those breaks
    literally, which turned a paragraph into a column of short lines."""
    text = page()
    body = text[text.index("What changed in"):]
    paragraphs = [p for p in body.split("\n\n") if p.strip() and not p.startswith(("```", "<", "#"))]

    assert paragraphs, "no prose found to check"
    assert max(len(p.splitlines()) for p in paragraphs) == 1, "a paragraph is still hard-wrapped"
