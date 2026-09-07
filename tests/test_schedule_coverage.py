"""The schedule reporting on itself.

"Every due slot gets served" was a claim about wall-clock time nobody could
see, so a schedule quietly running half the time looked exactly like one running
properly -- which is how eight due runs became four without a word on any page.
This turns it into a number, which means the number has to be right: an
overcount accuses the app of faults it did not have, and an undercount hides the
one thing this exists to expose.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sf_housing.scheduling import (
    PACIFIC,
    SLOT_GRACE,
    ScheduleCoverage,
    schedule_coverage,
    scheduled_slots,
)


def pacific(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=PACIFIC)


def scan(when: datetime, status="completed", trigger="scheduled"):
    return {"status": status, "started_at": when.astimezone(UTC).isoformat(), "trigger": trigger}


# --------------------------------------------------------------------------
# which slots were due
# --------------------------------------------------------------------------


def test_a_week_holds_fourteen_slots() -> None:
    now = pacific(2026, 9, 8, 12)
    slots = scheduled_slots(now - timedelta(days=7), now)

    assert len(slots) == 14
    assert all(slot.hour in (10, 18) for slot in slots)
    assert slots == sorted(slots)


def test_slots_are_pacific_across_a_daylight_saving_change() -> None:
    """The schedule is 10:00 and 18:00 where the reader lives, not UTC."""
    slots = scheduled_slots(pacific(2026, 10, 30, 0), pacific(2026, 11, 3, 23))

    assert {slot.hour for slot in slots} == {10, 18}
    offsets = {slot.utcoffset() for slot in slots}
    assert len(offsets) == 2, "the window has to span the change to be worth testing"


# --------------------------------------------------------------------------
# what counts as served
# --------------------------------------------------------------------------


def test_a_perfect_week_says_so() -> None:
    now = pacific(2026, 9, 8, 12)
    scans = [scan(slot + timedelta(minutes=1)) for slot in scheduled_slots(now - timedelta(days=7), now)]

    coverage = schedule_coverage(scans, now=now)

    assert coverage.complete
    assert coverage.served == coverage.due
    assert coverage.missed == ()
    assert "Every one of the last" in coverage.summary()


def test_a_missed_slot_is_named() -> None:
    now = pacific(2026, 9, 5, 20)
    slots = scheduled_slots(now - timedelta(days=2), now)
    skipped = slots[1]
    scans = [scan(slot + timedelta(minutes=2)) for slot in slots if slot != skipped]

    coverage = schedule_coverage(scans, now=now)

    assert coverage.served == coverage.due - 1
    assert coverage.missed == (skipped,)
    assert not coverage.complete
    assert f"{coverage.served} of the last {coverage.due}" in coverage.summary()


def test_a_scan_run_by_hand_serves_the_slot_it_falls_in() -> None:
    """The question is whether checking happened, not whether cron caused it."""
    now = pacific(2026, 9, 5, 20)
    slots = scheduled_slots(now - timedelta(days=1), now)
    scans = [scan(slot + timedelta(minutes=25), trigger="manual") for slot in slots]

    assert schedule_coverage(scans, now=now).complete


def test_a_catch_up_ninety_minutes_late_still_serves_its_slot() -> None:
    now = pacific(2026, 9, 5, 20)
    slots = scheduled_slots(now - timedelta(days=1), now)
    scans = [scan(slot + timedelta(minutes=90), trigger="catch_up") for slot in slots]

    assert schedule_coverage(scans, now=now).complete


def test_a_scan_just_before_a_slot_does_not_serve_it() -> None:
    """09:50 is not the ten o'clock check; counting it would let the app claim
    coverage it never had."""
    now = pacific(2026, 9, 5, 20)
    slots = scheduled_slots(now - timedelta(days=1), now)
    scans = [scan(slot - timedelta(minutes=10)) for slot in slots]

    coverage = schedule_coverage(scans, now=now)

    assert coverage.served < coverage.due


def test_one_scan_cannot_serve_two_slots() -> None:
    now = pacific(2026, 9, 5, 20)
    slots = scheduled_slots(now - timedelta(days=1), now)
    scans = [scan(slots[0] + timedelta(minutes=5))]

    coverage = schedule_coverage(scans, now=now)

    assert coverage.served == 1, "a single scan serves the slot it lands in, and no other"


@pytest.mark.parametrize("status", ["failed", "running", "skipped"])
def test_only_a_completed_scan_serves_a_slot(status: str) -> None:
    """A run that started and died is not a check that happened."""
    now = pacific(2026, 9, 5, 20)
    slots = scheduled_slots(now - timedelta(days=1), now)
    scans = [scan(slot + timedelta(minutes=2), status=status) for slot in slots]
    scans.append(scan(slots[0] + timedelta(minutes=1)))  # one real one, so there is history

    coverage = schedule_coverage(scans, now=now)

    assert coverage.served == 1


def test_completed_with_errors_still_counts() -> None:
    """A source failing is not the schedule failing."""
    now = pacific(2026, 9, 5, 20)
    slots = scheduled_slots(now - timedelta(days=1), now)
    scans = [scan(slot + timedelta(minutes=2), status="completed_with_errors") for slot in slots]

    assert schedule_coverage(scans, now=now).complete


# --------------------------------------------------------------------------
# the two ways this could lie
# --------------------------------------------------------------------------


def test_a_fresh_install_does_not_accuse_itself_of_a_missed_week() -> None:
    """Counting slots from before the app existed would report twelve failures
    on day one, which is both false and the fastest way to teach somebody to
    ignore this number."""
    now = pacific(2026, 9, 5, 20)
    scans = [scan(now - timedelta(hours=3))]

    coverage = schedule_coverage(scans, now=now)

    # Two slots exist today; only the ones from the app's own lifetime are
    # judged, so this is never the twelve a full week would hold.
    assert coverage.due <= 2, coverage
    assert all(slot >= coverage.since for slot in coverage.missed)


def test_a_slot_that_has_only_just_passed_is_not_yet_a_miss() -> None:
    """At 10:02 the cron may be mid-run and the catch-up has not had its turn.
    Accusing it then would make the number flicker twice a day, every day."""
    now = pacific(2026, 9, 5, 10, 2)
    yesterday = scheduled_slots(now - timedelta(days=1), now - timedelta(hours=1))
    scans = [scan(slot + timedelta(minutes=2)) for slot in yesterday]

    coverage = schedule_coverage(scans, now=now)

    assert coverage.complete, coverage.summary()
    assert all(now - slot.astimezone(UTC) >= SLOT_GRACE for slot in scheduled_slots(coverage.since, now)[: coverage.due])


def test_the_grace_expires_and_a_real_miss_appears() -> None:
    now = pacific(2026, 9, 5, 12)
    yesterday = scheduled_slots(now - timedelta(days=1), now - timedelta(hours=3))
    scans = [scan(slot + timedelta(minutes=2)) for slot in yesterday]

    coverage = schedule_coverage(scans, now=now)

    assert not coverage.complete, "the ten o'clock slot is long past and was not served"
    assert any(slot.hour == 10 for slot in coverage.missed)


# --------------------------------------------------------------------------
# junk in the history
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        {"status": "completed"},
        {"status": "completed", "started_at": ""},
        {"status": "completed", "started_at": "not a date"},
        {"status": "completed", "started_at": None},
        {},
    ],
)
def test_an_unreadable_row_never_breaks_the_report(row: dict) -> None:
    assert isinstance(schedule_coverage([row], now=pacific(2026, 9, 5, 20)), ScheduleCoverage)


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Older rows were written without a timezone."""
    now = pacific(2026, 9, 5, 20)
    slots = scheduled_slots(now - timedelta(days=1), now)
    scans = [
        {
            "status": "completed",
            "started_at": (slot + timedelta(minutes=2)).astimezone(UTC).replace(tzinfo=None).isoformat(),
        }
        for slot in slots
    ]

    assert schedule_coverage(scans, now=now).complete


