from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WINDOWS = ROOT / "release_assets" / "windows"


def test_windows_release_has_normal_user_handoff_commands() -> None:
    expected = {
        "Install SF Housing Monitor.cmd",
        "Open SF Housing Monitor.cmd",
        "Repair SF Housing Monitor.cmd",
        "Verify SF Housing Monitor.cmd",
        "Uninstall SF Housing Monitor.cmd",
        "START_HERE.txt",
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
