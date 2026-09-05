"""What a disconnected source is costing -- or an honest silence.

"Waiting for first alert" is true and completely unmotivating: it never says
what the missing setup costs, so a chore with no stated payoff does not get
done. A real number gives it one.

Counting turns out to be mostly impossible, and these tests exist to keep that
honest. Of the four providers, exactly one answers a script. The dangerous one
is Zillow: it returns HTTP 200 with a title reading "0 Rentals" and
"totalResultCount": 0, from behind a PerimeterX captcha. A parser that trusted
it would print "Zillow: 0 homes you are not seeing" -- not a missing number, but
the precise opposite of the truth, stated confidently.
"""

from __future__ import annotations

import pathlib

import httpx
import pytest

from sf_housing.coverage import (
    ALERT_SETUP_SEARCHES,
    MAXIMUM_CREDIBLE_COUNT,
    fetch_count,
    looks_blocked,
    missing_coverage_targets,
    parse_count,
)
from sf_housing.database import Repository

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "coverage"
PLATFORMS = {
    "hotpads": "HotPads",
    "apartmentscom": "Apartments.com",
    "roomies": "Roomies",
    "zillow": "Zillow",
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# real responses, saved from the real sites
# --------------------------------------------------------------------------


def test_hotpads_is_the_one_that_can_be_counted() -> None:
    """Its page title carries the figure: "... - 4,562 Rentals | HotPads"."""
    assert parse_count("HotPads", fixture("hotpads-200.html")) == 4562


def test_zillow_answers_a_blocked_client_with_a_confident_zero() -> None:
    """The whole reason this module refuses zero and looks for block signals."""
    html = fixture("zillow-200-blocked.html")

    assert "0 Rentals" in html, "the fixture has to still contain the trap"
    assert looks_blocked(html) is True
    assert parse_count("Zillow", html) is None


@pytest.mark.parametrize("name", ["apartmentscom-403.html", "roomies-403.html"])
def test_a_refusal_is_not_a_count(name: str) -> None:
    platform = PLATFORMS[name.split("-")[0]]
    assert parse_count(platform, fixture(name)) is None


def test_every_saved_fixture_is_still_what_it_claims_to_be() -> None:
    """These pages get redesigned. A fixture per provider means a redesign fails
    here rather than silently returning nothing in production."""
    counted = {
        PLATFORMS[path.stem.split("-")[0]]: parse_count(
            PLATFORMS[path.stem.split("-")[0]], path.read_text(encoding="utf-8")
        )
        for path in FIXTURES.glob("*.html")
    }

    assert counted["HotPads"] == 4562
    assert counted["Apartments.com"] is None
    assert counted["Roomies"] is None
    assert counted["Zillow"] is None


# --------------------------------------------------------------------------
# the rule: a number that is not real never appears
# --------------------------------------------------------------------------


def test_zero_is_never_a_count() -> None:
    """It is what a wall reports, it is falsy so it vanishes into every
    truthiness check downstream, and "0 homes you are missing" is the opposite
    of what a failure to look actually means."""
    assert parse_count("HotPads", "<title>SF - 0 Rentals | HotPads</title>") is None


def test_an_absurd_number_is_not_a_count() -> None:
    assert parse_count("HotPads", "<title>SF - 9,999,999 Rentals | HotPads</title>") is None
    assert MAXIMUM_CREDIBLE_COUNT < 9_999_999


@pytest.mark.parametrize(
    "signal",
    ["captcha", "PerimeterX", "Are you a human", "unusual traffic", "Just a moment"],
)
def test_a_wall_is_refused_however_it_is_worded(signal: str) -> None:
    html = f"<title>SF - 4,562 Rentals | HotPads</title><body>{signal}</body>"
    assert looks_blocked(html) is True
    assert parse_count("HotPads", html) is None


def test_an_unverified_provider_is_never_parsed() -> None:
    """Only sources proven against a real response are read. Adding one means
    proving it first, not hoping the regex generalises."""
    hotpads_shaped = "<title>SF - 4,562 Rentals | Apartments.com</title>"
    assert parse_count("Apartments.com", hotpads_shaped) is None
    assert parse_count("Roomies", hotpads_shaped) is None
    assert parse_count("Zillow", hotpads_shaped) is None


@pytest.mark.parametrize("html", ["", "<html></html>", "<title></title>", "not html at all"])
def test_an_empty_or_broken_body_is_not_a_count(html: str) -> None:
    assert parse_count("HotPads", html) is None


# --------------------------------------------------------------------------
# fetching: every failure answers None
# --------------------------------------------------------------------------


def client_returning(status: int, body: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_good_response_is_counted() -> None:
    with client_returning(200, fixture("hotpads-200.html")) as client:
        assert fetch_count(client, "HotPads", "https://hotpads.com/x") == 4562


@pytest.mark.parametrize("status", [301, 403, 404, 429, 500, 503])
def test_any_status_but_200_is_not_a_count(status: int) -> None:
    with client_returning(status, fixture("hotpads-200.html")) as client:
        assert fetch_count(client, "HotPads", "https://hotpads.com/x") is None


def test_a_network_failure_is_not_a_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_count(client, "HotPads", "https://hotpads.com/x") is None


def test_a_timeout_is_not_a_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("took too long")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_count(client, "HotPads", "https://hotpads.com/x") is None


def test_nothing_is_fetched_for_a_source_that_cannot_be_counted() -> None:
    """Not a wasted request, and not a chance to be wrong."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=fixture("hotpads-200.html"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_count(client, "Zillow", "https://zillow.com/x") is None
        assert fetch_count(client, "Roomies", "https://roomies.com/x") is None

    assert calls == []


# --------------------------------------------------------------------------
# who gets asked
# --------------------------------------------------------------------------


def repo(tmp_path: pathlib.Path) -> Repository:
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    return repository


def test_a_working_provider_is_not_asked(tmp_path: pathlib.Path) -> None:
    """Once its alerts arrive the question is meaningless, and the request is
    waste."""
    repository = repo(tmp_path)
    repository.set_connector_state("gmail:hotpads", "working", message="Imported 3 listings.")

    targets = dict(missing_coverage_targets(repository, None))

    assert "HotPads" not in targets
    assert "Roomies" in targets


def test_only_providers_with_a_known_search_page_are_asked(tmp_path: pathlib.Path) -> None:
    targets = dict(missing_coverage_targets(repo(tmp_path), None))

    assert set(targets) == set(ALERT_SETUP_SEARCHES)
    assert "Zillow" not in targets, "no verified way to count it, so it is never fetched"


def test_an_unreadable_connector_store_asks_nobody(tmp_path: pathlib.Path) -> None:
    class Broken:
        def connector_states(self):
            raise RuntimeError("database is locked")

    # It falls back to asking everyone rather than raising, which is safe: each
    # fetch is still bounded and each failure still answers None.
    assert isinstance(missing_coverage_targets(Broken(), None), list)


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def test_a_count_is_stored_with_the_moment_it_was_taken(tmp_path: pathlib.Path) -> None:
    repository = repo(tmp_path)
    repository.record_source_coverage("HotPads", 4562)

    stored = repository.source_coverage()

    assert stored["HotPads"]["count"] == 4562
    assert stored["HotPads"]["taken_at"], "a number with no date cannot be shown honestly"


def test_a_zero_is_never_stored(tmp_path: pathlib.Path) -> None:
    """Belt and braces: the parser refuses it, and so does the store."""
    repository = repo(tmp_path)
    repository.record_source_coverage("HotPads", 0)
    repository.record_source_coverage("Roomies", -5)

    assert repository.source_coverage() == {}


def test_a_newer_count_replaces_an_older_one(tmp_path: pathlib.Path) -> None:
    repository = repo(tmp_path)
    repository.record_source_coverage("HotPads", 4562)
    repository.record_source_coverage("HotPads", 4600)

    assert repository.source_coverage()["HotPads"]["count"] == 4600


def test_the_coverage_table_appears_on_an_existing_database(tmp_path: pathlib.Path) -> None:
    """It is a migration onto databases that already hold a year of listings."""
    repository = repo(tmp_path)
    repository.initialize()  # idempotent, as a restart would

    assert repository.source_coverage() == {}


# --------------------------------------------------------------------------
# a passenger on the scan, never the reason one fails
# --------------------------------------------------------------------------


class Room:
    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/a"
    manual_reason = None
    detail_budget = 0
    recheck_budget = 0

    def search(self, client, preferences):
        from sf_housing.models import ListingCandidate

        return [
            ListingCandidate(
                platform="Craigslist",
                source_id="r1",
                title="Sunny private room in NOPA",
                original_url="https://sfbay.craigslist.org/roo/d/x/1.html",
                price=1500,
                neighborhood="NOPA",
                listing_type="Room/share",
                summary="A private room in a shared home.",
            )
        ]

    def enrich(self, client, listing):
        return listing


def scanner_for(tmp_path: pathlib.Path):
    from sf_housing.preferences import parse_preferences
    from sf_housing.scanner import Scanner
    from tests.conftest import TEST_PREFERENCES

    repository = repo(tmp_path)
    preferences = parse_preferences(TEST_PREFERENCES)
    return repository, Scanner(repository, lambda: preferences, [Room()])


def test_a_counting_failure_never_fails_the_scan(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It rides along on a scan whose real job is collecting homes."""
    repository, scanner = scanner_for(tmp_path)

    def explode(*args, **kwargs):
        raise RuntimeError("hotpads fell over")

    monkeypatch.setattr("sf_housing.scanner.fetch_count", explode)

    outcome = scanner.run_scan("scheduled")

    assert outcome.status == "completed", "a counting failure is not a source failure"
    assert outcome.sources_failed == 0
    assert len(repository.query_listings(0, view="all")) == 1
    assert repository.source_coverage() == {}


def test_a_counting_failure_leaves_connector_state_untouched(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, scanner = scanner_for(tmp_path)
    repository.set_connector_state("gmail:hotpads", "waiting_first_alert", message="Nothing yet.")

    monkeypatch.setattr(
        "sf_housing.scanner.fetch_count",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")),
    )
    scanner.run_scan("scheduled")

    state = repository.connector_state("gmail:hotpads")
    assert state.state == "waiting_first_alert"
    assert state.message == "Nothing yet."


def test_a_real_count_reaches_the_store_during_a_scan(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, scanner = scanner_for(tmp_path)
    monkeypatch.setattr(
        "sf_housing.scanner.fetch_count",
        lambda client, platform, url: 4562 if platform == "HotPads" else None,
    )

    scanner.run_scan("scheduled")

    stored = repository.source_coverage()
    assert stored["HotPads"]["count"] == 4562
    assert "Roomies" not in stored, "only what could actually be counted is stored"


def test_a_manual_scan_does_not_go_counting(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing Check for new homes is about homes. Four extra requests every
    time somebody is impatient is not politeness."""
    repository, scanner = scanner_for(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "sf_housing.scanner.fetch_count",
        lambda client, platform, url: calls.append(platform) or 4562,
    )

    scanner.run_scan("manual")

    assert calls == []
    assert repository.source_coverage() == {}


# --------------------------------------------------------------------------
# what the reader ends up seeing
# --------------------------------------------------------------------------


def alerts_page(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, coverage=None):
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.settings import Settings
    from tests.conftest import TEST_PREFERENCES

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences_path,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    for platform, count in (coverage or {}).items():
        application.state.repository.record_source_coverage(platform, count)
    with TestClient(application) as client:
        return client.get("/alerts").text


def test_a_counted_source_says_what_it_is_holding(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = alerts_page(tmp_path, monkeypatch, {"HotPads": 4562})

    assert "4,562" in page
    assert "not seeing" in page, "it has to say what the number means"
    assert "as of" in page, "a number with no date cannot be read honestly"


def test_a_source_that_could_not_be_counted_shows_no_number(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest state, and the one three of the four are always in."""
    page = alerts_page(tmp_path, monkeypatch, {"HotPads": 4562})

    assert "on HotPads" in page
    assert "on Roomies" not in page, "only what could really be counted gets a figure"
    assert "Roomies" in page, "but every source still gets its invitation"


def test_no_count_at_all_still_renders_the_invitations(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = alerts_page(tmp_path, monkeypatch, {})

    assert "not seeing" not in page
    for platform in ("Zillow", "HotPads", "Apartments.com", "Roomies"):
        assert platform in page


def test_opening_the_page_fetches_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page request that counted would tell four companies every time the
    Alerts page was opened."""
    calls: list[str] = []
    monkeypatch.setattr(
        "sf_housing.coverage.fetch_count",
        lambda client, platform, url: calls.append(platform) or 1,
    )

    alerts_page(tmp_path, monkeypatch, {"HotPads": 4562})

    assert calls == []