# --------------------------------------------------------------------------
# what the Support page does with it
# --------------------------------------------------------------------------


def support_app(tmp_path, scans=()):
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.settings import Settings
    from tests.conftest import TEST_PREFERENCES

    data = tmp_path / "data"
    (data / "config").mkdir(parents=True, exist_ok=True)
    preferences_path = data / "config" / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences_path,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for when in scans:
        run_id = repository.begin_scan("scheduled")
        with repository.connection() as connection:
            connection.execute(
                "UPDATE scan_runs SET started_at = ?, finished_at = ?, status = 'completed' WHERE id = ?",
                (when.astimezone(UTC).isoformat(), when.astimezone(UTC).isoformat(), run_id),
            )
            connection.commit()
    return application, TestClient


def schedule_check(payload):
    return next(check for check in payload["checks"] if check["key"] == "schedule")


def test_support_reports_the_schedule_record_rather_than_asking_to_be_trusted(tmp_path) -> None:
    """The whole point: a run of missed slots is invisible in "last checked an
    hour ago", and that is exactly the fault this mechanism exists to catch."""
    now = datetime.now(UTC)
    slots = scheduled_slots(now - timedelta(days=3), now - SLOT_GRACE - timedelta(minutes=5))
    served = [slot + timedelta(minutes=2) for slot in slots[:-2]]
    application, client_cls = support_app(tmp_path, served)

    with client_cls(application) as client:
        payload = client.get("/support/report.json").json()

    check = schedule_check(payload)
    coverage = check["metadata"]["coverage"]
    assert coverage["due"] > coverage["served"], coverage
    assert coverage["missed"], "the slots that did not run have to be named"


