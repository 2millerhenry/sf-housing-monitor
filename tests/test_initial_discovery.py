"""The first scan after a deal is saved backfills a bounded window of recent listings.

``initial_discovery`` runs once per source, the first time a profile goes from
draft to active. Its date filter decides what a brand-new user sees on the very
first screen, so the window is asserted directly here -- nothing covered this
function before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sf_housing.models import ListingCandidate
from sf_housing.scanner import INITIAL_DISCOVERY_WINDOW, Scanner


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def listing(timestamp: object) -> ListingCandidate:
    metadata = {} if timestamp is None else {"listing_timestamp": timestamp}
    return ListingCandidate(
        platform="Test",
        source_id="1",
        title="A room",
        original_url="https://example.test/1",
        metadata=metadata,
    )


def published(days: float) -> ListingCandidate:
    return listing((NOW - timedelta(days=days)).isoformat())


def test_the_window_is_seven_days() -> None:
    assert INITIAL_DISCOVERY_WINDOW == timedelta(days=7)


@pytest.mark.parametrize("days", [0, 1, 3, 5.5, 6, 6.9])
def test_listings_inside_the_window_are_kept(days: float) -> None:
    assert Scanner._within_initial_window(published(days), now=NOW) is True


@pytest.mark.parametrize("days", [7.1, 8, 14, 90])
def test_listings_older_than_the_window_are_dropped(days: float) -> None:
    assert Scanner._within_initial_window(published(days), now=NOW) is False


def test_the_boundary_is_inclusive() -> None:
    """Exactly at the edge is kept; a second past it is not."""
    assert Scanner._within_initial_window(published(7), now=NOW) is True

    just_outside = (NOW - INITIAL_DISCOVERY_WINDOW - timedelta(seconds=1)).isoformat()
    assert Scanner._within_initial_window(listing(just_outside), now=NOW) is False


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "not a date", 1234567890, {"nested": "object"}, []],
)
def test_an_unusable_timestamp_keeps_the_listing(value: object) -> None:
    """Most public sources publish no reliable timestamp.

    Dropping those would empty the first screen rather than fill it, which is
    the opposite of what this window exists to do.
    """
    assert Scanner._within_initial_window(listing(value), now=NOW) is True


def test_a_naive_timestamp_is_read_as_utc() -> None:
    recent = (NOW - timedelta(days=2)).replace(tzinfo=None).isoformat()
    old = (NOW - timedelta(days=30)).replace(tzinfo=None).isoformat()

    assert Scanner._within_initial_window(listing(recent), now=NOW) is True
    assert Scanner._within_initial_window(listing(old), now=NOW) is False


def test_a_trailing_z_is_accepted() -> None:
    recent = (NOW - timedelta(days=2)).replace(tzinfo=None).isoformat() + "Z"
    old = (NOW - timedelta(days=30)).replace(tzinfo=None).isoformat() + "Z"

    assert Scanner._within_initial_window(listing(recent), now=NOW) is True
    assert Scanner._within_initial_window(listing(old), now=NOW) is False


def test_a_non_utc_offset_is_compared_correctly() -> None:
    """A Pacific-stamped listing must not be judged by its wall-clock text."""
    pacific_recent = (NOW - timedelta(days=1)).astimezone(
        __import__("zoneinfo").ZoneInfo("America/Los_Angeles")
    ).isoformat()

    assert Scanner._within_initial_window(listing(pacific_recent), now=NOW) is True


def test_whitespace_around_a_valid_timestamp_is_tolerated() -> None:
    padded = f"  {(NOW - timedelta(days=1)).isoformat()}  "

    assert Scanner._within_initial_window(listing(padded), now=NOW) is True
