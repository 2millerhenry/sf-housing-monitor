from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.deal_profile import (
    DealProfileError,
    deal_profile_from_form,
)
from sf_housing.models import ListingCandidate
from sf_housing.preferences import ensure_preferences, load_preferences, parse_preferences
from sf_housing.scoring import score_listing
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


def settings_for(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )


def valid_form() -> dict[str, object]:
    return {
        "housing_paths": ["private_room", "studio", "two_bedroom"],
        "private_room_maximum": "2400",
        "private_room_ideal": "2100",
        "studio_maximum": "2800",
        "studio_ideal": "2600",
        "studio_building_units": "50",
        "two_bedroom_maximum": "2500",
        "two_bedroom_occupants": "2",
        "two_bedroom_building_units": "50",
        "areas_dream": ["Noe Valley"],
        "areas_strong": ["Mission Dolores"],
        "areas_okay": ["Bernal Heights"],
        "move_in_flexible": "1",
        "lease_min_months": "6",
        "lease_max_months": "12",
        "household_maximum": "5",
        "preference_natural_light": "important",
        "preference_laundry": "nice",
        "preference_outdoor_space": "nice",
        "preference_furnished": "ignore",
        "preference_pets": "ignore",
        "preference_parking": "ignore",
        "preference_household_feel": "important",
    }


class MultiForm(dict):
    def getlist(self, name: str) -> list[object]:
        value = self.get(name, [])
        return list(value) if isinstance(value, list) else [value]


def test_missing_profile_becomes_neutral_draft_without_personal_answers(tmp_path: Path) -> None:
    path = tmp_path / "preferences.yaml"
    preferences = ensure_preferences(path)

    assert path.exists()
    assert preferences.profile_active is False
    assert preferences.deal_profile.enabled_paths == ()
    text = path.read_text(encoding="utf-8")
    assert "profile_version: 1" in text
    assert "Henry" not in text
    assert "henrymiller" not in text


def test_legacy_profile_migrates_in_memory_without_changing_the_file() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)

    assert preferences.profile_active is True
    assert preferences.canonical is None
    assert preferences.deal_profile.budgets["private_room"].maximum_monthly == 2000
    assert preferences.deal_profile.areas["strong"] == ("NOPA", "Inner Richmond", "Mission")


def test_canonical_deal_prevents_cross_tier_area_duplicates() -> None:
    form = valid_form()
    form["areas_okay"] = ["Noe Valley"]

    try:
        deal_profile_from_form(MultiForm(form))
    except DealProfileError as exc:
        assert "appears in both" in str(exc)
    else:
        raise AssertionError("A duplicate area tier must not activate")


def test_split_summary_uses_one_authoritative_per_person_pair() -> None:
    profile = deal_profile_from_form(MultiForm(valid_form()))

    assert profile.budgets["two_bedroom"].total_maximum == 5000
    assert "$5,000 total ($2,500 each for 2)" in profile.summary()


def test_blank_app_routes_to_onboarding_and_does_not_record_manual_scan(tmp_path: Path) -> None:
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        home = client.get("/", follow_redirects=False)
        scan = client.post("/scan", follow_redirects=False)
        onboarding = client.get("/preferences")

    assert home.status_code == 303
    assert home.headers["location"] == "/preferences?welcome=1"
    assert scan.status_code == 303
    assert "Finish+Your+deal" in scan.headers["location"]
    assert "Build the search once" in onboarding.text
    assert application.state.repository.recent_scans() == []


