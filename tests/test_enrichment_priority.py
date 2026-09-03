"""The detail budget must be spent on homes the user will actually see.

Craigslist publishes a posting time only on the detail page, and the scanner
fetches a bounded number of those per search. Spending them in arrival order
meant the first ten results consumed the budget regardless of quality, so most
listings on the shortlist could not say how old they were.
"""

from __future__ import annotations

import pathlib

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import parse_preferences
from sf_housing.scanner import Scanner
from tests.conftest import TEST_PREFERENCES


def room(source_id: str, price: int, area: str = "NOPA", summary: str = "") -> ListingCandidate:
    return ListingCandidate(
        platform="Craigslist",
        source_id=source_id,
        title=f"Private room {source_id}",
        original_url=f"https://sfbay.craigslist.org/x/{source_id}.html",
        price=price,
        neighborhood=area,
        listing_type="Room/share",
        summary=summary,
    )


class RecordingSource:
    """A source that records exactly which listings were enriched."""

    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/search"
    manual_reason = None

    def __init__(self, listings, budget: int, fail_on: set[str] | None = None):
        self._listings = listings
        self.detail_budget = budget
        self.enriched: list[str] = []
        self._fail_on = fail_on or set()

    def search(self, client, preferences):
        return list(self._listings)

    def enrich(self, client, listing):
        self.enriched.append(listing.source_id)
        if listing.source_id in self._fail_on:
            raise RuntimeError("detail page has expired")
        from dataclasses import replace

        return replace(
            listing,
            summary="A full detail page with a posting time.",
            metadata={**listing.metadata, "listing_timestamp": "2026-08-28T14:11:54-0700"},
        )


def run(tmp_path: pathlib.Path, source, preferences_text: str = TEST_PREFERENCES, trigger="manual"):
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    preferences = parse_preferences(preferences_text)
    outcome = Scanner(repository, lambda: preferences, [source]).run_scan(trigger)
    return repository, outcome


# --------------------------------------------------------------------------
# who gets the budget
# --------------------------------------------------------------------------


def test_the_budget_goes_to_the_best_candidates_not_the_first_ones(
    tmp_path: pathlib.Path,
) -> None:
    """The whole point of the change."""
    # Arrival order is deliberately worst-first. Only the last three are in budget.
    listings = [room(f"bad{i}", 9000) for i in range(6)] + [
        room("good1", 1500),
        room("good2", 1400),
        room("good3", 1600),
    ]
    source = RecordingSource(listings, budget=3)

    run(tmp_path, source)

    assert set(source.enriched) == {"good1", "good2", "good3"}
    assert len(source.enriched) == 3, "the budget must not grow"


def test_the_number_of_detail_fetches_is_unchanged(tmp_path: pathlib.Path) -> None:
    """This is a reordering, not more network traffic."""
    listings = [room(f"r{i}", 1500) for i in range(20)]
    source = RecordingSource(listings, budget=5)

    run(tmp_path, source)

    assert len(source.enriched) == 5


def test_ties_keep_document_order_so_the_newer_listing_wins(tmp_path: pathlib.Path) -> None:
    """Craigslist searches are date-sorted, so arrival order carries real meaning."""
    listings = [room(f"r{i}", 1500) for i in range(6)]
    source = RecordingSource(listings, budget=2)

    run(tmp_path, source)

    assert source.enriched == ["r0", "r1"], "equal scores must not be reordered arbitrarily"


def test_a_source_whose_cards_have_no_price_is_not_starved(tmp_path: pathlib.Path) -> None:
    """Zumper publishes no rent on its search page; unknowns score neutral."""
    listings = [room(f"r{i}", None) for i in range(6)]
    source = RecordingSource(listings, budget=3)

    run(tmp_path, source)

    assert len(source.enriched) == 3


def test_the_budget_is_spent_even_when_every_candidate_is_poor(tmp_path: pathlib.Path) -> None:
    """A low score is not a reason to leave the budget unused."""
    listings = [room(f"bad{i}", 9000) for i in range(5)]
    source = RecordingSource(listings, budget=2)

    run(tmp_path, source)

    assert len(source.enriched) == 2


# --------------------------------------------------------------------------
# what must not change
# --------------------------------------------------------------------------


