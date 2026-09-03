from __future__ import annotations

import threading
import time
from dataclasses import replace

from sf_housing.database import Repository
from sf_housing.gmail_alerts import GmailAlertError
from sf_housing.models import ListingCandidate
from sf_housing.preferences import Preferences
from sf_housing.scanner import Scanner


class GoodSource:
    platform = "Good"
    mode = "automatic"
    search_url = "https://example.test/good"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        return [
            ListingCandidate(
                platform=self.platform,
                source_id="1",
                title="Sunny private room in a house near Golden Gate Park",
                original_url="https://example.test/listing/1",
                price=1500,
                neighborhood="NOPA",
                summary="Flexible lease, communal garden, 4 roommates.",
                metadata={"property_type": "house", "rooms_in_property": "4"},
            )
        ]

    def enrich(self, client, listing):
        return listing


class BrokenSource:
    platform = "Broken"
    mode = "automatic"
    search_url = "https://example.test/broken"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        raise RuntimeError("upstream unavailable")

    def enrich(self, client, listing):
        return listing


class GmailProviderSource(GoodSource):
    platform = "Zillow"
    connector_key = "gmail"
    connector_state_key = "gmail:zillow"
    provider = "gmail"
    last_alert_count = 1


class EmptyGmailProviderSource(GmailProviderSource):
    last_alert_count = 0

    def search(self, client, preferences):
        return []


class OpaqueGmailProviderSource(EmptyGmailProviderSource):
    last_alert_count = 1
    empty_result_message = "An alert arrived without a supported direct listing link."


class ExpiredGmailProviderSource(GmailProviderSource):
    def search(self, client, preferences):
        raise GmailAlertError("Gmail authorization is invalid or expired. Reconnect it from Sources.")


class TimedOutGmailProviderSource(GmailProviderSource):
    def search(self, client, preferences):
        raise GmailAlertError("Gmail alert search timed out or failed.")


class BlockingSource(GoodSource):
    platform = "Blocking"

    def __init__(self, started: threading.Event, release: threading.Event):
        self.started = started
        self.release = release

    def search(self, client, preferences):
        self.started.set()
        assert self.release.wait(timeout=3)
        return super().search(client, preferences)


class IncrementalDetailSource(GoodSource):
    platform = "Incremental"
    detail_budget = 1

    def search(self, client, preferences):
        self.detail_budget = 1
        return [
            ListingCandidate(
                platform=self.platform,
                source_id=str(number),
                title=f"Room {number}",
                original_url=f"https://example.test/incremental/{number}",
                price=1500,
                neighborhood="NOPA",
                summary=f"Room {number}",
            )
            for number in (1, 2)
        ]

    def enrich(self, client, listing):
        return replace(
            listing,
            summary=f"{listing.title} full detail with a sunny private room in a house.",
            metadata={"enriched": True},
        )


class TriggerAwareSource(GoodSource):
    platform = "Trigger aware"

    def __init__(self):
        self.trigger = None

    def search_for_trigger(self, client, preferences, trigger):
        self.trigger = trigger
        return self.search(client, preferences)


def test_one_source_failure_does_not_stop_other_sources(
    repository: Repository, preferences: Preferences
) -> None:
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BrokenSource(), GoodSource()],
        detail_delay_seconds=0,
    )

    outcome = scanner.run_scan("test")

    assert outcome.status == "completed_with_errors"
    assert outcome.sources_failed == 1
    assert outcome.listings_added == 1
    assert len(repository.query_listings(0, view="all")) == 1
    statuses = {item["platform"]: item for item in repository.latest_source_runs()}
    assert statuses["Broken"]["status"] == "error"
    assert "upstream unavailable" in statuses["Broken"]["message"]
    assert statuses["Good"]["status"] == "success"


def test_scan_passes_trigger_to_sources_that_support_trigger_specific_depth(
    repository: Repository, preferences: Preferences
) -> None:
    source = TriggerAwareSource()
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed"
    assert source.trigger == "manual"


def test_gmail_provider_success_updates_provider_and_aggregate_state(repository, preferences) -> None:
    scanner = Scanner(repository, lambda: preferences, [GmailProviderSource()], detail_delay_seconds=0)

    outcome = scanner.run_scan("gmail_test")

    provider = repository.connector_state("gmail:zillow")
    aggregate = repository.connector_state("gmail")
    assert outcome.status == "completed"
    assert provider is not None and provider.state == "working"
    assert aggregate is not None and aggregate.state == "working"
    assert repository.query_listings(0, view="all")


def test_gmail_provider_distinguishes_waiting_from_unparseable_alert(repository, preferences) -> None:
    waiting = Scanner(
        repository, lambda: preferences, [EmptyGmailProviderSource()], detail_delay_seconds=0
    )
    waiting.run_scan("gmail_test")
    assert repository.connector_state("gmail:zillow").state == "waiting_first_alert"

    opaque = Scanner(
        repository, lambda: preferences, [OpaqueGmailProviderSource()], detail_delay_seconds=0
    )
    opaque.run_scan("gmail_test")
    state = repository.connector_state("gmail:zillow")
    assert state is not None and state.state == "degraded"
    assert "without a supported" in state.message