def test_blank_onboarding_uses_bounded_neighborhood_dropdowns(tmp_path: Path) -> None:
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        onboarding = client.get("/preferences?welcome=1")

    assert onboarding.status_code == 200
    assert onboarding.text.count('data-area-limit="5"') == 4
    assert onboarding.text.count('data-area-add=') == 4
    assert "Add a neighborhood" in onboarding.text
    assert "Hold Command" not in onboarding.text
    assert "Outer Sunset" in onboarding.text
    assert "Visitacion Valley" in onboarding.text
    assert onboarding.text.count('class="path-row"') == 6, "five sizes plus the four-bedroom split"
    # The five home types share one set of column headers instead of repeating
    # a budget label inside every card.
    assert onboarding.text.count("Monthly maximum") == 1
    assert onboarding.text.count("Sharing with") == 1
    assert onboarding.text.count('data-path-card=') == 6
    assert "Tick a home type to use its budget" in onboarding.text
    script = (Path(__file__).resolve().parent.parent / "sf_housing" / "static" / "deal-form.js").read_text()
    assert "Saving deal and starting your first check" in script
    assert 'classList.toggle("is-selected", toggle.checked)' in script


def test_real_form_activation_persists_canonical_profile_and_starts_first_discovery(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post("/preferences/deal", data=valid_form(), follow_redirects=False)

    saved = load_preferences(settings.preferences_path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?message=Your+deal+is+ready")
    assert "scan=starting" in response.headers["location"]
    assert saved.profile_active is True
    assert saved.canonical is not None
    assert saved.deal_profile.budgets["studio"].maximum_monthly == 2800
    assert saved.section("two_bedroom")["max_per_person"] == 2500


def test_scoring_separates_fit_confidence_and_hard_eligibility() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)
    sparse = ListingCandidate(
        "Example",
        "sparse",
        "Private room in Mission",
        "https://example.test/sparse",
        price=1500,
        neighborhood="Mission",
        summary="Private room available.",
    )
    outside = ListingCandidate(
        "Example",
        "outside",
        "Private room in Oakland",
        "https://example.test/outside",
        price=1500,
        neighborhood="Oakland",
        summary="Private room available in Oakland.",
    )

    sparse_result = score_listing(sparse, preferences)
    outside_result = score_listing(outside, preferences)

    assert sparse_result.score > outside_result.score
    assert sparse_result.confidence < 100
    assert sparse_result.eligibility == "needs_verification"
    assert outside_result.eligibility == "ineligible"
    assert outside_result.eligibility_reasons


def test_partial_onboarding_draft_survives_a_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    partial = {
        "housing_paths": "studio",
        "areas_dream": "Noe Valley",
        "move_in_flexible": "1",
    }

    with TestClient(application) as client:
        saved = client.post("/preferences/draft", data=partial)

    restarted = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(restarted) as client:
        page = client.get("/preferences")

    draft = load_preferences(settings.preferences_path).deal_profile
    assert saved.status_code == 200
    assert draft.active is False
    assert draft.enabled_paths == ("studio",)
    assert draft.budgets == {}
    assert 'value="studio" data-path-toggle="studio" checked' in page.text


def test_anywhere_in_sf_is_a_real_area_rule_and_disabled_paths_fail() -> None:
    form = valid_form()
    form["housing_paths"] = ["studio"]
    form["anywhere_in_sf"] = "1"
    form["areas_dream"] = []
    form["areas_strong"] = []
    form["areas_okay"] = []
    profile = deal_profile_from_form(MultiForm(form))
    preferences = parse_preferences(
        yaml.safe_dump(
            {"profile_version": 1, "profile": profile.to_dict(), "technical": {}},
            sort_keys=False,
        )
    )
    studio = ListingCandidate(
        "Example", "studio", "Studio in the Castro", "https://example.test/studio",
        price=2500, neighborhood="Castro", summary="Sunny studio apartment in San Francisco.",
    )
    room = ListingCandidate(
        "Example", "room", "Private room in the Castro", "https://example.test/room",
        price=1500, neighborhood="Castro", summary="Private room available in San Francisco.",
    )

    studio_result = score_listing(studio, preferences)
    room_result = score_listing(room, preferences)

    assert studio_result.details["neighborhood"]["value"] == 1.0
    assert studio_result.eligibility != "ineligible"
    assert room_result.eligibility == "ineligible"
    assert "not enabled" in room_result.concern
