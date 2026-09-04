from __future__ import annotations

import json
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import scan_release, validate_gmail_client
from sf_housing.app import FURNISHED_FINDER_BRIDGE_VERSION


ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "furnished_finder_chrome_bridge"
PACKAGED_EXTENSION = ROOT / "sf_housing" / "static" / "furnished-finder-bridge.zip"


def test_packaged_chrome_bridge_matches_source_exactly() -> None:
    expected = {
        "manifest.json",
        "service-worker.js",
        "content.js",
        "popup.html",
        "popup.js",
        "popup.css",
    }
    with zipfile.ZipFile(PACKAGED_EXTENSION) as archive:
        assert set(archive.namelist()) == expected
        for name in expected:
            assert archive.read(name) == (EXTENSION / name).read_bytes()


def test_bridge_versions_cannot_drift() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == FURNISHED_FINDER_BRIDGE_VERSION
    assert F'const BRIDGE_VERSION = chrome.runtime.getManifest().version;' in (
        EXTENSION / "service-worker.js"
    ).read_text(encoding="utf-8")


def test_release_assets_are_generic_and_preserve_private_data_by_default() -> None:
    release_assets = ROOT / "release_assets"
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in release_assets.rglob("*")
        if path.is_file()
    )

    assert "/Users/henrymiller" not in text
    assert "Library/Application Support/SF Housing Monitor" in text
    assert "does not use sudo" in text
    assert "Your private data remains" in text
    assert 'if [ "$CONFIRMATION" = "DELETE" ]' in text
    assert "Verify SF Housing Monitor.command" in (release_assets / "START_HERE.txt").read_text()
    assert (release_assets / "Verify SF Housing Monitor.command").stat().st_mode & 0o100
    assert (release_assets / "payload" / "tools" / "doctor.sh").stat().st_mode & 0o100
    assert 'support/report.json' in (release_assets / "payload" / "tools" / "doctor.sh").read_text()
    assert "SF_HOUSING_NO_LAUNCH_AGENT" in (
        release_assets / "payload" / "tools" / "doctor.sh"
    ).read_text()
    assert "SF_HOUSING_NO_LAUNCH_AGENT" in (
        release_assets / "payload" / "tools" / "open.sh"
    ).read_text()
    assert '"ok": true' in (release_assets / "payload" / "tools" / "open.sh").read_text()
    for command in (
        "Open SF Housing Monitor.command",
        "Verify SF Housing Monitor.command",
        "Repair SF Housing Monitor.command",
        "Uninstall SF Housing Monitor.command",
    ):
        assert "SF_HOUSING_APP_ROOT" in (release_assets / command).read_text()


def test_release_wheel_never_packages_a_profile_or_mutable_data() -> None:
    """A release starts blank. The wheel may carry read-only reference data
    built from public records, and nothing that belongs to a person: no
    profile, no database, no log, no credential."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = tomllib.loads(text)

    assert 'include = ["sf_housing", "sf_housing.*"]' in text
    assert "config/preferences.yaml" not in text

    patterns = sorted(
        {glob for globs in project["tool"]["setuptools"]["package-data"].values() for glob in globs}
    )
    assert patterns == ["*.css", "*.html", "*.js", "*.zip", "data/*.json"]

    # The one shipped data directory holds exactly one file, and it is the
    # street table. Anything else appearing there would be packaged silently.
    assert sorted(p.name for p in (ROOT / "sf_housing" / "data").iterdir()) == ["sf_streets.json"]
    table = json.loads((ROOT / "sf_housing" / "data" / "sf_streets.json").read_text(encoding="utf-8"))
    assert table["source"].startswith("DataSF"), "the shipped table must be public city data"


def test_the_street_table_is_committed_and_not_swallowed_by_an_ignore_rule() -> None:
    """The wheel builds from the working tree, so a table that exists locally
    but is untracked would pass every other check here and still leave a fresh
    clone shipping no neighbourhoods at all."""
    table = ROOT / "sf_housing" / "data" / "sf_streets.json"
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(table.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
    )
    if result.returncode != 0 and b"not a git repository" in result.stderr.lower():
        pytest.skip("not a git checkout")
    assert result.returncode == 0, "sf_housing/data/sf_streets.json is not tracked by git"


def test_release_accepts_only_a_loopback_owner_gmail_client(tmp_path: Path) -> None:
    valid = tmp_path / "valid-client.json"
    valid.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "owner.apps.googleusercontent.com",
                    "client_secret": "owner-build-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    remote = tmp_path / "remote-client.json"
    remote.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "owner.apps.googleusercontent.com",
                    "client_secret": "owner-build-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["https://example.test/callback"],
                }
            }
        ),
        encoding="utf-8",
    )

    assert validate_gmail_client(valid)
    with pytest.raises(SystemExit, match="loopback"):
        validate_gmail_client(remote)


def test_release_privacy_scan_allows_only_explicit_client_injection_never_a_token(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    client = payload / "gmail-client-secret.json"
    client.write_text('{"installed":{"client_secret":"build-fixture"}}', encoding="utf-8")

    scan_release(tmp_path, gmail_injected=True)
    with pytest.raises(SystemExit, match="unexpected OAuth client material"):
        scan_release(tmp_path, gmail_injected=False)

    client.unlink()
    (payload / "gmail-token.json").write_text('{"refresh_token":"never"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="forbidden file"):
        scan_release(tmp_path, gmail_injected=True)


def test_macos_installer_clears_the_download_quarantine_once() -> None:
    """Without this every command in the release re-prompts Gatekeeper separately.

    The build is unsigned by design (notarization needs a paid Apple account),
    so the goal is one approval rather than five.
    """
    installer = (ROOT / "release_assets" / "payload" / "install.sh").read_text(encoding="utf-8")

    assert "xattr -dr com.apple.quarantine" in installer
    assert '"$RELEASE_ROOT"' in installer
    # It must never be able to fail the installation.
    quarantine_line = next(
        line for line in installer.splitlines() if "xattr -dr com.apple.quarantine" in line
    )
    assert quarantine_line.rstrip().endswith("|| true")
    # And it must run before the payload is verified and used.
    assert installer.index("xattr -dr") < installer.index("shasum -a 256 -c")


def test_both_releases_ship_the_license_next_to_the_installer() -> None:
    """An MIT project must hand the licence to whoever downloads the ZIP."""
    mac = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "build_windows_release.py").read_text(encoding="utf-8")

    assert (ROOT / "LICENSE").is_file()
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    for script in (mac, windows):
        assert 'shutil.copy2(ROOT / "LICENSE", release_root / "LICENSE.txt")' in script


def test_project_metadata_declares_the_license() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'license = "MIT"' in project
    assert 'license-files = ["LICENSE"]' in project
