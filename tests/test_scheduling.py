from __future__ import annotations

from datetime import UTC, datetime

from sf_housing.scheduling import build_scheduler, latest_scheduled_time, scheduled_scan_due


class FakeScanner:
    def run_scan(self, trigger="manual"):
        return trigger


def test_scheduler_has_twice_daily_pacific_cron_job() -> None:
    scheduler = build_scheduler(FakeScanner())
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {"housing-scans-pacific", "housing-catch-up", "housing-deep-sweep"}
    job = jobs["housing-scans-pacific"]
    assert "hour='10,18'" in str(job.trigger)
    assert str(job.trigger.timezone) == "America/Los_Angeles"
    assert job.coalesce is True
    assert job.misfire_grace_time == 18 * 60 * 60

    heartbeat = jobs["housing-catch-up"]
    assert "interval" in str(heartbeat.trigger)
    assert heartbeat.coalesce is True
    assert heartbeat.max_instances == 1


def test_latest_scheduled_time_uses_pacific_and_daylight_saving() -> None:
    now = datetime(2026, 7, 20, 17, 0, tzinfo=UTC)  # 10:00 PDT
    latest = latest_scheduled_time(now)

    assert latest.hour == 10
    assert latest.tzinfo is not None
    assert latest.astimezone(UTC) == now


def test_startup_catches_up_only_when_latest_slot_was_missed() -> None:
    now = datetime(2026, 7, 20, 21, 0, tzinfo=UTC)  # 14:00 PDT; latest slot is 10:00

    assert scheduled_scan_due([], now) is True
    assert scheduled_scan_due(
        [{"status": "completed", "started_at": "2026-07-20T17:01:00+00:00"}], now
    ) is False
    assert scheduled_scan_due(
        [{"status": "failed", "started_at": "2026-07-20T18:00:00+00:00"}], now
    ) is True


def test_the_nightly_deep_sweep_runs_once_a_day_at_a_quiet_hour() -> None:
    """Most sources are read in full on every scan because it costs seconds.
    Trulia and Redfin cannot be: they answer 403 and 202 once they have had
    enough, and a source that has been turned away returns nothing at all. So
    they are read shallowly when somebody is waiting and to the bottom once a
    day, when being refused costs a run nobody is watching."""
    from sf_housing.scanner import DEEP_SWEEP_TRIGGER

    jobs = {job.id: job for job in build_scheduler(FakeScanner()).get_jobs()}
    sweep = jobs["housing-deep-sweep"]

    assert list(sweep.args) == [DEEP_SWEEP_TRIGGER]
    assert "hour='3'" in str(sweep.trigger)
    assert str(sweep.trigger.timezone) == "America/Los_Angeles"
    assert sweep.max_instances == 1


def test_a_missed_sweep_is_not_caught_up_hours_later() -> None:
    """A sweep collects the long tail; it is not how anything stays current.
    Run eight hours late on wake it competes with the scan that is due, and
    the next one on time is worth more."""
    jobs = {job.id: job for job in build_scheduler(FakeScanner()).get_jobs()}

    assert jobs["housing-deep-sweep"].misfire_grace_time < (
        jobs["housing-scans-pacific"].misfire_grace_time
    )


def test_the_deep_sweep_reaches_every_source_a_schedule_would() -> None:
    """Read as a lesser kind of run it would skip the scheduled-only sources,
    and the sweep would quietly cover less than the scans it supplements."""
    from sf_housing.scanner import AUTOMATIC_TRIGGERS, DEEP_SWEEP_TRIGGER, FULL_SOURCE_TRIGGERS

    assert DEEP_SWEEP_TRIGGER in AUTOMATIC_TRIGGERS
    assert DEEP_SWEEP_TRIGGER in FULL_SOURCE_TRIGGERS
