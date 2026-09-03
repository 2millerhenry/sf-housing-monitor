from __future__ import annotations

from dataclasses import replace

import pytest

from sf_housing.database import Repository
from sf_housing.gmail_alerts import AlertEmail
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import Preferences
from sf_housing.scanner import Scanner
from sf_housing.sources import SourceError, ZillowAlertSource


def _score(value: int) -> ScoreResult:
    return ScoreResult(value, ["Test match"], "Unknown: test detail.", {})


def test_low_score_listings_remain_visible_in_saved_and_all_views(repository: Repository) -> None:
    listing_id, _ = repository.upsert_listing(
        ListingCandidate(
            platform="Test",
            source_id="low-score",
            title="Saved room that later scored below threshold",
            original_url="https://example.test/low-score",
            price=1500,
            neighborhood="Potrero Hill",
        ),
        _score(45),
    )
    assert repository.set_listing_status(listing_id, "saved")

    assert repository.query_listings(minimum_score=60, view="active") == []
    assert [item["id"] for item in repository.query_listings(60, view="saved")] == [listing_id]
    assert [item["id"] for item in repository.query_listings(60, view="all")] == [listing_id]


class SparseRefreshSource:
    platform = "Sparse refresh"
    mode = "automatic"
    search_url = "https://example.test/search"
    manual_reason = None
    detail_budget = 0

    def __init__(self) -> None:
        self.sparse = False

    def search(self, client, preferences):
        rich = ListingCandidate(
            platform=self.platform,
            source_id="one",
            title="Sunny private room",
            original_url="https://example.test/listing/one",
            price=1500,
            neighborhood="Duboce Triangle",
            listing_type="Private room",
            summary="Sunny private room in a Victorian near Duboce Park.",
            metadata={"property_type": "Victorian"},
        )
        if not self.sparse:
            return [rich]
        return [
            replace(
                rich,
                price=None,
                neighborhood=None,
                listing_type=None,
                summary=rich.title,
                metadata={},
            )
        ]

    def enrich(self, client, listing):
        return listing


def test_sparse_refresh_does_not_erase_known_listing_fields(
    repository: Repository, preferences: Preferences
) -> None:
    source = SparseRefreshSource()
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    first = scanner.run_scan("first")
    source.sparse = True
    second = scanner.run_scan("second")

    assert first.status == "completed"
    assert second.status == "completed"
    stored = repository.query_listings(0, view="all")[0]
    assert stored["price"] == 1500
    assert stored["neighborhood"] == "Duboce Triangle"
    assert stored["listing_type"] == "Private room"
    assert stored["summary"] == "Sunny private room in a Victorian near Duboce Park."
    assert stored["metadata"]["property_type"] == "Victorian"


class MailboxWithUnparseableAlert:
    is_connected = True

    def messages(self, query: str, max_results: int = 100) -> list[AlertEmail]:
        return [
            AlertEmail(
                message_id="zillow-without-direct-link",
                subject="New listings for your saved search",
                html='<a href="https://www.zillow.com/myzillow/savedsearches/">View search</a>',
                text="A new home is available, but this fixture has no direct listing URL.",
            )
        ]


def test_alert_email_without_direct_listing_link_fails_visibly(preferences: Preferences) -> None:
    source = ZillowAlertSource(MailboxWithUnparseableAlert())

    with pytest.raises(SourceError, match="direct listing link"):
        source.search(None, preferences)
