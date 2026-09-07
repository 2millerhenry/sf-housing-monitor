"""The small San Francisco managers who let through AppFolio.

One parser and a list. The fixtures are real captures of the three shapes a
manager's page comes in: listings, none, and the page AppFolio serves for a
subdomain that is not a tenant site at all -- which is HTTP 200 and parses into
zero homes, and would otherwise read as a manager with nothing free, for good.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from sf_housing.classification import WHOLE_UNIT, classify_listing
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import AppFolioSource, SourceError, default_sources
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"


def listings_page() -> str:
    return (FIXTURES / "appfolio_listings.html").read_text(encoding="utf-8")


def empty_page() -> str:
    return (FIXTURES / "appfolio_no_listings.html").read_text(encoding="utf-8")


def not_a_manager() -> str:
    return (FIXTURES / "appfolio_not_a_manager.html").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class FakeClient:
    def __init__(self, *pages: FakeResponse):
        self.pages = list(pages)
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.pages[min(len(self.requested) - 1, len(self.pages) - 1)]


def deal(*paths: str) -> Preferences:
    return parse_preferences(
        yaml.safe_dump({"profile_version": 1, "profile": {
            "state": "active", "enabled_paths": list(paths),
            "budgets": {p: {"maximum_monthly": 9000, "minimum_monthly": 100, "occupants": 2} for p in paths},
            "geography": {"anywhere_in_sf": True}}}))


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


def found(prefs: Preferences | None = None, *pages: FakeResponse):
    client = FakeClient(*(pages or (FakeResponse(listings_page()), FakeResponse(empty_page()),
                                    FakeResponse(empty_page()))))
    return AppFolioSource().search(client, prefs or deal("studio", "one_bedroom", "two_bedroom")), client


# --------------------------------------------------------------------------
# the failure this source is mostly about
# --------------------------------------------------------------------------


def test_a_subdomain_that_is_not_a_manager_is_not_a_manager_with_nothing(caplog) -> None:
    """AppFolio answers an unknown subdomain with HTTP 200 and its own page.
    Counted as an empty manager, somebody's typo becomes a permanent zero."""
    assert "js-listings" not in not_a_manager()
    assert "js-listings" in empty_page(), "a real manager keeps the container even with nothing in it"

    with caplog.at_level(logging.WARNING, logger="sf_housing.sources"):
        listings, _ = found(None, FakeResponse(listings_page()), FakeResponse(not_a_manager()),
                            FakeResponse(empty_page()))

    assert listings, "the working manager is still read"
    assert any("not an AppFolio tenant site" in r.getMessage() for r in caplog.records)


def test_a_manager_with_nothing_free_is_simply_empty() -> None:
    listings, _ = found(None, FakeResponse(empty_page()), FakeResponse(empty_page()), FakeResponse(empty_page()))
    assert listings == []


def test_no_manager_answering_is_an_error(preferences) -> None:
    """Different from every manager having nothing: this is the roster being
    wrong, or AppFolio being down, and it must not read as a quiet market."""
    with pytest.raises(SourceError, match="answered with a listings page"):
        AppFolioSource().search(FakeClient(*(FakeResponse(not_a_manager()) for _ in range(3))), preferences)


