from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import logging

from .scanner import Scanner


LOGGER = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
SCHEDULE_HOURS = (10, 18)

# How often to ask whether a due slot went unserved. The cron job below is the
# normal path; this is what covers the cases it cannot see. Over the first four
# days of this app, eight scheduled runs were due and four happened -- a
# schedule that silently runs half the time is worse than a manual button,
# because it is trusted.
#
# Fifteen minutes is chosen against the cost of being wrong in either
# direction: a missed 10:00 check is served by 10:15 at the latest, and the
# question itself is one SQLite read of recent scans, so asking 96 times a day
# costs nothing.
CATCH_UP_INTERVAL_MINUTES = 15

# Named so liveness can tell the two jobs apart. "Next check" means the next
# real scan; the heartbeat runs every fifteen minutes and is not one, so a
# dashboard that took the soonest job would have started promising a check at
# 12:07 instead of 18:00.
SCAN_JOB_ID = "housing-scans-pacific"
CATCH_UP_JOB_ID = "housing-catch-up"


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


def scheduled_scan_due(recent_scans: list[dict], now: datetime | None = None) -> bool:
    """Has the most recent 10:00/18:00 slot gone by without a completed scan?

    Asked at start-up and then every quarter of an hour. Deliberately about the
    latest slot only: after three days asleep the answer is one scan, not six,
    so this can never queue up history.
    """
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


def catch_up_if_due(scanner: Scanner) -> bool:
    """Run one scan if a scheduled slot went unserved. Returns whether it did.

    The cron job is the normal path. It fires only while this process is alive
    and the Mac is awake, and APScheduler keeps its schedule in memory, so a
    restart after a missed slot recomputes the next run from now and that slot
    is gone. This asks the database instead of the scheduler, so it does not
    care why the slot was missed -- sleep, a crash, a reinstall, or a machine
    that was simply off.

    Every failure here is swallowed on purpose: a heartbeat that raises would be
    logged and dropped by APScheduler, and its job is to make scanning more
    reliable, never less.
    """
    try:
        if scanner.is_running:
            return False
        if not scanner.preference_loader().profile_active:
            return False
        if not scheduled_scan_due(scanner.repository.recent_scans(20)):
            return False
        started = scanner.start_scan("catch_up")
        if started:
            LOGGER.info("Catch-up scan started for a scheduled slot that did not run")
        return started
    except Exception:  # pragma: no cover - defensive, see docstring
        LOGGER.warning("Catch-up check failed", exc_info=True)
        return False


def build_scheduler(scanner: Scanner) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=PACIFIC)
    scheduler.add_job(
        scanner.run_scan,
        CronTrigger(hour="10,18", minute=0, timezone=PACIFIC),
        args=["scheduled"],
        id=SCAN_JOB_ID,
        name="Housing scans at 10:00 and 18:00 Pacific",
        replace_existing=True,
        # A sleeping Mac can miss one or both times. Run only the newest missed
        # occurrence after wake, while the startup catch-up covers a reboot.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=18 * 60 * 60,
    )
    # The safety net under that job. It asks the database what actually ran
    # rather than trusting the scheduler's own memory of what it meant to run.
    scheduler.add_job(
        catch_up_if_due,
        IntervalTrigger(minutes=CATCH_UP_INTERVAL_MINUTES, timezone=PACIFIC),
        args=[scanner],
        id=CATCH_UP_JOB_ID,
        name="Catch up a missed scheduled check",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
