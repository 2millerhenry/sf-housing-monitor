"""The leasing feed of San Francisco's largest landlord.

Nothing about this source is on the page it appears to come from: the search
returns eighty-five kilobytes of filter UI and no homes. The fixtures are real
captures of the search plugin's own JSON endpoint, including the one that
matters most -- a search the site could not satisfy, which answers with twelve
recommended homes in place of results.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sf_housing.classification import WHOLE_UNIT, classify_listing
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import RentSFNowSource, SourceError, default_sources
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"


def page(name: str) -> str:
    return (FIXTURES / f"rentsfnow_{name}.json").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class FakeClient:
    def __init__(self, *pages: FakeResponse):
        self.pages = list(pages) or [FakeResponse('{"units": []}')]
        self.posted: list[dict] = []
        self.urls: list[str] = []

    def post(self, url, **kwargs):
        self.urls.append(url)
        self.posted.append(dict(kwargs.get("data") or {}))
        return self.pages[min(len(self.posted) - 1, len(self.pages) - 1)]

    def get(self, url, **kwargs):  # pragma: no cover - this source never GETs
        raise AssertionError("RentSFNow reads its endpoint by POST")


def deal(*paths: str) -> Preferences:
    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": list(paths),
                    "budgets": {
                        path: {"maximum_monthly": 6000, "minimum_monthly": 100, "occupants": 2}
                        for path in paths
                    },
                    "geography": {"anywhere_in_sf": True},
                },
            }
        )
    )


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


def found(prefs: Preferences | None = None, *responses: FakeResponse):
    client = FakeClient(*(responses or (FakeResponse(page("page1")), FakeResponse('{"units": []}'))))
    return RentSFNowSource().search(client, prefs or deal("studio", "one_bedroom", "two_bedroom")), client


# --------------------------------------------------------------------------
# the trap this source is mostly about
# --------------------------------------------------------------------------


def test_recommendations_are_never_stored_as_results() -> None:
    """Asked for something it has not got, the site answers with twelve homes
    that match nothing that was asked for, and its own page shows those in
    place of results. Stored, they would be twelve inventions."""
    payload = json.loads(page("recommendations"))
    assert payload["units"] == [] and len(payload["recommended_units"]) == 12, "fixture must be the padded answer"

    listings, client = found(None, FakeResponse(page("recommendations")))

    assert listings == []
    assert len(client.posted) == 1, f"it must stop, not walk to last_page={payload['last_page']}"


def test_an_empty_answer_is_empty_and_not_an_error(preferences) -> None:
    """A landlord with nothing free this week is a fact, not a fault."""
    assert RentSFNowSource().search(FakeClient(FakeResponse('{"units": [], "last_page": 1}')), preferences) == []


# --------------------------------------------------------------------------
# what it asks for
# --------------------------------------------------------------------------


def test_the_search_is_never_narrowed_to_a_map_viewport() -> None:
    """latN/latS/lonE/lonW are the map view's bounds. Sent, they would quietly
    reduce a city-wide search to whatever rectangle was on screen."""
    _, client = found()

    for sent in client.posted:
        assert not any(key.startswith(("lat", "lon")) for key in sent), sent
        assert sent["action"] == "wpas_ajax_load"
        assert sent["type"] == "json"
        assert sent["city"] == "san-francisco"


def test_the_bedroom_floor_is_asked_of_the_server() -> None:
    _, wide = found(deal("studio", "one_bedroom"))
    _, narrow = found(deal("two_bedroom"))

    assert wide.posted[0]["bedrooms"] == ""
    assert narrow.posted[0]["bedrooms"] == "2"


def test_pages_are_walked_and_stopped(preferences) -> None:
    listings, client = found(
        preferences, FakeResponse(page("page1")), FakeResponse(page("last_page")), FakeResponse('{"units": []}')
    )

    assert [sent["page"] for sent in client.posted][:2] == ["1", "2"]
    assert len(listings) > 12, "both pages must be read"
    ids = [listing.source_id for listing in listings]
    assert len(ids) == len(set(ids))


def test_the_servers_own_last_page_ends_the_read(preferences) -> None:
    """It says how many pages there are; asking past that is wasted."""
    short = json.loads(page("page1"))
    short["last_page"] = 1
    client = FakeClient(FakeResponse(json.dumps(short)), FakeResponse(page("last_page")))

    RentSFNowSource().search(client, preferences)

    assert len(client.posted) == 1


# --------------------------------------------------------------------------
# what a unit says
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        ("Studio \\\\ 1  Bath \\\\ &#36;1,945", ((0, 0), 1.0, 1945)),
        ("1  Bed \\\\ 1  Bath \\\\ &#36;3,195", ((1, 1), 1.0, 3195)),
        ("2  Beds \\\\ 1  Bath \\\\ &#36;5,995", ((2, 2), 1.0, 5995)),
        ("2  Beds \\\\ 2  Baths \\\\ &#36;7,495", ((2, 2), 2.0, 7495)),
        ("", (None, None, None)),
    ],
)
def test_every_shape_the_feed_writes_a_unit_in(criteria, expected) -> None:
    """One escaped string carries the size, the baths and the rent. All four
    shapes the feed uses are here, and &#36; is a dollar sign."""
    assert RentSFNowSource._criteria(criteria) == expected


