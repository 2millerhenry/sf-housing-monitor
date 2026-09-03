"""Liveness must be observed, not asserted.

/health used to print the twice-daily schedule as a constant, so a scheduler that
died at start-up looked exactly like a healthy one. These tests kill the
scheduler, age the scan history, and check that every surface degrades together.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.liveness import (
    describe_age,
    last_finished_scan,
    previous_scheduled_time,
    schedule_health,
)
from sf_housing.scheduling import PACIFIC, latest_scheduled_time
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


NOW = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)  # 13:00 Pacific, after the 10:00 slot


class FakeJob:
    def __init__(self, next_run_time):
        self.next_run_time = next_run_time


class FakeScheduler:
    def __init__(self, running=True, jobs=None, explode=False):
        self.running = running
        self._jobs = jobs if jobs is not None else [FakeJob(NOW + timedelta(hours=5))]
        self._explode = explode

    def get_jobs(self):
        if self._explode:
            raise RuntimeError("scheduler is wedged")
        return self._jobs


def scan(status="completed", finished=NOW - timedelta(hours=1)):
    return {
        "id": 1,
        "trigger": "scheduled",
        "status": status,
        "started_at": (finished - timedelta(minutes=1)).isoformat() if finished else None,
        "finished_at": finished.isoformat() if finished else None,
    }


# --------------------------------------------------------------------------
# the computation
# --------------------------------------------------------------------------


def test_a_running_scheduler_with_a_recent_scan_is_current() -> None:
    health = schedule_health(FakeScheduler(), [scan()], now=NOW)

    assert health.state == "current"
    assert health.ok is True
    assert health.next_run_at is not None


def test_a_stopped_scheduler_is_reported_stopped() -> None:
    """The failure the old health endpoint could not see."""
    health = schedule_health(FakeScheduler(running=False), [scan()], now=NOW)

    assert health.state == "stopped"
    assert health.ok is False
    assert "not running" in health.summary


def test_a_running_scheduler_holding_no_jobs_is_also_stopped() -> None:
    """A scheduler with nothing registered fires nothing."""
    health = schedule_health(FakeScheduler(jobs=[]), [scan()], now=NOW)

    assert health.state == "stopped"
    assert health.ok is False


def test_a_scheduler_that_cannot_answer_is_not_treated_as_healthy() -> None:
    health = schedule_health(FakeScheduler(explode=True), [scan()], now=NOW)

    assert health.state == "stopped"


def test_a_fresh_install_reads_as_not_yet_rather_than_broken() -> None:
    health = schedule_health(FakeScheduler(), [], now=NOW)

    assert health.state == "not_yet"
    assert health.ok is True, "a new install is not a fault"


def test_a_scan_in_progress_is_never_reported_overdue() -> None:
    ancient = [scan(finished=NOW - timedelta(days=4))]

    health = schedule_health(FakeScheduler(), ancient, scan_running=True, now=NOW)

    assert health.state == "scanning"
    assert health.ok is True


def test_two_missed_slots_read_as_overdue() -> None:
    stale = [scan(finished=previous_scheduled_time(NOW) - timedelta(minutes=1))]

    health = schedule_health(FakeScheduler(), stale, now=NOW)

    assert health.state == "overdue"
    assert health.ok is False


def test_one_missed_slot_is_tolerated() -> None:
    """A Mac asleep through a single slot is normal, not a fault."""
    recent = [scan(finished=previous_scheduled_time(NOW) + timedelta(minutes=1))]

    assert schedule_health(FakeScheduler(), recent, now=NOW).state == "current"


def test_overdue_is_measured_against_the_schedule_not_wall_clock_hours() -> None:
    """Daylight saving and the schedule itself would drift a fixed hour count."""
    cutoff = previous_scheduled_time(NOW)

    assert cutoff < latest_scheduled_time(NOW)
    assert cutoff.astimezone(PACIFIC).hour in {10, 18}


def test_a_run_that_never_finished_is_not_evidence_of_health() -> None:
    assert last_finished_scan([scan(status="running", finished=None)]) is None
    assert last_finished_scan([scan(status="failed", finished=NOW)]) is None
    assert last_finished_scan([scan(status="completed_with_errors")]) is not None


def test_an_unmanaged_process_is_not_a_failure() -> None:
    """Command-line runs and the test suite do not own the schedule."""
    health = schedule_health(None, [], managed=False, now=NOW)

    assert health.state == "unmanaged"
    assert health.ok is True


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=10), "just now"),
        (timedelta(minutes=20), "20 minutes ago"),
        (timedelta(hours=3), "3 hours ago"),
        (timedelta(days=2), "2 days ago"),
    ],
)
def test_age_reads_in_plain_words(delta: timedelta, expected: str) -> None:
    assert describe_age(NOW - delta, NOW) == expected


def test_a_missing_or_unparseable_timestamp_does_not_crash() -> None:
    assert last_finished_scan([{"status": "completed", "finished_at": "not a date"}]) is None
    assert describe_age(None, NOW) == "never"


# --------------------------------------------------------------------------
# the surfaces
# --------------------------------------------------------------------------


def build(tmp_path: pathlib.Path, enable_scheduler: bool = True):
    preferences = tmp_path / "preferences.yaml"
    preferences.write_text(TEST_PREFERENCES, encoding="utf-8")
    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        preferences_path=preferences,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    return create_app(settings=settings, sources=[], enable_scheduler=enable_scheduler)


def test_health_reports_the_real_scheduler_and_says_ok(tmp_path: pathlib.Path) -> None:
    application = build(tmp_path)

    with TestClient(application) as client:
        payload = client.get("/health").json()

    assert payload["ok"] is True
    state = payload["scheduled_checking"]
    assert state["scheduler_running"] is True
    assert state["jobs"] >= 1
    assert state["next_run_at"], "a live scheduler holds a real next fire time"


def test_killing_the_scheduler_degrades_health_and_the_ready_check(tmp_path: pathlib.Path) -> None:
    """The whole point: a dead scheduler must stop looking healthy."""
    application = build(tmp_path)

    with TestClient(application) as client:
        assert client.get("/health").json()["scheduled_checking"]["ok"] is True

        application.state.scheduler.shutdown(wait=False)

        response = client.get("/health")
        # The process is still serving, so the installer's liveness poll must
        # keep succeeding; the schedule is what degrades.
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["scheduled_checking"]["ok"] is False
        assert response.json()["scheduled_checking"]["state"] == "stopped"

        report = client.get("/support/report.json").json()
        schedule = next(c for c in report["checks"] if c["key"] == "schedule")
        assert schedule["status"] == "blocked"
        assert "not running" in schedule["label"].lower()
        assert "quietly stop updating" in schedule["explanation"]

        page = client.get("/", follow_redirects=True).text
        assert "Automatic checking is not running" in page


def test_the_dashboard_and_the_ready_check_cannot_disagree(tmp_path: pathlib.Path) -> None:
    """Both read one computation, so they move together."""
    application = build(tmp_path)

    with TestClient(application) as client:
        report = client.get("/support/report.json").json()
        schedule = next(c for c in report["checks"] if c["key"] == "schedule")
        page = client.get("/", follow_redirects=True).text

        healthy_states = {"pass", "not_applicable"}
        assert schedule["status"] in healthy_states
        assert "Automatic checking is not running" not in page

        application.state.scheduler.shutdown(wait=False)

        report = client.get("/support/report.json").json()
        schedule = next(c for c in report["checks"] if c["key"] == "schedule")
        page = client.get("/", follow_redirects=True).text

        assert schedule["status"] == "blocked"
        assert "Automatic checking is not running" in page


def test_a_process_that_does_not_own_the_schedule_is_not_reported_broken(
    tmp_path: pathlib.Path,
) -> None:
    application = build(tmp_path, enable_scheduler=False)

    with TestClient(application) as client:
        payload = client.get("/health").json()
        assert payload["ok"] is True
        assert payload["scheduled_checking"]["ok"] is True
        assert payload["scheduled_checking"]["state"] == "unmanaged"

        report = client.get("/support/report.json").json()
        schedule = next(c for c in report["checks"] if c["key"] == "schedule")
        assert schedule["status"] == "not_applicable"


def test_health_never_leaks_a_credential(tmp_path: pathlib.Path) -> None:
    application = build(tmp_path)

    with TestClient(application) as client:
        body = client.get("/health").text

    for secret in ("password", "token", "client_secret"):
        assert secret not in body.lower()


def test_a_behind_schedule_install_warns_on_the_dashboard(tmp_path: pathlib.Path) -> None:
    """Two missed slots must be visible where the user actually looks."""
    import time

    application = build(tmp_path)
    repository = application.state.repository

    with TestClient(application) as client:
        # Starting on a machine that missed a slot correctly kicks off a catch-up
        # scan. Let it settle, then age every completed run: this is an install
        # that has been up for a while with nothing succeeding since.
        for _ in range(150):
            if not application.state.scanner.is_running:
                break
            time.sleep(0.1)
        assert not application.state.scanner.is_running, "catch-up scan never finished"

        stale = previous_scheduled_time(datetime.now(UTC)) - timedelta(hours=3)
        with repository.connection() as connection:
            connection.execute(
                "UPDATE scan_runs SET status='completed', started_at=?, finished_at=?",
                (stale.isoformat(), stale.isoformat()),
            )
            connection.commit()  # connection() closes without committing

        payload = client.get("/health").json()["scheduled_checking"]
        report = client.get("/support/report.json").json()
        schedule = next(c for c in report["checks"] if c["key"] == "schedule")
        page = client.get("/", follow_redirects=True).text

    assert payload["state"] == "overdue"
    assert payload["ok"] is False
    assert schedule["status"] == "attention"
    assert "behind schedule" in page
    assert "Check for new homes now" in schedule["action"]


def test_the_liveness_line_is_absent_before_the_first_check(tmp_path: pathlib.Path) -> None:
    """A new install must not be told it is behind on a schedule it just joined."""
    application = build(tmp_path)

    with TestClient(application) as client:
        page = client.get("/", follow_redirects=True).text

    assert "behind schedule" not in page
    assert "Automatic checking is not running" not in page
