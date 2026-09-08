"""The four failures a real renter will actually meet.

Recovery used to be proven for exactly one of these: a source raising during a
scan. The rest were inferred from code. Each test here forces the real failure
and checks the user is left with a true statement and a next step.
"""

from __future__ import annotations

import imaplib
import pathlib
import sqlite3
import sys
import threading
import time

import httpx
import pytest

from sf_housing.connectors import connector_state_for_error
from sf_housing.database import DatabaseUnreadableError, Repository
from sf_housing.imap_alerts import ImapAlertError, ImapAlertMailbox
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import parse_preferences
from sf_housing.scanner import Scanner
from tests.conftest import TEST_PREFERENCES

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_imap_alerts import FakeIMAP  # noqa: E402


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def room(source_id: str, platform: str = "Craigslist") -> ListingCandidate:
    return ListingCandidate(
        platform=platform,
        source_id=source_id,
        title="Sunny private room in NOPA",
        original_url=f"https://sfbay.craigslist.org/x/{source_id}.html",
        price=1500,
        neighborhood="NOPA",
        listing_type="Room/share",
        summary="Private room in a shared home, flexible lease.",
    )


class WorkingSource:
    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/a"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        return [room("a1"), room("a2")]

    def enrich(self, client, listing):
        return listing


class RaisingSource:
    mode = "automatic"
    search_url = "https://example.test/b"
    manual_reason = None
    detail_budget = 0

    def __init__(self, platform: str, error: Exception):
        self.platform = platform
        self.error = error

    def search(self, client, preferences):
        raise self.error

    def enrich(self, client, listing):
        return listing


def scanner_for(tmp_path: pathlib.Path, sources):
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    preferences = parse_preferences(TEST_PREFERENCES)
    return repository, Scanner(repository, lambda: preferences, sources)


# --------------------------------------------------------------------------
# 1. installing a second copy must not break the first
# --------------------------------------------------------------------------


def test_isolated_install_keeps_its_login_service_inside_the_app_root() -> None:
    """Writing to ~/Library/LaunchAgents in isolated mode repointed a working
    install's login service at a temporary directory, and the damage only showed
    up at the next login."""
    installer = (REPO_ROOT / "release_assets" / "payload" / "install.sh").read_text(encoding="utf-8")

    assert 'DEFAULT_LAUNCH_AGENTS_DIR="$APP_ROOT/LaunchAgents"' in installer
    assert 'DEFAULT_LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"' in installer
    # The default must be chosen by the isolation flag, not applied unconditionally.
    flag = installer.index('if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]')
    assignment = installer.index('LAUNCH_AGENTS_DIR="${SF_HOUSING_LAUNCH_AGENTS_DIR:-$DEFAULT_LAUNCH_AGENTS_DIR}"')
    assert flag < assignment
    # The isolated run must be told where the plist went, so the Ready Check
    # validates the one that was actually written.
    assert 'SF_HOUSING_LAUNCH_AGENTS_DIR="$LAUNCH_AGENTS_DIR" "$TOOLS_DIR/open.sh"' in installer


# --------------------------------------------------------------------------
# 2. a revoked app password
# --------------------------------------------------------------------------


def test_a_refused_password_asks_the_user_to_reconnect(tmp_path: pathlib.Path) -> None:
    """"Refused" and "the search was refused" need opposite advice, so the
    credential path states which one it is instead of leaving it to text."""
    mailbox = ImapAlertMailbox(
        tmp_path / "imap-credential.json",
        connector=lambda host, **kw: FakeIMAP(host, **kw, login_error=True),
    )
    mailbox.save_credential("someone@gmail.com", "abcd efgh ijkl mnop")

    with pytest.raises(ImapAlertError) as raised:
        mailbox.messages("from:(zillow.com) newer_than:90d")

    assert raised.value.connector_state == "authorization_expired"
    # Without the typed state the message alone reads as a generic problem.
    assert connector_state_for_error(str(raised.value)) == "degraded"


