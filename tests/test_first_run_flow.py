"""The first-run flow a stranger actually walks through.

A fresh install must open blank, and saving the deal for the first time must
kick off the one-off backfill. Without that trigger the seven-day window in
``Scanner._within_initial_window`` would never run, so it is asserted here
rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.preferences import load_preferences
from sf_housing.settings import Settings


BLANK_PROFILE = """profile_version: 1
profile:
  state: draft
  enabled_paths: []
  budgets: {}
technical: {}
"""

MINIMAL_DEAL = {
    "housing_paths": "private_room",
    "private_room_maximum": "2000",
    "anywhere_in_sf": "on",
    "move_in_flexible": "on",
}


def blank_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(BLANK_PROFILE, encoding="utf-8")
    return Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )


def build(tmp_path: Path, recorder: list[str]):
    settings = blank_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    def record(trigger: str, *args, **kwargs):
        recorder.append(trigger)
        return True

    application.state.scanner.start_scan = record
    return application, settings


def test_a_fresh_install_opens_on_the_setup_page(tmp_path: Path) -> None:
    application, _ = build(tmp_path, [])

    with TestClient(application) as client:
        landing = client.get("/", follow_redirects=False)
        assert landing.status_code == 303
        assert "welcome=1" in landing.headers["location"]

        page = client.get("/", follow_redirects=True)

    assert page.status_code == 200
    # The author's own identity must never reach a stranger's screen. SF
    # neighbourhood names legitimately appear as empty dropdown options, so the
    # real check is that nothing arrives pre-chosen.
    for personal in ("Henry", "Evan"):
        assert personal not in page.text
    for area in ("Duboce Triangle", "Potrero Hill", "NOPA"):
        assert f'value="{area}" selected' not in page.text, f"{area} must not arrive pre-chosen"
    for budget in ("2700", "1500", "800", "2600"):
        assert f'value="{budget}"' not in page.text, "no budget may be pre-filled"


def test_saving_the_deal_the_first_time_starts_the_backfill(tmp_path: Path) -> None:
    started: list[str] = []
    application, settings = build(tmp_path, started)

    with TestClient(application) as client:
        response = client.post("/preferences/deal", data=MINIMAL_DEAL, follow_redirects=False)

    assert response.status_code == 303, response.text
    assert "scan=starting" in response.headers["location"]
    assert started == ["initial_discovery"], "the first save must trigger the backfill scan"
    assert load_preferences(settings.preferences_path).profile_active is True


def test_saving_again_reranks_instead_of_backfilling_a_second_time(tmp_path: Path) -> None:
    """The backfill is a one-off. Editing a deal later must not replay it."""
    started: list[str] = []
    application, _ = build(tmp_path, started)

    with TestClient(application) as client:
        client.post("/preferences/deal", data=MINIMAL_DEAL, follow_redirects=False)
        second = client.post(
            "/preferences/deal",
            data={**MINIMAL_DEAL, "private_room_maximum": "2200"},
            follow_redirects=False,
        )

    assert second.status_code == 303, second.text
    assert "scan=starting" not in second.headers["location"]
    assert started == ["initial_discovery"], "the backfill must run exactly once"


def test_an_incomplete_deal_is_refused_without_starting_a_scan(tmp_path: Path) -> None:
    started: list[str] = []
    application, settings = build(tmp_path, started)

    with TestClient(application) as client:
        response = client.post(
            "/preferences/deal",
            data={"anywhere_in_sf": "on", "move_in_flexible": "on"},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert started == []
    assert load_preferences(settings.preferences_path).profile_active is False