def test_a_unit_carries_what_it_needs_to_be_scored(preferences) -> None:
    listings, _ = found(preferences)

    assert listings
    for listing in listings:
        assert listing.platform == "RentSFNow"
        assert listing.original_url.startswith("https://www.rentsfnow.com/apartments/rental/")
        assert listing.housing_kind == WHOLE_UNIT
        assert listing.source_id.isdigit(), "the feed's post id, not a slug"
        assert listing.price and listing.price > 0
        assert "bedrooms" in listing.metadata


def test_a_relative_link_is_made_absolute(preferences) -> None:
    """The feed publishes /apartments/rental/635-ellis-203, which stored as-is
    is a link to nowhere."""
    listings, _ = found(preferences)
    assert all(listing.original_url.startswith("https://") for listing in listings)


def test_the_landlord_names_the_area_and_the_street_answers_when_it_cannot() -> None:
    """It says "Tenderloin" itself. Where it says something this deal cannot
    rank -- "Lower Nob Hill", "Downtown" -- the address decides, rather than a
    table of near-misses somebody wrote."""
    listings, _ = found()
    placed = [listing for listing in listings if listing.neighborhood]

    assert len(placed) >= len(listings) - 2
    assert "Tenderloin" in {listing.neighborhood for listing in listings}
    for listing in listings:
        assert listing.neighborhood != "Lower Nob Hill", "not a name this deal can rank"


def test_a_home_too_small_for_the_deal_is_dropped() -> None:
    wide, _ = found(deal("studio", "one_bedroom", "two_bedroom"))
    two, _ = found(deal("two_bedroom"))

    assert len(two) < len(wide)
    for listing in two:
        assert listing.metadata["bedrooms"] >= 2


def test_a_size_the_feed_states_is_the_size_it_is_scored_as(preferences) -> None:
    """Unlike a student board, this is a landlord letting whole apartments, so
    its bed count really is the size of the home."""
    listings, _ = found(preferences)
    studios = [x for x in listings if x.metadata.get("bedrooms") == 0]

    assert studios
    assert classify_listing(studios[0]).unit_type == "studio"


# --------------------------------------------------------------------------
# how it fails
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse("", 202), "HTTP 202"),
        (FakeResponse("   ", 200), "empty page"),
        (FakeResponse("nope", 429), "turned away"),
        (FakeResponse("<html>Not found</html>"), "other than JSON"),
        (FakeResponse("[1, 2, 3]"), "unexpected shape"),
    ],
)
def test_an_answer_that_is_not_a_search_result_says_which(response, expected, preferences) -> None:
    with pytest.raises(SourceError, match=expected):
        RentSFNowSource().search(FakeClient(response), preferences)


def test_no_detail_pages_are_read() -> None:
    """The unit record already carries address, area, size, baths, rent, pets."""
    assert RentSFNowSource.detail_budget == 0
    assert not hasattr(RentSFNowSource, "enrich")


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_it_runs_without_setup_in_every_branch() -> None:
    class Mailbox:
        pass

    for sources in (default_sources(), default_sources(Mailbox())):
        registered = [x for x in sources if x.platform == "RentSFNow"]
        assert len(registered) == 1
        source = registered[0]
        assert source.mode == "automatic"
        assert source.manual_reason is None
        assert getattr(source, "connector_key", None) is None
        # The Ready Check probes search_url, so it has to be a page a person
        # can open -- not the POST endpoint the homes actually come from.
        assert source.search_url.startswith("https://") and "admin-ajax" not in source.search_url


def test_the_ready_check_still_probes_the_same_four() -> None:
    probed = [
        source.platform
        for source in default_sources()
        if getattr(source, "mode", "setup") == "automatic"
        and not getattr(source, "connector_key", None)
    ][:4]
    assert probed == ["Craigslist", "Listings Project", "Abacus (small buildings)", "SpareRoom"]


@pytest.mark.parametrize(
    "trailing",
    ["Available 9/15", "Call 415-555-1234", "1 Parking", "Call for pricing"],
    ids=["a date", "a phone number", "a count", "no digits at all"],
)
def test_only_a_dollar_sign_makes_a_rent(trailing) -> None:
    """A part that is not a price still has digits in it, and read loosely a
    move-in date of 9/15 becomes a home going for nine dollars a month."""
    span, baths, price = RentSFNowSource._criteria(f"2  Beds \\\\ 1  Bath \\\\ {trailing}")

    assert span == (2, 2) and baths == 1.0
    assert price is None, f"{trailing!r} is not a rent"


def test_a_size_is_never_mistaken_for_a_rent() -> None:
    """No price part at all leaves the rent unknown rather than guessed."""
    span, baths, price = RentSFNowSource._criteria("2  Beds \\\\ 1  Bath")

    assert span == (2, 2) and baths == 1.0
    assert price is None


def test_a_unit_the_feed_has_switched_off_is_dropped() -> None:
    """The feed carries an `active` flag and every live unit sets it. A unit it
    has switched off is one the landlord has stopped letting."""
    payload = json.loads(page("page1"))
    assert all(unit.get("active") for unit in payload["units"]), "every real unit is active"
    retired = dict(payload["units"][0], active=0)
    payload["units"] = [retired, *payload["units"][1:]]

    listings, _ = found(None, FakeResponse(json.dumps(payload)), FakeResponse('{"units": []}'))

    assert retired["id"] not in {listing.source_id for listing in listings}
    assert listings, "and the rest are still read"