def test_a_revoked_credential_mid_scan_moves_providers_to_reconnect(
    tmp_path: pathlib.Path,
) -> None:
    repository, scanner = scanner_for(
        tmp_path,
        [
            WorkingSource(),
            RaisingSource(
                "Zillow",
                ImapAlertError("password refused", connector_state="authorization_expired"),
            ),
        ],
    )
    # The failing source has to look like a connector-backed one.
    scanner.sources[1].connector_key = "gmail"
    scanner.sources[1].connector_state_key = "gmail:zillow"

    outcome = scanner.run_scan("scheduled")

    assert outcome.status == "completed_with_errors"
    state = repository.connector_state("gmail:zillow")
    assert state.state == "authorization_expired", "the user must be told to reconnect"
    # The free sources are unaffected and the user's data is untouched.
    assert outcome.listings_added == 2


def test_a_network_failure_is_not_mistaken_for_a_bad_password() -> None:
    """Both say "refused"; only one means reconnect."""
    assert connector_state_for_error("Could not reach imap.gmail.com. Check the connection.") == "degraded"


# --------------------------------------------------------------------------
# 3. losing the network mid-scan
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("[Errno 8] nodename nor servname provided"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_losing_the_network_keeps_what_already_worked(
    tmp_path: pathlib.Path, error: Exception
) -> None:
    repository, scanner = scanner_for(
        tmp_path, [WorkingSource(), RaisingSource("SpareRoom", error)]
    )

    outcome = scanner.run_scan("scheduled")

    assert outcome.status == "completed_with_errors"
    assert outcome.sources_failed == 1
    assert outcome.listings_added == 2, "results already collected must survive"
    with repository.connection() as connection:
        runs = {r["platform"]: r for r in connection.execute("SELECT * FROM source_runs")}
    assert runs["Craigslist"]["status"] == "success"
    assert runs["SpareRoom"]["status"] == "error"
    assert type(error).__name__ in runs["SpareRoom"]["message"], "name the real reason"


def test_every_source_failing_is_still_an_honest_finished_scan(tmp_path: pathlib.Path) -> None:
    """An entirely offline machine must not look like a successful empty search."""
    repository, scanner = scanner_for(
        tmp_path,
        [
            RaisingSource("Craigslist", httpx.ConnectError("offline")),
            RaisingSource("SpareRoom", httpx.ConnectError("offline")),
        ],
    )

    outcome = scanner.run_scan("scheduled")

    assert outcome.sources_failed == 2
    assert outcome.status == "completed_with_errors"
    assert outcome.status != "completed", "no sources succeeded, so this is not a clean run"


# --------------------------------------------------------------------------
# 4. a database that cannot be read
# --------------------------------------------------------------------------


def test_an_unreadable_database_names_itself_and_the_recovery(tmp_path: pathlib.Path) -> None:
    corrupt = tmp_path / "housing.sqlite3"
    corrupt.write_bytes(b"this is not a database")

    with pytest.raises(DatabaseUnreadableError) as raised:
        Repository(corrupt).initialize()

    message = str(raised.value)
    assert str(corrupt) in message, "say which file"
    assert "Repair" in message, "say what to do"
    assert "not lost" in message and "Do not delete" in message


def test_a_corrupt_database_is_never_replaced_automatically(tmp_path: pathlib.Path) -> None:
    """It holds every star, note and first-found date the user has built up."""
    corrupt = tmp_path / "housing.sqlite3"
    original = b"this is not a database"
    corrupt.write_bytes(original)

    with pytest.raises(DatabaseUnreadableError):
        Repository(corrupt).initialize()

    assert corrupt.read_bytes() == original, "the file must be left exactly as found"
    assert not list(tmp_path.glob("*.corrupt*")), "nothing may be moved aside either"


def test_a_healthy_database_still_initialises_normally(tmp_path: pathlib.Path) -> None:
    """The new guard must not change the working path."""
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    repository.upsert_listing(room("ok"), ScoreResult(80, ["Fits"], "", {}))

    assert len(repository.query_listings(0, view="all")) == 1
    repository.initialize()  # idempotent
    assert len(repository.query_listings(0, view="all")) == 1


def test_the_startup_failure_reaches_the_service_log(tmp_path: pathlib.Path, caplog) -> None:
    """Repair and the install log surface the service log, not a traceback."""
    import logging

    from sf_housing.app import create_app
    from sf_housing.settings import Settings

    data = tmp_path / "data"
    data.mkdir()
    (data / "housing.sqlite3").write_bytes(b"not a database")
    preferences = tmp_path / "preferences.yaml"
    preferences.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )

    with caplog.at_level(logging.ERROR, logger="sf_housing.app"):
        with pytest.raises(DatabaseUnreadableError):
            create_app(settings=settings, sources=[], enable_scheduler=False)

    assert any("Repair" in record.message for record in caplog.records)