def a_week_of_slots(days: int = 5) -> tuple[list, "datetime"]:
    """Every slot in the last `days`, and a `now` sitting just after the last.

    Taken straight from the clock, the newest slot can be sixteen hours back --
    the schedule sits at 10am and 6pm, so the gap between slots alternates
    between eight hours and sixteen -- and the page answers "checks are behind
    schedule" before it ever looks at coverage. These tests were therefore
    passing or failing on what time of day the suite happened to run.
    Anchoring `now` to the last slot makes them say the same thing at any hour.
    """
    slots = scheduled_slots(datetime.now(UTC) - timedelta(days=days), datetime.now(UTC))
    assert slots, "the schedule must produce slots for the window"
    return slots, slots[-1] + SLOT_GRACE + timedelta(minutes=5)


def test_a_complete_record_says_so_and_stays_a_pass(tmp_path) -> None:
    slots, _ = a_week_of_slots(3)
    application, client_cls = support_app(tmp_path, [slot + timedelta(minutes=2) for slot in slots])

    with client_cls(application) as client:
        payload = client.get("/support/report.json").json()

    check = schedule_check(payload)
    coverage = check["metadata"]["coverage"]
    assert coverage["served"] == coverage["due"] > 0
    assert coverage["missed"] == []


def test_a_fresh_install_is_not_told_it_missed_anything(tmp_path) -> None:
    application, client_cls = support_app(tmp_path, [])

    with client_cls(application) as client:
        payload = client.get("/support/report.json").json()

    check = schedule_check(payload)
    assert check["status"] != "attention"
    assert check["metadata"]["coverage"]["missed"] == []


def test_the_record_comes_from_the_same_history_as_the_state(tmp_path) -> None:
    """Two numbers on one page computed from different fetches is how they come
    to disagree."""
    import inspect

    from sf_housing import diagnostics

    source = inspect.getsource(diagnostics._schedule_check)
    assert source.count("recent_scans(") == 1, "one fetch feeds both answers"
    assert "recent[:8]" in source


# --------------------------------------------------------------------------
# proportion: an alarm nobody can act on is worse than no alarm
# --------------------------------------------------------------------------


def test_one_old_miss_is_stated_not_raised() -> None:
    """Its own advice is "nothing, if the Mac is often closed". Turning the
    whole page amber for that teaches people to ignore the page."""
    now = datetime.now(UTC)
    coverage = ScheduleCoverage(9, 8, (now.astimezone(PACIFIC) - timedelta(days=3),), now.astimezone(PACIFIC))

    assert coverage.worth_raising(now=now) is False
    assert not coverage.complete, "it is still counted and still reported"


def test_a_miss_in_the_last_day_is_worth_raising() -> None:
    now = datetime.now(UTC)
    coverage = ScheduleCoverage(9, 8, (now.astimezone(PACIFIC) - timedelta(hours=6),), now.astimezone(PACIFIC))

    assert coverage.worth_raising(now=now) is True


