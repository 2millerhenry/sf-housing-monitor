"""A schedule that runs half the time is worse than a manual button.

Over the first four days this app existed, eight scheduled runs were due and
four happened. The cron job fires only while this process is alive and the Mac
is awake, and APScheduler keeps its schedule in memory, so a restart after a
missed slot recomputes the next run from now and that slot is gone for good.

These tests are about the safety net: a heartbeat that asks the database what
actually ran, rather than asking the scheduler what it meant to run.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.scanner import AUTOMATIC_TRIGGERS, FULL_SOURCE_TRIGGERS, Scanner
from sf_housing.scheduling import (
    CATCH_UP_INTERVAL_MINUTES,
    catch_up_if_due,
    latest_scheduled_time,
    scheduled_scan_due,
)
from tests.conftest import TEST_PREFERENCES


class Source:
    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/a"
    manual_reason = None
    detail_budget = 0
    recheck_budget = 0

    def __init__(self):
        self.searches = 0

    def search(self, client, preferences):
        self.searches += 1
        return [
            ListingCandidate(
                platform="Craigslist",
                source_id="r1",
                title="Sunny private room in NOPA",
                original_url="https://sfbay.craigslist.org/roo/d/x/1.html",
                price=1500,
                neighborhood="NOPA",
                listing_type="Room/share",
                summary="A private room in a shared home.",
            )
        ]

    def enrich(self, client, listing):
        return listing


def settle(scanner: Scanner, timeout: float = 30.0) -> None:
    """Wait for a background scan to finish.

    Blocks on the scanner rather than asking it repeatedly against a deadline:
    a poll loop that gives up after a fixed number of seconds turns a slow
    machine into a failed assertion about the scanner.
    """
    assert scanner.wait_until_idle(timeout), "a scan never finished"


def board(tmp_path: pathlib.Path, source=None):
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    preferences = parse_preferences(TEST_PREFERENCES)
    scanner = Scanner(repository, lambda: preferences, [source or Source()])
    return repository, scanner


def record_scan(repository: Repository, *, when: datetime, status="completed", trigger="scheduled"):
    """A finished scan in the history, as the scheduler would have left it."""
    run_id = repository.begin_scan(trigger)
    with repository.connection() as connection:
        connection.execute(
            "UPDATE scan_runs SET started_at = ?, finished_at = ?, status = ? WHERE id = ?",
            (when.isoformat(), when.isoformat(), status, run_id),
        )
        connection.commit()
    return run_id


# --------------------------------------------------------------------------
# the question itself
# --------------------------------------------------------------------------


def test_a_slot_that_went_unserved_is_due() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert scheduled_scan_due([], now) is True


def test_a_slot_already_served_is_not_due() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    served = latest_scheduled_time(now) + timedelta(minutes=2)
    assert scheduled_scan_due(
        [{"status": "completed", "started_at": served.isoformat()}], now
    ) is False


def test_a_scan_that_never_finished_does_not_count_as_serving_a_slot() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    served = latest_scheduled_time(now) + timedelta(minutes=2)
    assert scheduled_scan_due(
        [{"status": "running", "started_at": served.isoformat()}], now
    ) is True


def test_three_days_asleep_asks_for_one_scan_not_six() -> None:
    """Catching up to now, never replaying history."""
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    stale = (latest_scheduled_time(now) - timedelta(days=3)).isoformat()

    assert scheduled_scan_due([{"status": "completed", "started_at": stale}], now) is True
    # One scan lands, and the question is answered for this slot.
    served = latest_scheduled_time(now) + timedelta(minutes=1)
    assert scheduled_scan_due(
        [{"status": "completed", "started_at": served.isoformat()}], now
    ) is False


# --------------------------------------------------------------------------
# the heartbeat
# --------------------------------------------------------------------------


def test_the_heartbeat_runs_a_scan_when_a_slot_was_missed(tmp_path: pathlib.Path) -> None:
    repository, scanner = board(tmp_path)

    assert catch_up_if_due(scanner) is True
    settle(scanner)

    runs = [dict(r) for r in repository.recent_scans(5)]
    assert [r["trigger"] for r in runs] == ["catch_up"]
    assert runs[0]["status"] == "completed"


def test_the_heartbeat_does_nothing_when_the_slot_was_served(tmp_path: pathlib.Path) -> None:
    repository, scanner = board(tmp_path)
    record_scan(repository, when=datetime.now(UTC))

    assert catch_up_if_due(scanner) is False
    assert len(repository.recent_scans(5)) == 1, "no second scan was started"


def test_the_heartbeat_never_runs_two_scans_at_once(tmp_path: pathlib.Path) -> None:
    """start_scan is lock-guarded; the heartbeat must not try to go around it."""
    repository, scanner = board(tmp_path)
    assert scanner.start_scan("scheduled") is True
    try:
        assert catch_up_if_due(scanner) is False
    finally:
        settle(scanner)


def test_the_heartbeat_does_nothing_before_a_deal_exists(tmp_path: pathlib.Path) -> None:
    """A draft profile means the app has no idea what to look for yet."""
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    import yaml

    document = yaml.safe_load(TEST_PREFERENCES)
    document["profile_version"] = 1
    document["profile"] = {"state": "draft", "enabled_paths": [], "budgets": {}}
    draft = parse_preferences(yaml.safe_dump(document))
    assert draft.profile_active is False, "the fixture has to actually be inactive"
    scanner = Scanner(repository, lambda: draft, [Source()])

    assert catch_up_if_due(scanner) is False
    assert repository.recent_scans(5) == []


def test_a_broken_preference_file_cannot_stop_the_heartbeat(tmp_path: pathlib.Path) -> None:
    """It runs inside APScheduler, which logs and drops an exception. Its job is
    to make scanning more reliable, so it can never be the thing that breaks."""
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()

    def explode():
        raise ValueError("preferences.yaml is not readable")

    scanner = Scanner(repository, explode, [Source()])

    assert catch_up_if_due(scanner) is False


def test_only_one_catch_up_runs_for_one_missed_slot(tmp_path: pathlib.Path) -> None:
    """Two heartbeats fifteen minutes apart must not mean two scans."""
    repository, scanner = board(tmp_path)

    assert catch_up_if_due(scanner) is True
    settle(scanner)
    assert catch_up_if_due(scanner) is False

    assert len(repository.recent_scans(10)) == 1


# --------------------------------------------------------------------------
# a catch-up has to be the scan it is replacing
# --------------------------------------------------------------------------


def test_a_catch_up_reaches_the_same_sources_a_schedule_would() -> None:
    """The trap this nearly walked into: a new trigger that is not in the
    full-source set quietly scans less than the run it stands in for."""
    assert "catch_up" in FULL_SOURCE_TRIGGERS
    assert "catch_up" in AUTOMATIC_TRIGGERS
    assert {"scheduled", "startup_catchup"} <= AUTOMATIC_TRIGGERS


def test_a_catch_up_includes_scheduled_only_sources(tmp_path: pathlib.Path) -> None:
    class ScheduledOnly(Source):
        platform = "SpareRoom"
        scheduled_only = True

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    preferences = parse_preferences(TEST_PREFERENCES)
    only = ScheduledOnly()
    scanner = Scanner(repository, lambda: preferences, [only])

    scanner.run_scan("catch_up")

    assert only.searches == 1, "a catch-up must not be a lesser scan than the one it replaces"


def test_the_heartbeat_interval_is_shorter_than_the_gap_it_covers() -> None:
    """A missed 10:00 check has to be served the same morning."""
    assert 0 < CATCH_UP_INTERVAL_MINUTES <= 30


# --------------------------------------------------------------------------
# the whole thing, through a real app
# --------------------------------------------------------------------------


def test_a_mac_that_slept_through_a_slot_catches_up_when_it_wakes(
    tmp_path: pathlib.Path,
) -> None:
    """The actual failure: the process is alive, the cron slot went by while the
    machine was asleep, and APScheduler's own memory of it is gone. The
    dashboard must go from behind to current without anybody pressing
    anything."""
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.liveness import schedule_health
    from sf_housing.scheduling import SCAN_JOB_ID
    from sf_housing.settings import Settings

    class Job:
        id = SCAN_JOB_ID
        next_run_time = datetime.now(UTC) + timedelta(hours=4)

    class RunningScheduler:
        running = True

        def get_jobs(self):
            return [Job()]

    alive = RunningScheduler()

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences_path,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    application = create_app(settings=settings, sources=[Source()], enable_scheduler=False)
    repository = application.state.repository
    scanner = application.state.scanner

    # Yesterday's checks ran; today's have not.
    long_ago = latest_scheduled_time(datetime.now(UTC)) - timedelta(days=1)
    record_scan(repository, when=long_ago)

    with TestClient(application):
        before = schedule_health(alive, [dict(r) for r in repository.recent_scans(20)])
        assert before.state == "overdue", before.summary

        assert catch_up_if_due(scanner) is True
        settle(scanner)

        after = schedule_health(alive, [dict(r) for r in repository.recent_scans(20)])

    assert after.state == "current", after.summary
    triggers = [r["trigger"] for r in repository.recent_scans(20)]
    assert triggers.count("catch_up") == 1, triggers


def test_the_catch_up_actually_collects_homes(tmp_path: pathlib.Path) -> None:
    """A catch-up that runs but imports nothing would pass every test above and
    still leave the shortlist stale."""
    repository, scanner = board(tmp_path)

    assert catch_up_if_due(scanner) is True
    settle(scanner)

    assert len(repository.query_listings(0, view="all")) == 1


def test_a_behind_schedule_dashboard_says_so(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A last-checked time reads as fine on its own. When a slot went unserved
    the page has to name it, or a schedule running half the time looks healthy.

    The state is supplied directly because an unmanaged test app reports
    "unmanaged" and a managed one catches up before the page can be read -- both
    correct, and neither the state this line exists for.
    """
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.liveness import ScheduleHealth
    from sf_housing.settings import Settings

    behind = ScheduleHealth(
        managed=True, scheduler_running=True, job_count=1,
        next_run_at=datetime.now(UTC) + timedelta(hours=4),
        last_finished_at=datetime.now(UTC) - timedelta(days=1),
        last_status="completed", scan_running=False, state="overdue",
        summary="The last completed check was 1 day ago, which is more than 2 scheduled checks ago.",
    )
    monkeypatch.setattr("sf_housing.app.schedule_health", lambda *a, **k: behind)

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences_path,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    application = create_app(settings=settings, sources=[Source()], enable_scheduler=False)
    repository = application.state.repository
    record_scan(repository, when=latest_scheduled_time(datetime.now(UTC)) - timedelta(days=1))

    with TestClient(application) as client:
        page = client.get("/").text

    assert "liveness-behind" in page
    assert "Checks are behind schedule" in page
    assert "asleep or offline" in page
    assert f"within {CATCH_UP_INTERVAL_MINUTES} minutes" in page


def test_a_healthy_schedule_says_nothing_extra(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the pair: a working schedule must raise no warning at
    all, or the one above becomes wallpaper."""
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.liveness import ScheduleHealth
    from sf_housing.settings import Settings

    healthy = ScheduleHealth(
        managed=True, scheduler_running=True, job_count=1,
        next_run_at=datetime.now(UTC) + timedelta(hours=4),
        last_finished_at=datetime.now(UTC) - timedelta(minutes=20),
        last_status="completed", scan_running=False, state="current",
        summary="Last completed check 20 minutes ago.",
    )
    monkeypatch.setattr("sf_housing.app.schedule_health", lambda *a, **k: healthy)

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences_path,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    application = create_app(settings=settings, sources=[Source()], enable_scheduler=False)
    record_scan(application.state.repository, when=datetime.now(UTC))

    with TestClient(application) as client:
        page = client.get("/").text

    assert "liveness-behind" not in page
    assert "liveness-warning" not in page, "a healthy schedule raises no warning at all"
    assert "Checks are behind schedule" not in page
    assert "liveness-line" in page, "it shows the ordinary last-checked line instead"