def test_a_database_that_breaks_after_startup_is_reported_by_the_ready_check(
    tmp_path: pathlib.Path,
) -> None:
    """Corruption found while running is a diagnostic, not a crash."""
    from sf_housing.diagnostics import _application_checks
    from sf_housing.settings import Settings

    database = tmp_path / "housing.sqlite3"
    Repository(database).initialize()
    database.write_bytes(b"corrupted after the app started")
    settings = Settings(
        data_dir=tmp_path,
        preferences_path=tmp_path / "preferences.yaml",
        database_path=database,
        log_path=tmp_path / "t.log",
    )

    checks = {
        c.key: c
        for c in _application_checks(
            settings,
            Repository(database),
            request_host="127.0.0.1",
            request_port=8000,
            app_version="test",
        )
    }

    assert checks["database"].status == "blocked"
    assert "Repair" in checks["database"].action


# --------------------------------------------------------------------------
# 5. the app is stopped while a check is running
# --------------------------------------------------------------------------


def interrupted_scan(repository: Repository) -> int:
    """The rows a process killed mid-check leaves behind."""
    run_id = repository.begin_scan("scheduled")
    repository.begin_source_run(run_id, "Craigslist", "https://sfbay.craigslist.org/search/roo")
    return run_id


def scan_row(repository: Repository, run_id: int) -> dict:
    with repository.connection() as connection:
        row = connection.execute("SELECT * FROM scan_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row)


def test_a_check_cut_off_by_a_quit_is_settled_when_the_app_comes_back(
    tmp_path: pathlib.Path,
) -> None:
    """Quitting mid-check left a row saying a check was running, and nothing
    ever cleared it. The Ready Check then told people to reopen the app, and
    reopening the app changed nothing."""
    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    run_id = interrupted_scan(repository)

    assert scanner.recover_interrupted_scans() == 1

    row = scan_row(repository, run_id)
    assert row["status"] == "interrupted"
    assert row["finished_at"], "a check that is over has to have an end"


def test_the_source_left_mid_fetch_is_settled_too(tmp_path: pathlib.Path) -> None:
    """The Sources page reads source runs, not scan runs. Settling only the
    parent leaves that page showing a source still fetching."""
    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    interrupted_scan(repository)

    scanner.recover_interrupted_scans()

    with repository.connection() as connection:
        rows = [dict(r) for r in connection.execute("SELECT * FROM source_runs")]
    assert [r["status"] for r in rows] == ["interrupted"]
    assert all(r["finished_at"] for r in rows)


def test_a_check_that_really_finished_is_left_exactly_as_it_was(
    tmp_path: pathlib.Path,
) -> None:
    """Recovery rewrites history. It may only rewrite the part that is false."""
    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    scanner.run_scan("scheduled")
    before = scan_row(repository, 1)

    assert scanner.recover_interrupted_scans() == 0
    assert scan_row(repository, 1) == before


def test_a_check_that_is_genuinely_running_is_never_declared_dead(
    tmp_path: pathlib.Path,
) -> None:
    """Recovery is only allowed to speak while it holds the lock a live check
    would be holding. Without that rule a scheduled check running at startup
    would be marked interrupted underneath itself."""
    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    run_id = interrupted_scan(repository)

    settled: list[int] = []
    other = Scanner(repository, scanner.preference_loader, [WorkingSource()])
    assert other._acquire_scan_locks(), "a lock nobody holds has to be available"
    try:
        settled.append(scanner.recover_interrupted_scans())
    finally:
        other._release_scan_locks()

    assert settled == [0]
    assert scan_row(repository, run_id)["status"] == "running"


def test_the_ready_check_stops_asking_for_repair_once_the_app_restarts(
    tmp_path: pathlib.Path,
) -> None:
    """The point of all of this. The Ready Check said an interrupted check
    needed Repair; reopening the app is the recovery, so after it the check
    has to stop asking."""
    from datetime import UTC, datetime, timedelta

    from sf_housing.diagnostics import _scan_check

    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    run_id = interrupted_scan(repository)
    with repository.connection() as connection:
        connection.execute(
            "UPDATE scan_runs SET started_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), run_id),
        )
        connection.commit()

    now = datetime.now(UTC)
    before = _scan_check(repository, scanner, now)
    assert before.status == "attention", "the stuck row is what the check is for"
    assert "Repair" in before.action

    scanner.recover_interrupted_scans()

    assert _scan_check(repository, scanner, now).status != "attention"


def test_reopening_the_app_is_what_actually_settles_the_record(
    tmp_path: pathlib.Path,
) -> None:
    """The path a person really takes. Recovery nothing calls is not recovery,
    and the Ready Check tells them reopening the app is the fix."""
    from sf_housing.app import create_app
    from sf_housing.settings import Settings

    database = tmp_path / "housing.sqlite3"
    repository = Repository(database)
    repository.initialize()
    run_id = interrupted_scan(repository)

    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    create_app(
        settings=Settings(
            data_dir=tmp_path,
            preferences_path=preferences_path,
            database_path=database,
            log_path=tmp_path / "t.log",
        ),
        sources=[],
        enable_scheduler=False,
    )

    assert scan_row(repository, run_id)["status"] == "interrupted"


# --------------------------------------------------------------------------
# 6. a source whose detail pages stop answering
# --------------------------------------------------------------------------


class StallingDetailSource:
    """Searches fine, then never finishes a detail page.

    The real shape of the failure: an HTTP read timeout bounds each chunk of a
    response rather than the whole of it, so a server that trickles bytes holds
    the connection open for as long as it likes.
    """

    mode = "automatic"
    search_url = "https://example.test/stall"
    manual_reason = None
    detail_budget = 3

    def __init__(self, platform: str = "Craigslist") -> None:
        self.platform = platform
        self.released = threading.Event()
        self.enrich_calls = 0

    def search(self, client, preferences):
        return [room("s1"), room("s2"), room("s3")]

    def enrich(self, client, listing):
        self.enrich_calls += 1
        self.released.wait(12)  # far past any ceiling a test sets
        return listing


class LateSource:
    """Runs after the stalling one, and is what starvation actually costs."""

    mode = "automatic"
    search_url = "https://example.test/late"
    manual_reason = None
    detail_budget = 0

    def __init__(self, platform: str = "Zillow") -> None:
        self.platform = platform

    def search(self, client, preferences):
        return [room("late1", self.platform), room("late2", self.platform)]

    def enrich(self, client, listing):
        return listing


def test_a_detail_page_that_never_answers_does_not_hold_the_scan(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The ceiling was written to cover a source's whole turn, but only ever
    wrapped the search. One Craigslist check spent 593 seconds inside detail
    pages and every source behind it was skipped with nothing collected."""
    from sf_housing import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "DETAIL_HARD_CEILING_SECONDS", 0.4)
    stalling = StallingDetailSource()
    repository, scanner = scanner_for(tmp_path, [stalling, LateSource()])

    try:
        started = time.monotonic()
        outcome = scanner.run_scan("scheduled")
        elapsed = time.monotonic() - started
    finally:
        stalling.released.set()

    assert stalling.enrich_calls, "the detail fetch has to have been attempted"
    assert elapsed < 9, f"a stalled detail page held the scan for {elapsed:.1f}s"
    assert outcome.status in {"completed", "completed_with_errors"}


def test_the_sources_behind_a_stalled_one_still_run(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """This is what the overrun actually cost: not one slow source, but every
    source queued behind it collecting nothing."""
    from sf_housing import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "DETAIL_HARD_CEILING_SECONDS", 0.4)
    stalling = StallingDetailSource()
    repository, scanner = scanner_for(tmp_path, [stalling, LateSource()])

    try:
        scanner.run_scan("scheduled")
    finally:
        stalling.released.set()

    with repository.connection() as connection:
        platforms = {
            row["platform"] for row in connection.execute("SELECT platform FROM listings")
        }
    assert "Zillow" in platforms, "the source behind the stalled one was skipped"


def test_a_stalled_detail_page_never_loses_the_search_result(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A detail page is an enrichment of a home already found. Giving up on the
    detail must cost the detail, never the home."""
    from sf_housing import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "DETAIL_HARD_CEILING_SECONDS", 0.4)
    stalling = StallingDetailSource()
    repository, scanner = scanner_for(tmp_path, [stalling, LateSource()])

    try:
        scanner.run_scan("scheduled")
    finally:
        stalling.released.set()

    with repository.connection() as connection:
        kept = [
            row["source_id"]
            for row in connection.execute(
                "SELECT source_id FROM listings WHERE platform = 'Craigslist'"
            )
        ]
    assert sorted(kept) == ["s1", "s2", "s3"], "the searched homes have to survive"


def test_the_detail_ceiling_never_outlasts_what_the_phase_has_left(
    tmp_path: pathlib.Path,
) -> None:
    """Each call is bounded on its own, but a run of them must not add up to an
    overrun either, so the ceiling also stops at the phase's own limit."""
    from sf_housing.scanner import DETAIL_HARD_CEILING_SECONDS

    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    asked: list[float] = []

    class Recorder:
        platform = "Craigslist"

        def enrich(self, client, listing):
            raise AssertionError("never reached")

    scanner._within_ceiling = lambda label, ceiling, run, *, timed_out: asked.append(ceiling)

    near = DETAIL_HARD_CEILING_SECONDS / 3
    scanner._enrich_within_ceiling(
        Recorder(), None, room("x"), limit=time.monotonic() + near
    )
    scanner._enrich_within_ceiling(
        Recorder(), None, room("x"), limit=time.monotonic() + DETAIL_HARD_CEILING_SECONDS * 5
    )

    assert asked[0] < DETAIL_HARD_CEILING_SECONDS, "a near limit has to shorten the ceiling"
    assert asked[0] <= near + 1e-6, f"ceiling {asked[0]} outran the phase limit {near}"
    assert asked[1] == DETAIL_HARD_CEILING_SECONDS, "a distant limit leaves a whole page's worth"


class StallingRecheckSource:
    """Finds nothing new, and stalls on the rechecks of what it used to list.

    detail_budget is zero, so the only place this source can call ``enrich`` is
    the recheck of a home missing from its search. That is what makes the test
    below aim at the recheck path and nothing else.
    """

    mode = "automatic"
    search_url = "https://example.test/recheck"
    manual_reason = None
    detail_budget = 0

    def __init__(self, platform: str = "Craigslist") -> None:
        self.platform = platform
        self.released = threading.Event()
        self.enrich_calls = 0

    def search(self, client, preferences):
        return [room("still-here", self.platform)]

    def enrich(self, client, listing):
        self.enrich_calls += 1
        self.released.wait(12)
        return listing


def test_a_stalled_recheck_does_not_hold_the_scan_either(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The recheck walks homes a source has stopped listing, one detail page
    each. It reads the clock before every one, but the read itself was
    unbounded, so a page that never answered spent the whole scan there."""
    from sf_housing import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "DETAIL_HARD_CEILING_SECONDS", 0.4)
    stalling = StallingRecheckSource()
    repository, scanner = scanner_for(tmp_path, [stalling])
    # A shortlisted home the search no longer returns is exactly what a recheck
    # goes and looks at.
    for index in range(3):
        repository.upsert_listing(
            room(f"gone{index}"), ScoreResult(90, ["fits"], "check", {})
        )

    try:
        started = time.monotonic()
        scanner.run_scan("scheduled")
        elapsed = time.monotonic() - started
    finally:
        stalling.released.set()

    assert stalling.enrich_calls, "the recheck has to have been attempted"
    assert elapsed < 9, f"a stalled recheck held the scan for {elapsed:.1f}s"


def test_one_detail_page_is_never_worth_more_than_a_whole_source(tmp_path: pathlib.Path) -> None:
    """The ceilings are only meaningful relative to each other and to the scan.
    A single page allowed as long as a source's entire turn, or as long as the
    scan itself, is not a ceiling."""
    from sf_housing.scanner import (
        DETAIL_HARD_CEILING_SECONDS,
        SOURCE_HARD_CEILING_SECONDS,
    )
    from sf_housing.settings import Settings

    assert 0 < DETAIL_HARD_CEILING_SECONDS < SOURCE_HARD_CEILING_SECONDS
    assert DETAIL_HARD_CEILING_SECONDS < Settings.from_environment().scan_max_seconds / 4