def test_an_already_enriched_listing_is_not_fetched_again(tmp_path: pathlib.Path) -> None:
    listings = [room("kept", 1500), room("fresh", 1500)]
    first = RecordingSource(listings, budget=2)
    repository, _ = run(tmp_path, first)
    assert set(first.enriched) == {"kept", "fresh"}

    second = RecordingSource(listings, budget=2)
    preferences = parse_preferences(TEST_PREFERENCES)
    Scanner(repository, lambda: preferences, [second]).run_scan("scheduled")

    assert second.enriched == [], "stored detail pages must not be refetched"


def test_a_failing_detail_page_never_loses_the_listing(tmp_path: pathlib.Path) -> None:
    listings = [room("broken", 1500), room("fine", 1500)]
    source = RecordingSource(listings, budget=2, fail_on={"broken"})

    repository, outcome = run(tmp_path, source)

    assert outcome.status == "completed", "a bad detail page is a warning, not a failure"
    stored = {row["source_id"] for row in repository.query_listings(0, view="all")}
    assert stored == {"broken", "fine"}


def test_stored_detail_is_not_erased_by_a_later_thin_card(tmp_path: pathlib.Path) -> None:
    """The merge that protects earlier enrichment must survive the restructure."""
    repository, _ = run(tmp_path, RecordingSource([room("r1", 1500)], budget=1))
    before = repository.query_listings(0, view="all")[0]
    assert before["summary"] == "A full detail page with a posting time."

    thin = RecordingSource([room("r1", None, summary="")], budget=0)
    preferences = parse_preferences(TEST_PREFERENCES)
    Scanner(repository, lambda: preferences, [thin]).run_scan("scheduled")

    after = repository.query_listings(0, view="all")[0]
    assert after["summary"] == "A full detail page with a posting time."
    assert after["price"] == 1500, "a card without a price must not erase a known one"
    assert after["published_at"], "the posting date learned earlier must survive"


def test_the_scan_deadline_still_stops_enrichment(tmp_path: pathlib.Path) -> None:
    listings = [room(f"r{i}", 1500) for i in range(10)]
    source = RecordingSource(listings, budget=10)
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    preferences = parse_preferences(TEST_PREFERENCES)
    scanner = Scanner(repository, lambda: preferences, [source], max_scan_seconds=0.0)

    scanner.run_scan("manual")

    assert source.enriched == [], "an expired budget must not start detail fetches"


def test_every_listing_is_still_stored_and_counted(tmp_path: pathlib.Path) -> None:
    listings = [room(f"r{i}", 1500) for i in range(9)]
    source = RecordingSource(listings, budget=2)

    repository, outcome = run(tmp_path, source)

    assert outcome.listings_seen == 9
    assert outcome.listings_added == 9
    assert len(repository.query_listings(0, view="all")) == 9


def test_the_first_run_window_still_applies_before_the_budget(tmp_path: pathlib.Path) -> None:
    """Filtering happens first, so the budget is never spent on excluded homes."""
    from dataclasses import replace

    old = replace(room("old", 1500), metadata={"listing_timestamp": "2020-01-01T00:00:00+00:00"})
    new = room("new", 1500)
    source = RecordingSource([old, new], budget=5)

    run(tmp_path, source, trigger="initial_discovery")

    assert source.enriched == ["new"], "a listing outside the window must not be enriched"


# --------------------------------------------------------------------------
# the outcome that matters
# --------------------------------------------------------------------------


def test_the_shortlist_gets_the_posting_dates(tmp_path: pathlib.Path) -> None:
    """Homes above the display threshold should carry a real date."""
    good = [room(f"good{i}", 1500) for i in range(5)]
    poor = [room(f"poor{i}", 9500) for i in range(15)]
    source = RecordingSource(poor + good, budget=5)

    repository, _ = run(tmp_path, source)

    preferences = parse_preferences(TEST_PREFERENCES)
    shortlist = repository.query_listings(
        preferences.minimum_score, view="active", housing_kind="room"
    )
    assert shortlist, "the good rooms should clear the threshold"
    dated = [row for row in shortlist if row["published_at"]]
    assert len(dated) == len(shortlist), "every shortlisted home carries a posting date"
