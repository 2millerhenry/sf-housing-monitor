from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.furnished_finder_bridge import (
    FurnishedFinderBridgeError,
    cards_to_candidates,
    validated_search_url,
)
from sf_housing.preferences import parse_preferences
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


def bridge_card(**overrides: str) -> dict[str, str]:
    card = {
        "url": "https://www.furnishedfinder.com/property/381429_1",
        "title": "Sunny private room near Dolores Park",
        "price": "$1,650 /month",
        "listing_type": "Room - House",
        "summary": "Room - House. Mission Dolores, San Francisco, CA. Available Aug. 1. Laundry and patio.",
    }
    card.update(overrides)
    return card


def bridge_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    return Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )


class BlockingSource:
    platform = "Blocking source"
    mode = "automatic"
    search_url = "https://example.test/blocking"
    manual_reason = None
    detail_budget = 0

    def __init__(self, started: threading.Event, release: threading.Event):
        self.started = started
        self.release = release

    def search(self, client, preferences):
        self.started.set()
        assert self.release.wait(timeout=3)
        return []

    def enrich(self, client, listing):
        return listing


def test_bridge_keeps_private_room_and_explicit_three_bedroom_cards_and_leaves_unknowns_neutral() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)

    listings = cards_to_candidates(
        [
            bridge_card(),
            bridge_card(
                url="https://www.furnishedfinder.com/property/other",
                title="Entire apartment in Mission",
                listing_type="Apartment",
                summary="Apartment in Mission Dolores",
            ),
            bridge_card(
                url="https://www.furnishedfinder.com/property/roomy-whole-home",
                title="Roomy and well-located three-bedroom home",
                listing_type="Condo",
                summary="A whole condo with three bedrooms in Mission Dolores.",
            ),
        ],
        preferences,
    )

    assert len(listings) == 2
    room = next(listing for listing in listings if listing.housing_kind == "room")
    three_bedroom = next(listing for listing in listings if listing.unit_type == "three_bedroom")
    assert room.platform == "Furnished Finder"
    assert room.price == 1650
    assert room.neighborhood == "Mission"
    assert room.listing_type == "Private room · House"
    assert room.metadata["bridge"] == "furnished_finder_chrome"
    assert three_bedroom.housing_kind == "whole_unit"
    assert three_bedroom.neighborhood == "Mission"


def test_bridge_accepts_an_explicit_studio_without_calling_it_a_room() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)

    listing = cards_to_candidates(
        [
            bridge_card(
                url="https://www.furnishedfinder.com/property/studio",
                title="Sunny studio apartment near Dolores Park",
                listing_type="Studio - Apartment",
                price="$2,550 /month",
                summary="Studio - Apartment. Mission Dolores. 18-unit building.",
            )
        ],
        preferences,
    )[0]

    assert listing.housing_kind == "whole_unit"
    assert listing.unit_type == "studio"
    assert listing.building_units == 18
    assert listing.listing_type == "Studio - Apartment"


def test_bridge_accepts_an_explicit_two_bedroom_for_the_split_search() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)

    listing = cards_to_candidates(
        [
            bridge_card(
                url="https://www.furnishedfinder.com/property/two-bedroom",
                title="Furnished 2 bedroom in Potrero Hill",
                listing_type="2 Bedroom - Apartment",
                price="$5,200 /month",
                summary="2 Bedroom - Apartment. Potrero Hill. 20-unit building.",
            )
        ],
        preferences,
    )[0]

    assert listing.housing_kind == "whole_unit"
    assert listing.unit_type == "two_bedroom"
    assert listing.building_units == 20


def test_bridge_accepts_an_explicit_three_bedroom_for_the_split_search() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)

    listing = cards_to_candidates(
        [
            bridge_card(
                url="https://www.furnishedfinder.com/property/three-bedroom",
                title="Furnished 3 bedroom in Potrero Hill",
                listing_type="3 Bedroom - Apartment",
                price="$7,350 /month",
                summary="3 Bedroom - Apartment. Potrero Hill. 18-unit building.",
            )
        ],
        preferences,
    )[0]

    assert listing.housing_kind == "whole_unit"
    assert listing.unit_type == "three_bedroom"
    assert listing.building_units == 18


