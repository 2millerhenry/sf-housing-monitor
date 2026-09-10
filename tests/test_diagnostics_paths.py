"""Where the login-service check looks when the environment names no directory.

``SF_HOUSING_LAUNCH_AGENTS_DIR`` set but empty used to mean "here":
``os.environ.get`` falls back only when a variable is *absent*, and ``Path("")``
is ``.``, so ``com.sfhousing.monitor.plist`` stayed relative and ``is_file()``
was answered against whatever directory the process happened to start in --
``/`` under launchd. The check then reported the login service missing on a Mac
where it was installed and running, and sent that person to Repair to fix a
problem they did not have. A diagnostic that is confidently wrong costs more
than one that says nothing.

``install.sh`` has always read the same variable as
``${SF_HOUSING_LAUNCH_AGENTS_DIR:-$DEFAULT_LAUNCH_AGENTS_DIR}``, and the shell's
``:-`` falls back on empty as readily as on absent. These pin the Python side to
the installer's meaning.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from sf_housing.diagnostics import _launch_agent_check
from sf_housing.settings import Settings


BLANKS = ["", " ", "\t", "\n", "   \t\n "]


def settings_for(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )


def install_runtime(settings: Settings) -> Path:
    """Make the check believe it is looking at the installed friend release."""
    runtime = settings.data_dir.parent / "current" / "bin" / "python"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("runtime", encoding="utf-8")
    return runtime


def write_plist(directory: Path, runtime: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    plist = directory / "com.sfhousing.monitor.plist"
    payload = {
        "Label": "com.sfhousing.monitor",
        "ProgramArguments": [
            str(runtime),
            "-m",
            "sf_housing",
            "serve",
            "--host",
            "127.0.0.1",
        ],
    }
    with plist.open("wb") as handle:
        plistlib.dump(payload, handle)
    return plist


@pytest.fixture
def elsewhere(monkeypatch, tmp_path: Path) -> Path:
    """Run from a directory that is not the default, so "here" is visible."""
    working = tmp_path / "somewhere-else"
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.delenv("SF_HOUSING_LAUNCH_AGENTS_DIR", raising=False)
    # Own the default location rather than reading the developer's real one.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # The generated file is what is under test; loading it into a real login
    # session is not.
    monkeypatch.setenv("SF_HOUSING_NO_LAUNCH_AGENT", "1")
    return working.resolve()


@pytest.fixture
def installed(tmp_path: Path, elsewhere: Path) -> Settings:
    settings = settings_for(tmp_path)
    install_runtime(settings)
    return settings


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_launch_agents_dir_still_finds_the_installed_login_service(
    monkeypatch, installed: Settings, blank: str
) -> None:
    write_plist(Path.home() / "Library" / "LaunchAgents", install_runtime(installed))
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", blank)

    check = _launch_agent_check(installed)

    assert check.status == "unknown"
    assert "missing" not in check.label.casefold()


@pytest.mark.parametrize("blank", BLANKS)
def test_a_stray_plist_in_the_working_directory_is_not_the_login_service(
    monkeypatch, installed: Settings, elsewhere: Path, blank: str
) -> None:
    """Reading "here" would trust a file that never came from the installer."""
    write_plist(elsewhere, install_runtime(installed))
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", blank)

    check = _launch_agent_check(installed)

    assert check.status == "blocked"
    assert check.label == "Login service is missing"


def test_an_absent_variable_still_looks_in_the_login_services_directory(
    installed: Settings,
) -> None:
    write_plist(Path.home() / "Library" / "LaunchAgents", install_runtime(installed))

    assert _launch_agent_check(installed).status == "unknown"


def test_a_real_value_is_still_honoured(
    monkeypatch, installed: Settings, tmp_path: Path
) -> None:
    """The fallback must not swallow the directory the installer actually chose."""
    agents = tmp_path / "chosen agents"
    write_plist(agents, install_runtime(installed))
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", str(agents))

    assert _launch_agent_check(installed).status == "unknown"


def test_a_padded_value_names_the_directory_it_meant_to(
    monkeypatch, installed: Settings, tmp_path: Path
) -> None:
    """A value that arrives padded from a plist or install.sh is still a location."""
    agents = tmp_path / "chosen agents"
    write_plist(agents, install_runtime(installed))
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", f"  {agents}\n")

    assert _launch_agent_check(installed).status == "unknown"


def test_a_tilde_value_is_still_expanded(monkeypatch, installed: Settings) -> None:
    agents = Path.home() / "Library" / "LaunchAgents"
    write_plist(agents, install_runtime(installed))
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", "~/Library/LaunchAgents")

    assert _launch_agent_check(installed).status == "unknown"


def test_a_relative_value_is_still_honoured_against_the_working_directory(
    monkeypatch, installed: Settings, elsewhere: Path
) -> None:
    """Asking for a relative directory is a choice; only silence is not."""
    write_plist(elsewhere / "agents", install_runtime(installed))
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", "agents")

    assert _launch_agent_check(installed).status == "unknown"


def test_a_symlinked_launch_agents_directory_still_finds_the_plist(
    monkeypatch, installed: Settings, tmp_path: Path
) -> None:
    """Resolving the directory must not lose a home that reaches it by symlink."""
    real = tmp_path / "real agents"
    alias = tmp_path / "alias agents"
    write_plist(real, install_runtime(installed))
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", str(alias))

    assert _launch_agent_check(installed).status == "unknown"