def test_expired_gmail_does_not_stop_a_later_public_source(repository, preferences) -> None:
    scanner = Scanner(
        repository,
        lambda: preferences,
        [ExpiredGmailProviderSource(), GoodSource()],
        detail_delay_seconds=0,
    )

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed_with_errors"
    assert repository.connector_state("gmail:zillow").state == "authorization_expired"
    assert repository.connector_state("gmail").state == "authorization_expired"
    statuses = {run["platform"]: run["status"] for run in repository.latest_source_runs()}
    assert statuses["Zillow"] == "error"
    assert statuses["Good"] == "success"


def test_gmail_timeout_is_provider_degraded_and_public_source_still_runs(
    repository, preferences
) -> None:
    scanner = Scanner(
        repository,
        lambda: preferences,
        [TimedOutGmailProviderSource(), GoodSource()],
        detail_delay_seconds=0,
    )

    outcome = scanner.run_scan("gmail_test")

    assert outcome.status == "completed_with_errors"
    assert repository.connector_state("gmail:zillow").state == "degraded"
    statuses = {run["platform"]: run["status"] for run in repository.latest_source_runs()}
    assert statuses == {"Good": "success", "Zillow": "error"}


def test_scheduled_scan_runs_fast_sources_before_slow_browser_helpers(
    repository: Repository, preferences: Preferences
) -> None:
    def source(
        platform: str,
        scheduled_only: bool = False,
        manual_scan_enabled: bool = False,
    ):
        return type(
            "Source",
            (),
            {
                "platform": platform,
                "scheduled_only": scheduled_only,
                "manual_scan_enabled": manual_scan_enabled,
                "mode": "automatic",
                "search_url": "https://example.test",
                "manual_reason": None,
                "detail_budget": 0,
            },
        )()

    scanner = Scanner(
        repository,
        lambda: preferences,
        [
            source("Facebook Marketplace", True, True),
            source("Facebook Groups", True, True),
            source("Furnished Finder", True),
            source("Craigslist"),
            source("SpareRoom"),
            source("Zillow"),
        ],
    )

    assert [item.platform for item in scanner._eligible_sources("scheduled")] == [
        "Craigslist",
        "SpareRoom",
        "Zillow",
        "Furnished Finder",
        "Facebook Marketplace",
        "Facebook Groups",
    ]

    assert [item.platform for item in scanner._eligible_sources("manual")] == [
        "Craigslist",
        "SpareRoom",
        "Zillow",
        "Facebook Marketplace",
        "Facebook Groups",
    ]


def test_a_one_off_source_list_does_not_run_every_configured_source(
    repository: Repository, preferences: Preferences
) -> None:
    scanner = Scanner(repository, lambda: preferences, [BrokenSource(), GoodSource()], detail_delay_seconds=0)

    outcome = scanner.run_scan("backfill", sources=[GoodSource()])

    assert outcome.status == "completed"
    assert outcome.sources_failed == 0
    assert outcome.listings_added == 1
    assert "Broken" not in {item["platform"] for item in repository.latest_source_runs()}


def test_overlapping_scans_are_skipped(
    repository: Repository, preferences: Preferences
) -> None:
    started, release = threading.Event(), threading.Event()
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BlockingSource(started, release)],
        detail_delay_seconds=0,
    )
    thread = threading.Thread(target=scanner.run_scan, args=("scheduled",))
    thread.start()
    assert started.wait(timeout=2)

    overlapping = scanner.run_scan("manual")
    release.set()
    thread.join(timeout=3)

    assert overlapping.status == "skipped"
    assert overlapping.already_running is True
    assert not thread.is_alive()


def test_start_scan_marks_running_before_returning(
    repository: Repository, preferences: Preferences
) -> None:
    started, release = threading.Event(), threading.Event()
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BlockingSource(started, release)],
        detail_delay_seconds=0,
    )

    assert scanner.start_scan("manual") is True
    assert scanner.is_running is True
    assert started.wait(timeout=2)
    assert scanner.start_scan("manual") is False

    release.set()


def test_scan_progress_reports_real_source_and_completion(
    repository: Repository, preferences: Preferences
) -> None:
    started, release = threading.Event(), threading.Event()
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BlockingSource(started, release), GoodSource()],
        detail_delay_seconds=0,
    )

    assert scanner.start_scan("manual") is True
    assert started.wait(timeout=2)
    active = scanner.progress
    assert active["running"] is True
    assert active["current_source"] == "Blocking"
    assert active["sources_total"] == 2
    assert active["sources_completed"] == 0
    assert active["percent"] == 0

    release.set()
    deadline = time.monotonic() + 3
    while scanner.is_running and time.monotonic() < deadline:
        time.sleep(0.01)

    finished = scanner.progress
    assert finished["running"] is False
    assert finished["status"] == "completed"
    assert finished["sources_completed"] == 2
    assert finished["listings_seen"] == 2
    assert finished["percent"] == 100


def test_detail_enrichment_is_preserved_and_advances_on_later_scans(
    repository: Repository, preferences: Preferences
) -> None:
    scanner = Scanner(
        repository,
        lambda: preferences,
        [IncrementalDetailSource()],
        detail_delay_seconds=0,
    )

    scanner.run_scan("first")
    after_first = {item["source_id"]: item for item in repository.query_listings(0, view="all")}
    assert "full detail" in after_first["1"]["summary"]
    assert after_first["2"]["summary"] == "Room 2"

    scanner.run_scan("second")
    after_second = {item["source_id"]: item for item in repository.query_listings(0, view="all")}
    assert "full detail" in after_second["1"]["summary"]
    assert "full detail" in after_second["2"]["summary"]
    assert after_second["1"]["metadata"]["enriched"] is True
