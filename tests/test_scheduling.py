from __future__ import annotations

from datetime import UTC, datetime

from sf_housing.scheduling import build_scheduler, latest_scheduled_time, scheduled_scan_due


class FakeScanner:
    def run_scan(self, trigger="manual"):
        return trigger


def test_scheduler_has_twice_daily_pacific_cron_job() -> None:
    scheduler = build_scheduler(FakeScanner())
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {
        "housing-scans-pacific",
        "housing-catch-up",
        "housing-deep-sweep",
        # The cron above only fires while this Mac is awake at 03:20, which
        # over this app's history it never was. This is the floor under it.
        "housing-deep-sweep-catch-up",
    }
    job = jobs["housing-scans-pacific"]
    assert "hour='10,18'" in str(job.trigger)
    assert str(job.trigger.timezone) == "America/Los_Angeles"
    assert job.coalesce is True
    assert job.misfire_grace_time == 18 * 60 * 60

    heartbeat = jobs["housing-catch-up"]
    assert "interval" in str(heartbeat.trigger)
    assert heartbeat.coalesce is True
    assert heartbeat.max_instances == 1

    sweep_net = jobs["housing-deep-sweep-catch-up"]
    assert "interval" in str(sweep_net.trigger)
    assert sweep_net.coalesce is True
    assert sweep_net.max_instances == 1


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


def test_a_scan_can_never_run_longer_than_four_minutes() -> None:
    """The ceiling somebody watching a progress bar will tolerate. A scan that
    would run past it stops and records which sources it did not reach, which
    is the honest outcome: the nightly sweep is what collects the rest."""
    from sf_housing.settings import Settings

    assert Settings.from_environment().scan_max_seconds <= 240


def test_no_single_source_may_use_more_than_a_third_of_a_scan() -> None:
    """The per-source ceiling has to leave room for the other twenty-two. Set
    at or above the scan's own budget it stops being a bound at all."""
    from sf_housing.scanner import SOURCE_HARD_CEILING_SECONDS
    from sf_housing.settings import Settings

    assert SOURCE_HARD_CEILING_SECONDS < Settings.from_environment().scan_max_seconds / 2


# --------------------------------------------------------------------------
# the nightly sweep, on a Mac that is asleep at 03:20
# --------------------------------------------------------------------------


def swept(hours_ago: float, trigger: str = "deep_sweep", status: str = "completed") -> dict:
    from datetime import UTC, datetime, timedelta

    return {
        "trigger": trigger,
        "status": status,
        "started_at": (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(),
    }


def test_a_sweep_that_never_ran_is_due() -> None:
    """The cron fires only while this process is alive and the Mac is awake.
    Over this app's whole history that never coincided with 03:20, so the
    nightly sweep ran zero times and the long tail was never collected."""
    from sf_housing.scheduling import deep_sweep_due

    assert deep_sweep_due([]) is True
    assert deep_sweep_due([swept(2, trigger="scheduled")]) is True


def test_a_sweep_from_last_night_is_not_due_again() -> None:
    """It is a day's work, not an hourly one. Asking twice in a night would
    spend the sources' patience for nothing."""
    from sf_housing.scheduling import deep_sweep_due

    assert deep_sweep_due([swept(2)]) is False
    assert deep_sweep_due([swept(25)]) is False


def test_a_sweep_older_than_a_day_and_a_bit_is_due(monkeypatch) -> None:
    """The window is a day plus slack, so a machine awake at 03:20 always uses
    the cron and only one that is not falls through to here."""
    from sf_housing.scheduling import DEEP_SWEEP_MAX_AGE, deep_sweep_due

    assert DEEP_SWEEP_MAX_AGE.total_seconds() > 24 * 3600, "a daily sweep would fight the cron"
    assert deep_sweep_due([swept(30)]) is True


def test_a_sweep_that_did_not_finish_does_not_count_as_one() -> None:
    """A crashed sweep collected nothing, so it cannot stand in for the sweep
    that would have."""
    from sf_housing.scheduling import deep_sweep_due

    assert deep_sweep_due([swept(2, status="failed")]) is True
    assert deep_sweep_due([swept(2, status="running")]) is True


def test_the_shortlist_is_caught_up_before_the_tail_is(tmp_path) -> None:
    """A sweep takes minutes and holds the scan lock. Running one while a
    scheduled check is still owed would leave the shortlist stale to finish
    collecting homes nobody has asked for yet."""
    from sf_housing.scheduling import sweep_if_due as catch_up_if_due

    started: list[str] = []

    class Scanner:
        is_running = False

        class repository:
            @staticmethod
            def recent_scans(limit=20):
                # A missed 10:00 slot and a sweep that is also overdue.
                return [swept(40)]

        @staticmethod
        def preference_loader():
            class P:
                profile_active = True

            return P()

        @staticmethod
        def start_scan(trigger):
            started.append(trigger)
            return True

    catch_up_if_due(Scanner())

    assert started == [], "the ordinary check has to come first"


def test_a_sweep_that_is_owed_actually_starts() -> None:
    """The predicate is only worth having if something acts on it. Nothing
    did: every test here asked whether a sweep was due and none checked that
    one then ran, which is the whole point of the safety net."""
    from sf_housing.scheduling import sweep_if_due

    started: list[str] = []

    class Scanner:
        is_running = False

        class repository:
            @staticmethod
            def recent_scans(limit=20):
                # The ordinary check is current; no sweep has ever run.
                return [swept(0, trigger="scheduled")]

        @staticmethod
        def preference_loader():
            class P:
                profile_active = True

            return P()

        @staticmethod
        def start_scan(trigger):
            started.append(trigger)
            return True

    assert sweep_if_due(Scanner()) is True
    assert started == ["deep_sweep"]


def test_a_sweep_is_not_started_while_a_check_is_already_running() -> None:
    """It would be refused by the lock anyway; asking is how a heartbeat turns
    into a log full of noise."""
    from sf_housing.scheduling import sweep_if_due

    started: list[str] = []

    class Scanner:
        is_running = True

        class repository:
            @staticmethod
            def recent_scans(limit=20):
                return []

        @staticmethod
        def preference_loader():
            class P:
                profile_active = True

            return P()

        @staticmethod
        def start_scan(trigger):
            started.append(trigger)
            return True

    assert sweep_if_due(Scanner()) is False
    assert started == []
