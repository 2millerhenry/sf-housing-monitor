from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import logging

from .scanner import DEEP_SWEEP_TRIGGER, Scanner


LOGGER = logging.getLogger(__name__)

from .freshness import PACIFIC  # one definition, re-exported for existing callers
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
DEEP_SWEEP_JOB_ID = "housing-deep-sweep"
DEEP_SWEEP_CATCH_UP_JOB_ID = "housing-deep-sweep-catch-up"

# When the nightly deep sweep runs. Most sources are read in full on every
# scan because doing so costs seconds; two cannot be. Trulia and Redfin answer
# 403 and 202 once they have had enough, so they are read shallowly at 10:00
# and 18:00 -- when a shallow source that works beats a deep one that is turned
# away -- and to the bottom once a day at an hour where being refused costs a
# run nobody is watching. 03:20 rather than 03:00: the hour itself is when
# every other scheduled thing on a machine fires.
DEEP_SWEEP_HOUR = 3
DEEP_SWEEP_MINUTE = 20

# A sweep is worth catching up, but only once the day it belonged to is gone.
# The original reasoning against catching one up was that running eight hours
# late is worth less than the next one on time -- true, and it assumed there
# would be a next one. On a Mac that is asleep at 03:20 there never is: over
# this app's whole history the nightly sweep has run zero times. So the cron
# stays the normal path, and this is the floor under it: a day and a bit,
# so a machine that is awake at 03:20 always uses the cron and one that is
# not still gets a sweep rather than none.
DEEP_SWEEP_MAX_AGE = timedelta(hours=26)

# How far back the schedule reports on itself. A week is long enough to expose a
# pattern and short enough that a fault shows up while it still matters.
COVERAGE_WINDOW = timedelta(days=7)

# A slot that has only just passed is not yet a miss: the cron fires on the hour
# and the catch-up follows within its interval, so a page loaded at 10:02 must
# not accuse the app of missing a check it is in the middle of running.
SLOT_GRACE = timedelta(minutes=CATCH_UP_INTERVAL_MINUTES + 10)


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


@dataclass(frozen=True, slots=True)
class ScheduleCoverage:
    """How many due checks actually happened, over a recent window.

    "Every due slot gets served" was a claim about wall-clock time that nobody
    could see, so a schedule quietly running half the time looked exactly like
    one running properly. This makes it a number on a page.
    """

    due: int
    served: int
    missed: tuple[datetime, ...]
    since: datetime

    @property
    def complete(self) -> bool:
        return self.due > 0 and self.served == self.due

    @property
    def measurable(self) -> bool:
        """False on a fresh install, where there is nothing to report on yet."""
        return self.due > 0

    def worth_raising(self, *, now: datetime | None = None) -> bool:
        """Is this a fault to raise, or a fact to state?

        One slot missed days ago, already caught up, is not something to act on
        -- the advice for it is "nothing, if the Mac is often closed". Turning
        the whole Support page amber for that trains people to ignore the page,
        which costs more than the miss did. A pattern is different: two or more
        in a week, or one in the last day, is worth a look.
        """
        if not self.missed:
            return False
        if len(self.missed) >= 2:
            return True
        current = (now or datetime.now(UTC)).astimezone(UTC)
        return current - self.missed[-1].astimezone(UTC) < timedelta(days=1)

    def summary(self) -> str:
        if not self.measurable:
            return "Not enough history yet to report on the schedule."
        if self.complete:
            return f"Every one of the last {self.due} scheduled checks ran."
        return f"{self.served} of the last {self.due} scheduled checks ran."


def scheduled_slots(start: datetime, end: datetime) -> list[datetime]:
    """Every 10:00/18:00 Pacific slot in ``[start, end]``, oldest first."""
    first = start.astimezone(PACIFIC)
    last = end.astimezone(PACIFIC)
    slots: list[datetime] = []
    day = first.date() - timedelta(days=1)
    while day <= last.date():
        for hour in SCHEDULE_HOURS:
            slot = datetime.combine(day, time(hour), tzinfo=PACIFIC)
            if first <= slot <= last:
                slots.append(slot)
        day += timedelta(days=1)
    return sorted(slots)


