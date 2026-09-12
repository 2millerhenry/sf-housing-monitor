from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WINDOWS = ROOT / "release_assets" / "windows"


def test_windows_release_has_normal_user_handoff_commands() -> None:
    expected = {
        "2 Install SF Home Finder.cmd",
        "3 Open SF Home Finder.cmd",
        "Repair SF Home Finder.cmd",
        "Verify SF Home Finder.cmd",
        "Uninstall SF Home Finder.cmd",
        "1 START HERE.txt",
        "RELEASE_NOTES.txt",
    }
    assert expected.issubset({path.name for path in WINDOWS.iterdir()})
    for name in expected:
        assert (WINDOWS / name).read_text(encoding="utf-8")


def test_windows_installer_verifies_bootstrap_and_preserves_private_data() -> None:
    installer = (WINDOWS / "payload" / "install.ps1").read_text(encoding="utf-8")
    assert "Get-FileHash" in installer
    assert "2E70ECD22196CBD9D14EEFB700814BCAFC5B75A0D8275B52E8402E5FE256D928" in installer
    assert "PROCESSOR_ARCHITECTURE -ne 'AMD64'" in installer
    assert "Invoke-Uv -Arguments" in installer
    assert "schtasks.exe /Create" in installer
    assert "SF_HOUSING_DATA_DIR" in (WINDOWS / "payload" / "tools" / "run-service.cmd").read_text(encoding="utf-8")
    assert "Remove-Item -LiteralPath $DataDir" not in installer
    assert "Start-Process \"http://127.0.0.1:$Port/\"" in installer


def test_windows_ready_check_and_uninstall_are_explicit_and_recoverable() -> None:
    tools = WINDOWS / "payload" / "tools"
    verify = (tools / "verify.ps1").read_text(encoding="utf-8")
    uninstall = (tools / "uninstall.ps1").read_text(encoding="utf-8")
    assert "support/report.json" in verify
    assert "Double-click Repair" in verify
    assert "Type DELETE" in uninstall
    assert "schtasks.exe /Delete" in uninstall


def test_windows_installer_clears_the_mark_of_the_web_once() -> None:
    """Files extracted from a downloaded ZIP carry a Mark-of-the-Web stream.

    Clearing it once stops PowerShell and SmartScreen challenging each script
    in turn. It edits no file content, so payload checksums still verify.
    """
    installer = (WINDOWS / "payload" / "install.ps1").read_text(encoding="utf-8")

    assert "Unblock-File" in installer
    assert "-Recurse -File" in installer
    assert "SilentlyContinue" in installer, "unblocking must never abort the install"
    assert installer.index("Unblock-File") < installer.index("Get-FileHash")


def test_the_windows_folder_reads_itself_top_to_bottom() -> None:
    """Explorer sorts the same way Finder does, and the same page was buried."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build_windows_release import COMMAND_FILES

    shown = sorted(
        [*COMMAND_FILES, "1 START HERE.txt", "RELEASE_NOTES.txt", "LICENSE.txt"]
    )

    assert shown[0] == "1 START HERE.txt", shown
    assert shown[1].startswith("2 Install"), shown
    assert shown[2].startswith("3 Open"), shown
    assert not any(name[0].isdigit() for name in shown if "Uninstall" in name)


def test_uninstalling_can_run_with_nobody_at_the_keyboard() -> None:
    """It asked whether to delete the data and waited for an answer forever.

    The macOS uninstaller has always coped with having no one to ask: its read
    falls back to keeping the data. This one blocked, which made it impossible
    to script, impossible to test, and impossible to run from a Windows install
    that had no console attached. Silence means keep, on both platforms.
    """
    script = (
        ROOT / "release_assets" / "windows" / "payload" / "tools" / "uninstall.ps1"
    ).read_text(encoding="utf-8")

    assert "[switch]$KeepData" in script, "there is no way to say so in advance"
    assert "catch { $confirmation = '' }" in script, (
        "a prompt with nobody to answer it still stops the uninstall"
    )
    # And the fallback is never the destructive one.
    assert "if ($confirmation -eq 'DELETE')" in script


def test_the_windows_installer_waits_as_long_as_the_mac_one_learned_to() -> None:
    """0.4.2 fixed this on macOS and nothing fixed it here. A first start on a
    full board spends about fifteen seconds re-ranking what is already stored,
    and the installer gave up at 45 seconds and sent people to Repair for an
    install that had worked."""
    script = (
        ROOT / "release_assets" / "windows" / "payload" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "function Wait-ForMonitor([int]$Seconds = 150)" in script


def test_installing_never_fails_because_a_browser_would_not_open() -> None:
    """Opening the dashboard is a courtesy at the very end, after everything
    that matters has already succeeded. On a machine with no browser
    association it threw, and an install that had completely worked reported
    failure."""
    script = (
        ROOT / "release_assets" / "windows" / "payload" / "install.ps1"
    ).read_text(encoding="utf-8")

    tail = script[script.index('Write-Host "Installed.'):]
    assert "try { Start-Process" in tail, "opening a browser can still fail the install"
    assert "$env:SF_HOUSING_NO_BROWSER -ne '1'" in tail, "no way to install without one"