def test_a_pattern_of_misses_is_worth_raising_however_old() -> None:
    now = datetime.now(UTC)
    missed = (
        now.astimezone(PACIFIC) - timedelta(days=5),
        now.astimezone(PACIFIC) - timedelta(days=4),
    )
    coverage = ScheduleCoverage(9, 7, missed, now.astimezone(PACIFIC))

    assert coverage.worth_raising(now=now) is True


def test_a_complete_record_raises_nothing() -> None:
    now = datetime.now(UTC)
    assert ScheduleCoverage(14, 14, (), now.astimezone(PACIFIC)).worth_raising(now=now) is False


def managed_schedule_check(tmp_path, scans, now=None):
    """The check as the installed app runs it: managed, with a live scan job.

    The test app runs unmanaged, which returns "this process does not keep the
    schedule" before any of this is reached -- correct, and the reason the
    sentence a person actually reads needs asserting here rather than through
    the page.
    """
    from sf_housing.diagnostics import _schedule_check
    from sf_housing.scheduling import SCAN_JOB_ID

    application, _ = support_app(tmp_path, scans)

    class Job:
        id = SCAN_JOB_ID
        next_run_time = (now or datetime.now(UTC)) + timedelta(hours=4)

    class Alive:
        running = True

        def get_jobs(self):
            return [Job()]

    return _schedule_check(
        application.state.repository,
        application.state.scanner,
        Alive(),
        True,
        now or datetime.now(UTC),
    )


def test_an_old_miss_still_appears_as_a_fact(tmp_path) -> None:
    """Not raising it must not mean hiding it."""
    slots, now = a_week_of_slots()
    # Deliberately not the first: the window floors at the app's own history, so
    # a slot before the earliest scan is correctly never judged at all.
    skipped = slots[1]
    check = managed_schedule_check(
        tmp_path, [slot + timedelta(minutes=2) for slot in slots if slot != skipped], now
    )

    assert check.status == "pass", f"one old, caught-up miss is not an alarm: {check.explanation}"
    assert "caught up" in check.explanation
    assert check.metadata["coverage"]["missed"], "and it is still recorded"


def test_a_recent_miss_does_raise_on_the_page(tmp_path) -> None:
    """A slot missed recently, on a week otherwise kept.

    Every date here is derived rather than taken from the clock. The slots sit
    at 10am and 6pm, so the gap between them alternates between eight hours and
    sixteen, and "the slot before last" was inside a day or outside it
    depending on what time the suite happened to run. Anchoring on a 10am slot
    makes the previous slot sixteen hours back every time.

    The missed slot is also deliberately not the most recent one: dropping that
    leaves the newest scan half a day old, and "checks are behind schedule"
    answers before coverage is ever consulted.
    """
    slots, _ = a_week_of_slots(6)
    latest = next(slot for slot in reversed(slots) if slot.astimezone(PACIFIC).hour == 10)
    now = latest + SLOT_GRACE + timedelta(minutes=5)
    due = [slot for slot in slots if slot <= latest]
    assert len(due) >= 4, "six days must span several slots"
    missed = due[-2]
    assert now - missed < timedelta(days=1), "a lone old miss is not worth raising, by design"

    check = managed_schedule_check(
        tmp_path, [slot + timedelta(minutes=2) for slot in due if slot != missed], now
    )

    assert check.status == "attention", check.explanation
    assert "did not run" in check.label, check.label
    assert "Missed:" in check.explanation


def test_a_complete_record_reads_as_a_pass_and_says_so(tmp_path) -> None:
    slots, now = a_week_of_slots()
    check = managed_schedule_check(tmp_path, [slot + timedelta(minutes=2) for slot in slots], now)

    assert check.status == "pass"
    assert "Every one of the last" in check.explanation


def test_a_missed_slot_is_named_with_its_date(tmp_path) -> None:
    """"10am thu" is ambiguous across a week-long window."""
    from sf_housing.diagnostics import _slot_label

    label = _slot_label(datetime(2026, 9, 3, 10, 0, tzinfo=PACIFIC))

    assert label == "10am Thu Sep 3", label