def test_one_manager_failing_does_not_lose_the_others(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="sf_housing.sources"):
        listings, _ = found(None, FakeResponse("", 429), FakeResponse(listings_page()), FakeResponse(empty_page()))

    assert listings, "the manager that answered is still read"
    assert any("could not read" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# the roster
# --------------------------------------------------------------------------


def test_the_roster_is_data_not_code() -> None:
    """Adding a manager has to be a row. That is the whole point."""
    roster = json.loads(AppFolioSource.ROSTER.read_text(encoding="utf-8"))

    assert roster["managers"], "at least one manager"
    for manager in roster["managers"]:
        assert manager["subdomain"] and manager["name"]
        assert "." not in manager["subdomain"], "a subdomain, not a host"


def test_every_manager_is_asked_for_its_own_page() -> None:
    _, client = found()
    roster = [m["subdomain"] for m in AppFolioSource.managers()]

    assert len(client.requested) == len(roster)
    for subdomain, url in zip(roster, client.requested):
        assert url == f"https://{subdomain}.appfolio.com/listings"


# --------------------------------------------------------------------------
# what a card says
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 bd / 1 ba", (1, 1.0)),
        ("2 bd / 1 ba", (2, 1.0)),
        ("3 bd / 2.5 ba", (3, 2.5)),
        ("Studio / 1 ba", (0, 1.0)),
        ("0 bd / 1 ba", (0, 1.0)),
        ("", (None, None)),
    ],
)
def test_the_bed_count_is_not_the_bathroom_count(text, expected) -> None:
    """Both numbers live in one string. Read as a range, a two-bed-one-bath
    becomes a one-bedroom and a three-bed-two-and-a-half becomes a two."""
    assert AppFolioSource._bed_bath(text) == expected


def test_only_san_francisco_is_kept() -> None:
    """These managers let outside the city too."""
    listings, _ = found()

    assert listings
    for listing in listings:
        assert "Oakland" not in listing.summary
        assert listing.platform == "AppFolio"
        assert listing.housing_kind == WHOLE_UNIT


def test_a_home_is_identified_by_the_uuid_in_its_link() -> None:
    listings, _ = found()
    for listing in listings:
        assert listing.source_id in listing.original_url
        assert listing.original_url.startswith("https://") and ".appfolio.com/listings/detail/" in listing.original_url


def test_a_studio_is_scored_as_a_studio() -> None:
    listings, _ = found()
    studios = [x for x in listings if x.metadata.get("bedrooms") == 0]

    assert studios, "the fixture carries a studio"
    assert classify_listing(studios[0]).unit_type == "studio"


def test_the_manager_is_named_because_the_reader_will_be_writing_to_them() -> None:
    listings, _ = found()
    assert all("let by" in x.summary for x in listings)


def test_a_home_too_small_for_the_deal_is_dropped() -> None:
    wide, _ = found(deal("studio", "one_bedroom", "two_bedroom"))
    two, _ = found(deal("two_bedroom"))

    assert len(two) < len(wide)
    for listing in two:
        assert listing.metadata["bedrooms"] >= 2


def test_no_detail_pages_are_read() -> None:
    assert AppFolioSource.detail_budget == 0
    assert not hasattr(AppFolioSource, "enrich")


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_it_runs_without_setup_in_every_branch() -> None:
    class Mailbox:
        pass

    for sources in (default_sources(), default_sources(Mailbox())):
        registered = [x for x in sources if x.platform == "AppFolio"]
        assert len(registered) == 1
        assert registered[0].mode == "automatic"
        assert getattr(registered[0], "connector_key", None) is None
        assert registered[0].search_url.startswith("https://")


def test_the_ready_check_still_probes_the_same_four() -> None:
    probed = [
        source.platform for source in default_sources()
        if getattr(source, "mode", "setup") == "automatic" and not getattr(source, "connector_key", None)
    ][:4]
    assert probed == ["Craigslist", "Listings Project", "Abacus (small buildings)", "SpareRoom"]


def test_a_street_named_after_the_city_is_not_the_city() -> None:
    """Real listing, real trap: "43632 San Francisco Ave, Lancaster, CA" is a
    home in Lancaster. One manager on this platform publishes three hundred
    homes and four of them say San Francisco in the street line; none is in it."""
    page = listings_page()
    assert "San Francisco Ave" in page and "Lancaster" in page, "the fixture must carry the trap"

    listings, _ = found()

    assert listings, "the real San Francisco homes are still read"
    for listing in listings:
        assert "Lancaster" not in listing.summary
        assert not listing.title.lower().startswith("lancaster")
