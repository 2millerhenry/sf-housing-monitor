"""The "show the extension folder" button must not 500 on a non-macOS machine."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sf_housing import app as app_module


@pytest.fixture
def recorded(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    return calls


def test_macos_uses_open_and_ignores_a_non_zero_exit(monkeypatch, recorded, tmp_path: Path) -> None:
    monkeypatch.setattr(app_module.os, "name", "posix")

    assert app_module._reveal_folder(tmp_path) is True
    assert recorded == [["/usr/bin/open", str(tmp_path)]]


def test_windows_uses_explorer(monkeypatch, recorded, tmp_path: Path) -> None:
    monkeypatch.setattr(app_module.os, "name", "nt")

    assert app_module._reveal_folder(tmp_path) is True
    assert recorded == [["explorer", str(tmp_path)]]


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError("no such binary"), subprocess.TimeoutExpired("open", 5)],
)
def test_a_missing_or_hung_file_manager_is_reported_not_raised(
    monkeypatch, tmp_path: Path, failure: Exception
) -> None:
    def fake_run(command, **kwargs):
        raise failure

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    assert app_module._reveal_folder(tmp_path) is False
