from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sf_housing.database import Repository
from sf_housing.freshness import FRESHNESS_WINDOW, evaluate_source_freshness, source_key
from sf_housing.models import ListingCandidate
from sf_housing.scanner import Scanner
from sf_housing.sources import SourceError


class CurrentSource:
    platform = "Craigslist"
    mode = "automatic"
    provider = "public"
    search_url = "https://example.test/craigslist"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        return [
            ListingCandidate(
                platform=self.platform,
                source_id="current-1",
                title="Private room in NOPA",
                original_url="https://example.test/craigslist/current-1",
                price=1500,
                neighborhood="NOPA",
            )
        ]

    def enrich(self, client, listing):
        return listing


class EmptySource(CurrentSource):
    platform = "SpareRoom"
    search_url = "https://example.test/spareroom"

    def search(self, client, preferences):
        return []


class FlakySource(CurrentSource):
    platform = "Craigslist"
    search_url = "https://example.test/flaky"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = True

    def search(self, client, preferences):
        self.calls += 1
        if self.fail:
            raise SourceError("Craigslist returned no recognizable result cards; its page format may have changed.")
        return super().search(client, preferences)


class FacebookGmailSource(CurrentSource):
    platform = "Facebook Marketplace"
    provider = "gmail_alerts"
    source_key = "FacebookMarketplaceSource"
    search_url = "https://www.facebook.com/marketplace/you/alerts/"


def record_source_run(
    repository: Repository,
    source: CurrentSource,
    status: str,
    *,
    seen: int = 0,
    message: str | None = None,
) -> int:
    scan_id = repository.begin_scan("fixture")
    source_id = repository.begin_source_run(
        scan_id,
        source.platform,
        source.search_url,
        provider=source.provider,
        source_key=source_key(source),
    )
    repository.finish_source_run(source_id, status, seen=seen, message=message)
    repository.finish_scan(scan_id, "completed" if status == "success" else "completed_with_errors")
    return source_id


def test_successful_zero_inventory_is_current_not_a_failure(repository: Repository) -> None:
    source = EmptySource()
    record_source_run(repository, source, "success", seen=0)

    health = evaluate_source_freshness(repository, source)

    assert health.status == "working_zero"
    assert "valid result" in health.explanation
    assert health.needs_attention is False


def test_stale_success_is_visible_even_when_old_listings_are_preserved(repository: Repository) -> None:
    source = CurrentSource()
    source_id = record_source_run(repository, source, "success", seen=3)
    old = (datetime.now(UTC) - FRESHNESS_WINDOW - timedelta(minutes=1)).isoformat()
    with repository.connection() as connection:
        connection.execute(
            "UPDATE source_runs SET finished_at = ? WHERE id = ?", (old, source_id)
        )
        connection.commit()

    health = evaluate_source_freshness(repository, source)

    assert health.status == "stale"
    assert "Existing listings remain available" in health.explanation
    assert "Check for new homes now" in health.action


def test_parser_drift_after_a_good_run_is_attention_not_silent(repository: Repository) -> None:
    source = CurrentSource()
    record_source_run(repository, source, "success", seen=2)
    record_source_run(
        repository,
        source,
        "error",
        message="Craigslist returned no recognizable result cards; its page format may have changed.",
    )

    health = evaluate_source_freshness(repository, source)

    assert health.status == "attention"
    assert health.failure_streak == 1
    assert "last good result is preserved" in health.explanation.casefold()
    assert "format may have changed" in health.explanation


def test_repeated_failures_back_off_automatic_scans_but_manual_retry_recovers(
    repository: Repository, preferences
) -> None:
    flaky = FlakySource()
    healthy = EmptySource()
    scanner = Scanner(repository, lambda: preferences, [flaky, healthy], detail_delay_seconds=0)

    first = scanner.run_scan("scheduled")
    second = scanner.run_scan("scheduled")
    third = scanner.run_scan("scheduled")

    assert first.sources_failed == 1
    assert second.sources_failed == 1
    assert third.status == "completed"
    assert flaky.calls == 2
    statuses = {run["platform"]: run for run in repository.latest_source_runs()}
    assert statuses["Craigslist"]["status"] == "backoff"
    assert statuses["SpareRoom"]["status"] == "success"
    paused = evaluate_source_freshness(repository, flaky)
    assert paused.status == "backoff"
    assert paused.failure_streak == 2
    assert "Retries automatically after" in paused.action
    # The panel used to say a source was paused and when it would retry, and
    # never what had gone wrong.
    assert paused.reason, "a paused source has to say what failed"
    assert paused.reason in paused.panel_note
    assert paused.action in paused.panel_note
    # The deferral writes its own notice as that run's message, so reading the
    # newest run quoted "Retries automatically after ..." back as the cause.
    assert "Retries automatically" not in paused.reason
    assert "recognizable result cards" in paused.reason, "quote the failure itself"
    # The row is already headed by the platform, so the cause does not repeat it.
    assert not paused.reason.startswith("Craigslist")

    flaky.fail = False
    recovered = scanner.run_scan("manual")
    health = evaluate_source_freshness(repository, flaky)

    assert recovered.status == "completed"
    assert flaky.calls == 3
    assert health.status == "working"
    assert health.failure_streak == 0


def test_freshness_survives_restart_and_new_source_identity_does_not_merge_providers(tmp_path) -> None:
    path = tmp_path / "housing.sqlite3"
    repository = Repository(path)
    repository.initialize()
    source = CurrentSource()
    record_source_run(repository, source, "error", message="network one")
    record_source_run(repository, source, "error", message="network two")
    # A different provider may share a human platform name, but must not erase
    # this source's repeated-failure history.
    scan_id = repository.begin_scan("fixture")
    other = repository.begin_source_run(
        scan_id,
        "Craigslist",
        "https://example.test/other",
        provider="gmail",
        source_key="GmailCraigslistFixture",
    )
    repository.finish_source_run(other, "success", seen=1)
    repository.finish_scan(scan_id, "completed")

    restarted = Repository(path)
    restarted.initialize()
    health = evaluate_source_freshness(restarted, source)

    assert health.status == "backoff"
    assert health.failure_streak == 2
    latest = {run["source_key"]: run for run in restarted.latest_source_runs()}
    assert latest["CurrentSource::public"]["status"] == "error"
    assert latest["GmailCraigslistFixture"]["status"] == "success"


def test_gmail_and_apify_facebook_history_stay_separate(repository: Repository) -> None:
    gmail = FacebookGmailSource()
    record_source_run(repository, gmail, "error", message="Gmail alert parser found no supported direct link.")
    scan_id = repository.begin_scan("fixture")
    apify_id = repository.begin_source_run(
        scan_id,
        "Facebook Marketplace",
        "https://example.test/apify",
        provider="apify",
        source_key="FacebookMarketplaceSource::apify",
    )
    repository.finish_source_run(apify_id, "success", seen=3)
    repository.finish_scan(scan_id, "completed_with_errors", failed=1)

    gmail_health = evaluate_source_freshness(repository, gmail)
    latest = {run["source_key"]: run for run in repository.latest_source_runs()}

    assert gmail_health.status == "stale"
    assert gmail_health.failure_streak == 1
    assert latest["FacebookMarketplaceSource::gmail_alerts"]["status"] == "error"
    assert latest["FacebookMarketplaceSource::apify"]["status"] == "success"
