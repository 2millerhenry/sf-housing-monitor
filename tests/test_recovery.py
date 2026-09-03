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


def room(source_id: str) -> ListingCandidate:
    return ListingCandidate(
        platform="Craigslist",
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