def schedule_coverage(
    scans: list[dict],
    *,
    now: datetime | None = None,
    window: timedelta = COVERAGE_WINDOW,
) -> ScheduleCoverage:
    """Count the slots that were due against the ones a scan actually served.

    A slot counts as served by any completed scan that started between it and
    the next slot, whatever triggered it: the question is whether checking
    happened, not whether cron was the thing that caused it. A scan run by hand
    at 10:05 genuinely served the ten o'clock check.

    Slots before the app's own history begins are not counted, so a fresh
    install does not accuse itself of missing a week of checks it could not
    have run.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    completed = sorted(
        moment
        for moment in (
            _scan_started(scan)
            for scan in scans
            if str(scan.get("status")) in {"completed", "completed_with_errors"}
        )
        if moment is not None
    )
    if not completed:
        return ScheduleCoverage(0, 0, (), current.astimezone(PACIFIC))

    # Never report on time before this app was doing anything -- but floor to
    # the slot that first scan belongs to, not to its clock time. A scan at
    # 10:05 served the ten o'clock check, and flooring at 10:05 would put that
    # slot outside the window and score its own scan as serving nothing.
    since = max(current - window, latest_scheduled_time(completed[0]).astimezone(UTC))
    slots = [
        slot
        for slot in scheduled_slots(since, current)
        # The newest slot is still in flight until the catch-up has had its turn.
        if current - slot.astimezone(UTC) >= SLOT_GRACE
    ]
    if not slots:
        return ScheduleCoverage(0, 0, (), since.astimezone(PACIFIC))

    missed: list[datetime] = []
    served = 0
    for index, slot in enumerate(slots):
        opens = slot.astimezone(UTC)
        closes = (
            slots[index + 1].astimezone(UTC) if index + 1 < len(slots) else current
        )
        if any(opens <= moment < closes for moment in completed):
            served += 1
        else:
            missed.append(slot)
    return ScheduleCoverage(len(slots), served, tuple(missed), since.astimezone(PACIFIC))


def _scan_started(scan: dict) -> datetime | None:
    text = str(scan.get("started_at") or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


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


def deep_sweep_due(recent_scans: list[dict], now: datetime | None = None) -> bool:
    """Has it been more than a day since a deep sweep finished?

    Asked on the same heartbeat as the scheduled-slot check. Deliberately about
    age rather than about a slot: a sweep collects the long tail, so what
    matters is that one happened recently, not which night it belonged to.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    for scan in recent_scans:
        if str(scan.get("trigger") or "") != DEEP_SWEEP_TRIGGER:
            continue
        if scan.get("status") not in {"completed", "completed_with_errors"}:
            continue
        timestamp = scan.get("started_at")
        if not timestamp:
            continue
        try:
            started = datetime.fromisoformat(str(timestamp))
        except ValueError:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if current - started.astimezone(UTC) < DEEP_SWEEP_MAX_AGE:
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


def sweep_if_due(scanner: Scanner) -> bool:
    """Run the nightly sweep if a day has gone by without one.

    Its own job rather than part of the catch-up heartbeat, because the two
    answer different questions: that one is "is the shortlist current", this is
    "has the long tail been collected lately". Sharing a function would have
    made a fresh install's first heartbeat start a fifteen-minute sweep.

    Failures are swallowed for the same reason they are there: a heartbeat that
    raises is logged and dropped, and its job is to make scanning more
    reliable, never less.
    """
    try:
        if scanner.is_running:
            return False
        if not scanner.preference_loader().profile_active:
            return False
        recent = scanner.repository.recent_scans(40)
        # A current shortlist beats a complete tail: if an ordinary check is
        # owed, that runs first and this waits for the next heartbeat.
        if scheduled_scan_due(recent):
            return False
        if not deep_sweep_due(recent):
            return False
        started = scanner.start_scan(DEEP_SWEEP_TRIGGER)
        if started:
            LOGGER.info("Deep sweep started; the nightly one did not run")
        return started
    except Exception:  # pragma: no cover - defensive, see docstring
        LOGGER.warning("Deep sweep check failed", exc_info=True)
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
    # Once a day, read the sources that cannot be read deeply the rest of the
    # time. Deliberately not caught up if it is missed: a sweep is how the
    # long tail is collected, not how anything stays current, so running one
    # eight hours late on wake is worth less than the next one on time.
    scheduler.add_job(
        scanner.run_scan,
        CronTrigger(
            hour=DEEP_SWEEP_HOUR, minute=DEEP_SWEEP_MINUTE, timezone=PACIFIC
        ),
        args=[DEEP_SWEEP_TRIGGER],
        id=DEEP_SWEEP_JOB_ID,
        name="Nightly deep sweep of the rate-limited sources",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=2 * 60 * 60,
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
    # The same safety net under the sweep. Hourly rather than quarter-hourly:
    # what it is checking changes once a day.
    scheduler.add_job(
        sweep_if_due,
        IntervalTrigger(hours=1, timezone=PACIFIC),
        args=[scanner],
        id=DEEP_SWEEP_CATCH_UP_JOB_ID,
        name="Catch up a missed deep sweep",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
