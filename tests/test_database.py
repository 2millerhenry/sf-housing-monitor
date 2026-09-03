from __future__ import annotations

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult


def result(score: int = 75) -> ScoreResult:
    return ScoreResult(score, ["Good price"], "Unknown: lease terms.", {"price": {"known": True}})


def test_storage_deduplicates_repeated_urls_and_preserves_review_state(repository: Repository) -> None:
    first = ListingCandidate(
        platform="SpareRoom",
        source_id="100",
        title="Private room",
        original_url="https://example.test/room/100?listing_click=1",
        price=1500,
        neighborhood="Mission",
    )
    listing_id, created = repository.upsert_listing(first, result(), "2026-07-19T12:00:00+00:00")
    assert created is True
    assert repository.set_listing_status(listing_id, "saved")
    assert repository.set_listing_note(listing_id, "Contact tomorrow")

    repeated = ListingCandidate(
        platform="SpareRoom",
        source_id="changed-id",
        title="Private room — updated",
        original_url="https://example.test/room/100?utm_source=email",
        price=1450,
        neighborhood="Mission",
    )
    repeated_id, created_again = repository.upsert_listing(
        repeated, result(82), "2026-07-20T12:00:00+00:00"
    )

    stored = repository.query_listings(minimum_score=0, view="all")
    assert repeated_id == listing_id
    assert created_again is False
    assert len(stored) == 1
    assert stored[0]["first_found"] == "2026-07-19T12:00:00+00:00"
    assert stored[0]["last_seen"] == "2026-07-20T12:00:00+00:00"
    assert stored[0]["status"] == "saved"
    assert stored[0]["note"] == "Contact tomorrow"
    assert stored[0]["price"] == 1450


def test_database_status_and_note_validation(repository: Repository) -> None:
    listing_id, _ = repository.upsert_listing(
        ListingCandidate("Test", "1", "Room", "https://example.test/1"), result()
    )
    assert repository.set_listing_status(listing_id, "dismissed")
    assert repository.set_listing_note(listing_id, "Pass")
    assert repository.query_listings(0, view="active") == []
    assert repository.query_listings(0, view="dismissed")[0]["note"] == "Pass"


def test_database_tracks_when_a_listing_is_opened_without_changing_its_status(repository: Repository) -> None:
    listing_id, _ = repository.upsert_listing(
        ListingCandidate("Test", "opened", "Room", "https://example.test/opened"), result()
    )
    assert repository.set_listing_status(listing_id, "saved")
    assert repository.mark_listing_opened(listing_id)

    stored = repository.query_listings(0, view="saved")[0]
    assert stored["status"] == "saved"
    assert stored["opened_at"] is not None
    assert repository.open_listing_url(listing_id) == "https://example.test/opened"


def test_database_can_sort_unopened_listings_before_opened_ones(repository: Repository) -> None:
    unopened_id, _ = repository.upsert_listing(
        ListingCandidate("Test", "unopened", "Untouched room", "https://example.test/unopened"),
        result(score=70),
    )
    opened_id, _ = repository.upsert_listing(
        ListingCandidate("Test", "opened-first", "Opened room", "https://example.test/opened-first"),
        result(score=90),
    )
    assert repository.mark_listing_opened(opened_id)

    listings = repository.query_listings(0, view="all", sort="unopened")

    assert [listing["id"] for listing in listings] == [unopened_id, opened_id]


def test_database_can_filter_home_style_and_sort_by_explicit_move_in_date(repository: Repository) -> None:
    early = ListingCandidate(
        "Furnished Finder",
        "early-house",
        "Private room in house",
        "https://example.test/early-house",
        listing_type="Private room · House",
    )
    unknown = ListingCandidate(
        "Furnished Finder",
        "unknown-flat",
        "Private room in apartment",
        "https://example.test/unknown-flat",
        listing_type="Private room · Apartment",
    )
    late = ListingCandidate(
        "Furnished Finder",
        "late-house",
        "Private room in house",
        "https://example.test/late-house",
        listing_type="Private room · House",
    )
    early_result = ScoreResult(
        75,
        [],
        "Unknown: lease.",
        {"availability": {"available_on": "2026-08-01"}, "home_facts": {"primary": "Private room · House"}},
    )
    late_result = ScoreResult(
        80,
        [],
        "Unknown: lease.",
        {"availability": {"available_on": "2026-08-20"}, "home_facts": {"primary": "Private room · House"}},
    )
    unknown_result = ScoreResult(
        90,
        [],
        "Unknown: lease.",
        {"home_facts": {"primary": "Private room · Shared flat"}},
    )
    repository.upsert_listing(early, early_result)
    repository.upsert_listing(unknown, unknown_result)
    repository.upsert_listing(late, late_result)

    soonest = repository.query_listings(0, view="all", sort="available")
    houses = repository.query_listings(0, view="all", home_style="house")

    assert [row["source_id"] for row in soonest] == ["early-house", "late-house", "unknown-flat"]
    assert [row["source_id"] for row in houses] == ["late-house", "early-house"]
    assert soonest[0]["available_on"] == "2026-08-01"
    assert soonest[0]["home_facts"]["primary"] == "Private room · House"
