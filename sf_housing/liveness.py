"""One observed answer to "is this thing still checking for me?".

The health endpoint used to answer with a hard-coded schedule string, so it
reported the same thing whether the scheduler was alive or had died at start-up.
Everything that speaks about liveness now derives it here from two observations:
what the scheduler actually holds, and when a scan last finished. Sharing one
computation is the point, so the dashboard, the health endpoint and the Ready
Check cannot disagree with each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .scheduling import PACIFIC, latest_scheduled_time


# Wall-clock hours would drift with daylight saving and with the schedule
# itself. Overdue is defined against the schedule: a scan that predates the slot
# before the most recent one means two chances have now passed.
MISSED_SLOTS_BEFORE_OVERDUE = 2

COMPLETED_STATUSES = {"completed", "completed_with_errors"}


@dataclass(frozen=True, slots=True)
class ScheduleHealth:
    """What is actually true about scheduled checking, right now."""

    managed: bool
    scheduler_running: bool
    job_count: int
    next_run_at: datetime | None
    last_finished_at: datetime | None
    last_status: str | None
    scan_running: bool
    state: str
    summary: str

    @property
    def ok(self) -> bool:
        return self.state in {"scanning", "current", "not_yet", "unmanaged"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state,
            "summary": self.summary,
            "managed": self.managed,
            "scheduler_running": self.scheduler_running,
            "jobs": self.job_count,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_finished_at": self.last_finished_at.isoformat() if self.last_finished_at else None,
            "last_status": self.last_status,
            "scan_running": self.scan_running,
        }


def _parse(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def previous_scheduled_time(now: datetime) -> datetime:
    """The slot before the most recent one, so 'overdue' means two were missed."""
    latest = latest_scheduled_time(now)
    return latest_scheduled_time(latest - timedelta(seconds=1))


def last_finished_scan(scans: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest scan that actually finished, successfully or with source errors.

    A run that is still going, or one that failed before storing anything, is not
    evidence that checking is working.
    """
    for scan in scans:
        if str(scan.get("status")) in COMPLETED_STATUSES and _parse(scan.get("finished_at")):
            return scan
    return None


def describe_age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def schedule_health(
    scheduler: Any,
    scans: list[dict[str, Any]],
    *,
    scan_running: bool = False,
    managed: bool = True,
    now: datetime | None = None,
) -> ScheduleHealth:
    """Observe scheduled checking rather than assert it.

    ``managed`` is false when this process was never asked to own the schedule,
    which is how the test suite and one-off command-line runs behave. That is not
    a fault, so it is reported as its own state rather than as a failure.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    jobs = []
    running = False
    if scheduler is not None:
        try:
            running = bool(getattr(scheduler, "running", False))
            jobs = list(scheduler.get_jobs())
        except Exception:
            # A scheduler that cannot answer is a scheduler that cannot fire.
            jobs, running = [], False
    next_run = min(
        (job.next_run_time for job in jobs if getattr(job, "next_run_time", None)),
        default=None,
    )
    finished = last_finished_scan(scans)
    last_finished_at = _parse(finished.get("finished_at")) if finished else None
    last_status = str(finished.get("status")) if finished else None

    def build(state: str, summary: str) -> ScheduleHealth:
        return ScheduleHealth(
            managed=managed,
            scheduler_running=running,
            job_count=len(jobs),
            next_run_at=next_run.astimezone(UTC) if next_run else None,
            last_finished_at=last_finished_at,
            last_status=last_status,
            scan_running=scan_running,
            state=state,
            summary=summary,
        )

    if not managed:
        return build(
            "unmanaged",
            "Scheduled checking is not owned by this process; scans can still be run by hand.",
        )
    if not running or not jobs:
        return build(
            "stopped",
            "Scheduled checking is not running, so no automatic check will happen.",
        )
    if scan_running:
        return build("scanning", "A source check is running right now.")
    if last_finished_at is None:
        return build(
            "not_yet",
            "Scheduled checking is running; the first check has not finished yet.",
        )
    cutoff = previous_scheduled_time(current)
    if last_finished_at < cutoff:
        return build(
            "overdue",
            f"The last completed check was {describe_age(last_finished_at, current)}, "
            f"which is more than {MISSED_SLOTS_BEFORE_OVERDUE} scheduled checks ago.",
        )
    return build(
        "current",
        f"Last completed check {describe_age(last_finished_at, current)}.",
    )


def next_run_label(health: ScheduleHealth) -> str:
    """Pacific wording for the next check, for people rather than logs."""
    if health.next_run_at is None:
        return "not scheduled"
    return health.next_run_at.astimezone(PACIFIC).strftime("%-I:%M %p on %a")
