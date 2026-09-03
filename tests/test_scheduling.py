from __future__ import annotations

from datetime import UTC, datetime

from sf_housing.scheduling import build_scheduler, latest_scheduled_time, startup_scan_due


class FakeScanner:
    def run_scan(self, trigger="manual"):
        return trigger


def test_scheduler_has_twice_daily_pacific_cron_job() -> None:
    scheduler = build_scheduler(FakeScanner())
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {"housing-scans-pacific"}
    job = jobs["housing-scans-pacific"]
    assert "hour='10,18'" in str(job.trigger)
    assert str(job.trigger.timezone) == "America/Los_Angeles"
    assert job.coalesce is True
    assert job.misfire_grace_time == 18 * 60 * 60


def test_latest_scheduled_time_uses_pacific_and_daylight_saving() -> None:
    now = datetime(2026, 7, 20, 17, 0, tzinfo=UTC)  # 10:00 PDT
    latest = latest_scheduled_time(now)

    assert latest.hour == 10
    assert latest.tzinfo is not None
    assert latest.astimezone(UTC) == now


def test_startup_catches_up_only_when_latest_slot_was_missed() -> None:
    now = datetime(2026, 7, 20, 21, 0, tzinfo=UTC)  # 14:00 PDT; latest slot is 10:00

    assert startup_scan_due([], now) is True
    assert startup_scan_due(
        [{"status": "completed", "started_at": "2026-07-20T17:01:00+00:00"}], now
    ) is False
    assert startup_scan_due(
        [{"status": "failed", "started_at": "2026-07-20T18:00:00+00:00"}], now
    ) is True
