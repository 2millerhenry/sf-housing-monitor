from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .scanner import Scanner


PACIFIC = ZoneInfo("America/Los_Angeles")
SCHEDULE_HOURS = (10, 18)


def latest_scheduled_time(now: datetime | None = None) -> datetime:
    """Return the most recent 10:00/18:00 Pacific slot at or before ``now``."""
    current = now or datetime.now(PACIFIC)
    current = current.astimezone(PACIFIC)
    candidates = [
        datetime.combine(current.date() - timedelta(days=day_offset), time(hour), tzinfo=PACIFIC)
        for day_offset in (0, 1)
        for hour in SCHEDULE_HOURS
    ]
    return max(candidate for candidate in candidates if candidate <= current)


def startup_scan_due(recent_scans: list[dict], now: datetime | None = None) -> bool:
    """Catch up after a reboot without re-scanning on every process restart."""
    latest_due_utc = latest_scheduled_time(now).astimezone(UTC)
    for scan in recent_scans:
        if scan.get("status") not in {"completed", "completed_with_errors"}:
            continue
        timestamp = scan.get("started_at")
        if not timestamp:
            continue
        try:
            started = datetime.fromisoformat(str(timestamp))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            continue
        if started.astimezone(UTC) >= latest_due_utc:
            return False
    return True


def build_scheduler(scanner: Scanner) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=PACIFIC)
    scheduler.add_job(
        scanner.run_scan,
        CronTrigger(hour="10,18", minute=0, timezone=PACIFIC),
        args=["scheduled"],
        id="housing-scans-pacific",
        name="Housing scans at 10:00 and 18:00 Pacific",
        replace_existing=True,
        # A sleeping Mac can miss one or both times. Run only the newest missed
        # occurrence after wake, while the startup catch-up covers a reboot.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=18 * 60 * 60,
    )
    return scheduler
