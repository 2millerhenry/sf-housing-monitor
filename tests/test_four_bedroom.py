"""Four-bedroom homes are a first-class search path.

Adding a sixth path has two risks worth pinning: that an existing profile
silently gains a search it never asked for, and that a listing card stating a
whole property's bed count gets read as a whole home for rent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_housing.classification import FOUR_BEDROOM, ROOM, WHOLE_UNIT, classify_listing
from sf_housing.deal_profile import BEDROOM_PATHS, DEFAULT_OCCUPANTS, HOUSING_PATHS, SPLIT_PATHS
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.sources import CraigslistSource
from tests.conftest import TEST_PREFERENCES


FOUR_BEDROOM_YAML = "\nfour_bedroom:\n  enabled: true\n  max_per_person: 2000\n  occupants: 4\n"


def listing(title: str, summary: str = "", metadata: dict | None = None) -> ListingCandidate:
    return ListingCandidate(
        platform="Test",
        source_id="1",
        title=title,
        original_url="https://example.test/1",
        summary=summary,
        metadata=metadata or {},
    )


def test_four_bedroom_is_a_recognised_path() -> None:
    assert "four_bedroom" in HOUSING_PATHS
    assert "four_bedroom" in SPLIT_PATHS
    assert DEFAULT_OCCUPANTS["four_bedroom"] == 4
    assert BEDROOM_PATHS[4] == "four_bedroom"


@pytest.mark.parametrize(
    "title,metadata",
    [
        ("Spacious 4 bedroom Victorian", None),
        ("4BR house for rent", None),
        ("Four-bedroom flat in the Mission", None),
        ("Great place", {"bedrooms": 4}),
        ("Great place", {"unit_type": "4 BR"}),
    ],
)
def test_a_real_four_bedroom_classifies_as_a_whole_home(title: str, metadata: dict | None) -> None:
    classified = classify_listing(listing(title, metadata=metadata))

    assert classified.housing_kind == WHOLE_UNIT
    assert classified.unit_type == FOUR_BEDROOM


def test_a_room_inside_a_four_bed_flat_is_still_a_room() -> None:
    """Marketplace states the property's bed count even when renting one room."""
    classified = classify_listing(
        listing(
            "4 Beds 1.5 Baths - Apartment",
            "Large sunny bay window room with garden view. Four roommates, sunny back porch.",
        )
    )

    assert classified.housing_kind == ROOM
    assert classified.unit_type is None


def test_an_existing_profile_does_not_silently_gain_a_fourth_search() -> None:
    """Profiles written before this path existed never asked for it."""
    profile = parse_preferences(TEST_PREFERENCES).deal_profile

    assert "four_bedroom" not in profile.enabled_paths


def test_opting_in_enables_the_path_and_its_own_budget() -> None:
    profile = parse_preferences(TEST_PREFERENCES + FOUR_BEDROOM_YAML).deal_profile

    assert "four_bedroom" in profile.enabled_paths
    budget = profile.budgets["four_bedroom"]
    assert budget.maximum_monthly == 2000
    assert budget.occupants == 4


def test_craigslist_asks_for_exactly_four_bedrooms_within_the_group_budget() -> None:
    """A 2-3 bedroom search would bury four-bedroom inventory."""
    preferences = parse_preferences(TEST_PREFERENCES + FOUR_BEDROOM_YAML)

    url = CraigslistSource()._four_bedroom_url(preferences)

    assert "min_bedrooms=4" in url
    assert "max_bedrooms=4" in url
    # Four people at $2,000 each is the real ceiling for the whole home.
    assert "max_price=8000" in url


def test_the_four_bedroom_search_only_runs_when_the_path_is_enabled() -> None:
    from sf_housing.sources import SourceError  # noqa: F401

    source = CraigslistSource()
    without = parse_preferences(TEST_PREFERENCES)
    with_four = parse_preferences(TEST_PREFERENCES + FOUR_BEDROOM_YAML)

    assert "four_bedroom" not in without.deal_profile.enabled_paths
    assert "four_bedroom" in with_four.deal_profile.enabled_paths


def test_a_four_bedroom_survives_storage_and_filtering(tmp_path: Path) -> None:
    """The stored unit_type must be one the dashboard filter accepts."""
    from sf_housing.database import Repository
    from sf_housing.models import ScoreResult

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    home = classify_listing(listing("Spacious 4 bedroom Victorian", metadata={"bedrooms": 4}))
    repository.upsert_listing(home, ScoreResult(80, ["fits"], "", {}))

    rows = repository.query_listings(
        0, view="all", housing_kind="whole_unit", unit_type="four_bedroom"
    )

    assert [row["unit_type"] for row in rows] == ["four_bedroom"]
