"""Where the app writes when the environment names no location.

``SF_HOUSING_DATA_DIR`` set but empty used to mean "here": ``os.environ.get``
falls back only when a variable is absent, and ``Path("").resolve()`` is the
working directory. A launcher or shell profile that exported the variable
empty would scatter ``preferences.yaml``, ``housing.sqlite3``, ``scan.lock``
and ``sf_housing.log`` wherever the process started -- ``/`` under launchd --
where nobody would ever find them. Both variables the settings read are
pinned here, because both had the same shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_housing.settings import PROJECT_ROOT, Settings


DEFAULT_DATA_DIR = (PROJECT_ROOT / "data").resolve()


@pytest.fixture
def elsewhere(monkeypatch, tmp_path: Path) -> Path:
    """Run from a directory that is not the default, so "here" is visible."""
    working = tmp_path / "somewhere-else"
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.delenv("SF_HOUSING_DATA_DIR", raising=False)
    monkeypatch.delenv("SF_HOUSING_PREFERENCES", raising=False)
    return working.resolve()


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "   \t\n "])
def test_a_blank_data_dir_falls_back_to_the_default_not_the_working_directory(
    monkeypatch, elsewhere: Path, blank: str
) -> None:
    monkeypatch.setenv("SF_HOUSING_DATA_DIR", blank)

    settings = Settings.from_environment()

    assert settings.data_dir == DEFAULT_DATA_DIR
    assert settings.data_dir != elsewhere
    for path in (settings.database_path, settings.log_path, settings.preferences_path):
        assert elsewhere not in path.parents


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "   \t\n "])
def test_a_blank_preferences_path_falls_back_under_the_data_dir(
    monkeypatch, elsewhere: Path, tmp_path: Path, blank: str
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SF_HOUSING_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SF_HOUSING_PREFERENCES", blank)

    settings = Settings.from_environment()

    assert settings.preferences_path == (data_dir / "config" / "preferences.yaml").resolve()
    assert elsewhere not in settings.preferences_path.parents


def test_an_absent_variable_still_uses_the_default(elsewhere: Path) -> None:
    settings = Settings.from_environment()

    assert settings.data_dir == DEFAULT_DATA_DIR
    assert settings.preferences_path == (DEFAULT_DATA_DIR / "config" / "preferences.yaml")


def test_a_real_value_is_still_honoured(monkeypatch, elsewhere: Path, tmp_path: Path) -> None:
    """The fallback must not swallow the variable people actually rely on."""
    chosen = tmp_path / "chosen"
    preferences = tmp_path / "elsewhere" / "preferences.yaml"
    monkeypatch.setenv("SF_HOUSING_DATA_DIR", str(chosen))
    monkeypatch.setenv("SF_HOUSING_PREFERENCES", str(preferences))

    settings = Settings.from_environment()

    assert settings.data_dir == chosen.resolve()
    assert settings.database_path == chosen.resolve() / "housing.sqlite3"
    assert settings.preferences_path == preferences.resolve()


def test_a_padded_value_names_the_directory_it_meant_to(
    monkeypatch, elsewhere: Path, tmp_path: Path
) -> None:
    """A value that arrives padded from a plist or .env file is still a location."""
    chosen = tmp_path / "chosen"
    monkeypatch.setenv("SF_HOUSING_DATA_DIR", f"  {chosen}\n")

    assert Settings.from_environment().data_dir == chosen.resolve()


def test_a_relative_value_is_still_resolved_against_the_working_directory(
    monkeypatch, elsewhere: Path
) -> None:
    """Asking for a relative directory is a choice; only silence is not."""
    monkeypatch.setenv("SF_HOUSING_DATA_DIR", "listings")

    assert Settings.from_environment().data_dir == elsewhere / "listings"


def test_a_tilde_value_is_still_expanded(monkeypatch, elsewhere: Path) -> None:
    monkeypatch.setenv("SF_HOUSING_DATA_DIR", "~/sf-housing-data")

    assert Settings.from_environment().data_dir == (Path.home() / "sf-housing-data").resolve()