def test_bridge_keeps_an_explicit_non_target_area_without_guessing_from_a_city_card() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)
    listing = cards_to_candidates(
        [
            bridge_card(
                title="Beautiful Victorian furnished rooms in Hayes Valley",
                summary="Room - Apartment. Hayes Valley, San Francisco, CA. Available: Aug. 1, 2026.",
            )
        ],
        preferences,
    )[0]

    assert listing.neighborhood == "Hayes Valley"


def test_bridge_uses_an_unambiguous_furnished_finder_map_pin_for_a_target_area() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)
    listing = cards_to_candidates(
        [
            bridge_card(
                title="Private room in a house",
                summary="Room - House. San Francisco, CA. Available: Aug. 1, 2026.",
                latitude="37.75780",
                longitude="-122.40093",
            )
        ],
        preferences,
    )[0]

    assert listing.neighborhood == "Potrero Hill"
    assert listing.metadata["furnished_finder_location_evidence"] == "Map pin"


def test_bridge_validates_furnished_finder_urls_and_safely_caps_a_large_card_batch() -> None:
    assert validated_search_url("https://www.furnishedfinder.com/housing/us--ca--san-francisco?budget=2300").endswith(
        "?budget=2300"
    )
    try:
        validated_search_url("https://example.test/housing/us--ca--san-francisco")
    except FurnishedFinderBridgeError as exc:
        assert "Furnished Finder" in str(exc)
    else:  # pragma: no cover - makes the intended failure clear if validation regresses
        raise AssertionError("An off-site search URL must be rejected")

    capped = cards_to_candidates(
        [bridge_card(url=f"https://www.furnishedfinder.com/property/{item}") for item in range(181)],
        parse_preferences(TEST_PREFERENCES),
    )
    assert len(capped) == 180


def test_bridge_import_uses_normal_scoring_deduplication_and_source_health(tmp_path: Path) -> None:
    application = create_app(settings=bridge_settings(tmp_path), sources=[], enable_scheduler=False)
    payload = {
        "search_url": "https://www.furnishedfinder.com/housing/us--ca--san-francisco",
        "cards": [bridge_card()],
    }

    with TestClient(application) as client:
        first = client.post("/integrations/furnished-finder/import", json=payload)
        second = client.post("/integrations/furnished-finder/import", json=payload)

    assert first.status_code == 200
    assert first.json() == {"seen": 1, "added": 1, "updated": 0, "status": "completed"}
    assert second.status_code == 200
    assert second.json()["added"] == 0
    assert second.json()["updated"] == 1

    repository = application.state.repository
    listings = repository.query_listings(0, view="all")
    assert len(listings) == 1
    assert listings[0]["score"] >= 60
    assert any(
        item["platform"] == "Furnished Finder" and item["status"] == "success"
        for item in repository.latest_source_runs()
    )


def test_bridge_import_rejects_empty_or_non_room_payload(tmp_path: Path) -> None:
    application = create_app(settings=bridge_settings(tmp_path), sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        response = client.post(
            "/integrations/furnished-finder/import",
            json={
                "search_url": "https://www.furnishedfinder.com/housing/us--ca--san-francisco",
                "cards": [
                    bridge_card(
                        listing_type="Apartment",
                        title="Entire apartment in Mission",
                        summary="Apartment in Mission Dolores",
                    )
                ],
            },
        )

    assert response.status_code == 422
    assert "No supported private-room, studio, one-bedroom, two-bedroom, or three-bedroom cards" in response.json()["detail"]


def test_bridge_import_returns_a_retryable_conflict_while_normal_scan_is_running(tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()
    application = create_app(
        settings=bridge_settings(tmp_path),
        sources=[BlockingSource(started, release)],
        enable_scheduler=False,
    )
    payload = {
        "search_url": "https://www.furnishedfinder.com/housing/us--ca--san-francisco",
        "cards": [bridge_card()],
    }

    with TestClient(application) as client:
        assert client.post("/scan", follow_redirects=False).status_code == 303
        assert started.wait(timeout=2)
        response = client.post("/integrations/furnished-finder/import", json=payload)
        release.set()

    assert response.status_code == 409
    assert "regular scan is already running" in response.json()["detail"]
