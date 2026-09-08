"""Zillow, read directly rather than waited on.

Zillow was a setup source here for as long as this app has existed: connect an
inbox, save a search on Zillow, wait for it to email you. It had delivered
nothing. Its own search page answers an ordinary request -- including one that
identifies itself honestly, which is rarer here than the browser string most of
these need -- and carries 41 rentals of a stated 2,568 in the page itself.

The fixture is real captured records: a building letting three sizes, a building
letting one, a single home whose numbers are numbers, a real Oakland record, and
one bent to FOR_SALE at $1,495,000. That last one is the single check standing
between this source and a million-dollar "rent", because a sale and a let are
the same record with a different status.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from sf_housing.classification import ROOM, WHOLE_UNIT
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import SourceError, ZillowSource, default_sources
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"

PRISM = "37.781826--122.41123"        # building, three sizes
TRINITY = "37.777885--122.41319"      # building, one size
MISSION = "459079645"                 # a single home, and the 32767 placeholder
OAKLAND_CITY = "Oakland"
SALE = "sale-1"


def search_page() -> str:
    return (FIXTURES / "zillow_search.html").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class FakeClient:
    """Serves each queued page once, then repeats the last one."""

    def __init__(self, *pages: FakeResponse):
        self.pages = list(pages) or [FakeResponse("<html></html>")]
        self.requested: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url, **kwargs):
        self.requested.append(url)
        self.headers = kwargs.get("headers") or {}
        return self.pages[min(len(self.requested) - 1, len(self.pages) - 1)]


def profile(*paths: str, maximum: int = 12000) -> Preferences:
    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": list(paths),
                    "budgets": {
                        path: {"maximum_monthly": maximum, "minimum_monthly": 100, "occupants": 3}
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


def found(preferences: Preferences, page: str | None = None):
    return ZillowSource().search(FakeClient(FakeResponse(page or search_page())), preferences)


def by_id(listings):
    return {item.source_id: item for item in listings}


def doctored(identifier: str, **changes) -> str:
    """The captured page with one record's fields changed."""
    page = search_page()
    payload = json.loads(
        re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    results = payload["props"]["pageProps"]["searchPageState"]["cat1"]["searchResults"]["listResults"]
    target = next(x for x in results if str(x.get("id")) == identifier)
    target.update(changes)
    return (
        '<html><body><script id="__NEXT_DATA__">' + json.dumps(payload) + "</script></body></html>"
    )


# --------------------------------------------------------------------------
# the check that matters most
# --------------------------------------------------------------------------


def test_a_home_for_sale_is_never_read_as_a_home_to_let(preferences) -> None:
    """A sale and a let are the same record with a different statusType, and
    the price field means a sale price on one and a monthly rent on the other.
    The fixture carries one bent to FOR_SALE at $1,495,000."""
    stored = by_id(found(preferences))

    assert SALE not in stored
    assert all(item.price is None or item.price < 100_000 for item in stored.values())


def test_the_status_is_what_is_checked_not_the_url(preferences) -> None:
    page = doctored(MISSION, statusType="FOR_SALE", unformattedPrice=1_650_000)
    assert MISSION not in by_id(found(preferences, page))


def test_a_home_in_another_city_is_left_on_the_page(preferences) -> None:
    """The fixture carries a real Oakland record from Zillow's Oakland page."""
    for item in found(preferences):
        assert OAKLAND_CITY.lower() not in (item.metadata.get("address") or "").lower()
    assert len(found(preferences)) == 3


# --------------------------------------------------------------------------
# two shapes on one page
# --------------------------------------------------------------------------


def test_both_shapes_on_the_page_are_read(preferences) -> None:
    """Buildings carry `units` with rents as text; single homes carry numbers.
    Read as one shape, four homes in every page of 41 lose their bedroom count
    and 37 lose their rent."""
    assert set(by_id(found(preferences))) == {PRISM, TRINITY, MISSION}


def test_a_building_is_priced_at_the_size_the_deal_asked_for() -> None:
    """Prism lets a studio at $3,675, a one-bedroom at $4,517 and a
    two-bedroom at $4,973."""
    assert by_id(ZillowSource().search(FakeClient(FakeResponse(search_page())), profile("studio")))[PRISM].price == 3675
    assert by_id(ZillowSource().search(FakeClient(FakeResponse(search_page())), profile("one_bedroom")))[PRISM].price == 4517
    assert by_id(ZillowSource().search(FakeClient(FakeResponse(search_page())), profile("two_bedroom")))[PRISM].price == 4973


def test_a_buildings_other_sizes_are_reported_without_repeating_the_one_quoted() -> None:
    summary = by_id(ZillowSource().search(
        FakeClient(FakeResponse(search_page())), profile("studio")
    ))[PRISM].summary or ""

    assert "Its studio homes start at $3,675 a month." in summary
    assert "also lets a 1-bedroom from $4,517, a 2-bedroom from $4,973" in summary
    assert summary.count("$3,675") == 1


def test_a_single_home_reads_its_own_numbers(preferences) -> None:
    listing = by_id(found(preferences))[MISSION]

    assert listing.price == 2300
    assert listing.metadata["bedrooms"] == 0
    assert listing.metadata["floor_area"] == "265 sq ft"
    assert "1 bathroom." in (listing.summary or "")
    assert listing.listing_type == "Home"


def test_a_bedroom_count_written_as_the_word_studio_is_still_a_studio(preferences) -> None:
    """Zillow writes these as strings, and a studio arrives as "0" on some
    buildings and "Studio" on others. Read only as digits, the second becomes
    no bedroom count at all."""
    page = doctored(TRINITY, units=[{"price": "$2,900+", "beds": "Studio", "roomForRent": False}])
    listing = by_id(found(preferences, page))[TRINITY]

    assert listing.metadata["bedrooms"] == 0
    assert listing.price == 2900


def test_a_room_let_inside_a_home_is_not_scored_as_a_whole_home(preferences) -> None:
    """Zillow marks this on the unit. None of the captured pages carried one,
    so it is induced here -- a lodger's room reaching a whole-home deal is the
    failure, and it must not wait for the first one to appear in the wild."""
    page = doctored(TRINITY, units=[{"price": "$1,400+", "beds": "1", "roomForRent": True}])

    assert by_id(found(preferences, page))[TRINITY].housing_kind == ROOM
    assert by_id(found(preferences))[TRINITY].housing_kind == WHOLE_UNIT


def test_what_is_free_is_never_recorded_as_the_size_of_the_building(preferences) -> None:
    listing = by_id(found(preferences))[PRISM]

    assert listing.metadata["homes_available"] == 6
    assert listing.building_units is None
    assert "6 units" not in (listing.summary or "")


# --------------------------------------------------------------------------
# identity and links
# --------------------------------------------------------------------------


def test_one_building_listed_twice_does_not_overwrite_itself(preferences) -> None:
    """Zillow lists a building as itself and again as a unit inside it, both
    pointing at one detail page: Vara arrived as a studio building at $3,852
    and as 1863 Mission St #306 at $3,300. `canonical_url` is unique, so
    stored as they come the second silently replaces the first."""
    from sf_housing.database import canonicalize_url

    same_page = doctored(TRINITY, detailUrl=json.loads(
        re.search(r'"detailUrl":\s*("[^"]+")', search_page()).group(1)
    ))
    links = {canonicalize_url(item.original_url) for item in found(preferences, same_page)}

    assert len(links) == len(found(preferences, same_page))


def test_the_link_is_absolute_and_on_zillow(preferences) -> None:
    """Some detailUrls arrive as a path and some as a full address."""
    for listing in found(preferences):
        assert listing.original_url.startswith("https://www.zillow.com/")


def test_a_link_pointing_at_another_host_is_refused(preferences) -> None:
    assert MISSION not in by_id(found(preferences, doctored(MISSION, detailUrl="https://evil.example/x")))


def test_the_placeholder_unit_number_is_not_shown_to_anybody(preferences) -> None:
    """32767 is the largest signed 16-bit integer, and Zillow publishes it
    where a home has no unit -- on Trulia too, which it owns."""
    listing = by_id(found(preferences))[MISSION]

    assert listing.title == "1825 Mission St"
    assert "32767" not in (listing.summary or "")
    assert listing.metadata["address"] == "1825 Mission St"


def test_every_home_publishes_the_address_corroboration_matches_on(preferences) -> None:
    from sf_housing.location import parse_street_address

    for listing in found(preferences):
        assert parse_street_address(listing.metadata["address"]) is not None


# --------------------------------------------------------------------------
# paging, walls, and cost
# --------------------------------------------------------------------------


def test_paging_stops_when_the_page_repeats_itself(preferences) -> None:
    client = FakeClient(FakeResponse(search_page()))
    ZillowSource().search(client, preferences)

    assert len(client.requested) == 2
    assert client.requested[0] == "https://www.zillow.com/san-francisco-ca/rentals/"
    assert client.requested[1] == "https://www.zillow.com/san-francisco-ca/rentals/2_p/"


def test_the_nightly_sweep_reads_far_deeper_than_a_waiting_scan() -> None:
    """2,568 rentals is 63 pages. Six of them is a scan's worth."""
    from sf_housing.sources import DEEP_SWEEP_TRIGGER, _pages_for_trigger

    source = ZillowSource()
    assert _pages_for_trigger(source, "scheduled") == 6
    assert _pages_for_trigger(source, DEEP_SWEEP_TRIGGER) == 65


def test_the_per_source_cap_is_honoured() -> None:
    preferences = parse_preferences(
        yaml.safe_dump({**yaml.safe_load(TEST_PREFERENCES), "sources": {"max_results_per_source": 2}})
    )
    assert len(ZillowSource().search(FakeClient(FakeResponse(search_page())), preferences)) == 2


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse("", 202),
        FakeResponse("", 403),
        FakeResponse("", 429),
        FakeResponse("   ", 200),
        FakeResponse("<html><body>Please verify you are a human</body></html>", 200),
        FakeResponse("<html><title>Just a moment...</title></html>", 200),
    ],
    ids=["202-empty", "403", "429", "blank-200", "captcha", "cloudflare"],
)
def test_a_wall_is_never_read_as_an_empty_result(response, preferences) -> None:
    with pytest.raises(SourceError):
        ZillowSource().search(FakeClient(response), preferences)


@pytest.mark.parametrize(
    "payload_text",
    [
        "{not json",
        "[1,2,3]",
        '{"props": {}}',
        '{"props": []}',
        '{"props": {"pageProps": {"searchPageState": {"cat1": {"searchResults": {"listResults": "no"}}}}}}',
    ],
    ids=["malformed", "not-an-object", "no-searchPageState", "props-is-a-list", "results-wrong-type"],
)
def test_a_payload_this_no_longer_understands_is_reported_not_crashed(
    payload_text, preferences
) -> None:
    page = f'<html><body><script id="__NEXT_DATA__">{payload_text}</script></body></html>'
    with pytest.raises(SourceError) as error:
        ZillowSource().search(FakeClient(FakeResponse(page)), preferences)
    assert "format may have changed" in str(error.value)


def test_junk_records_inside_a_good_payload_are_skipped_not_fatal(preferences) -> None:
    page = search_page()
    payload = json.loads(
        re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    results = payload["props"]["pageProps"]["searchPageState"]["cat1"]["searchResults"]
    results["listResults"] = [None, "nonsense", {}, *results["listResults"]]
    doctored_page = (
        '<html><body><script id="__NEXT_DATA__">' + json.dumps(payload) + "</script></body></html>"
    )

    assert len(found(preferences, doctored_page)) == 3


def test_a_page_of_only_out_of_town_homes_is_read_not_raised(preferences) -> None:
    page = search_page().replace('"San Francisco"', '"Oakland"')
    assert ZillowSource().search(FakeClient(FakeResponse(page)), preferences) == []


# --------------------------------------------------------------------------
# it stopped being a setup source
# --------------------------------------------------------------------------


def test_zillow_runs_without_anybody_connecting_anything() -> None:
    """It was a setup source for as long as this app has existed -- connect an
    inbox, save a search on Zillow, wait -- and had delivered nothing."""
    class FakeMailbox:
        credential = None

        def configured(self):
            return False

    for label, sources in (
        ("no inbox", default_sources()),
        ("inbox connected", default_sources(FakeMailbox())),
    ):
        zillow = [item for item in sources if item.platform == "Zillow"]
        assert len(zillow) == 1, f"{label}: {len(zillow)} sources called Zillow"
        assert zillow[0].mode == "automatic", label
        assert not getattr(zillow[0], "connector_key", None), label


def test_the_page_no_longer_offers_zillow_as_something_to_set_up(tmp_path) -> None:
    """The email list is derived from what is not checked directly, so this
    follows on its own -- which is how Zumper left it. Asserted on the rendered
    page because the day it stops following, somebody is asked to set up a
    source that already runs, to add nothing."""
    import re as _re

    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.preferences import ensure_preferences
    from sf_housing.settings import Settings

    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    data.mkdir(parents=True, exist_ok=True)
    (data / "config").mkdir(parents=True, exist_ok=True)
    # A finished deal, or the page redirects to onboarding instead.
    from tests.test_named_checks import PREFERENCES

    settings.preferences_path.write_text(PREFERENCES, encoding="utf-8")
    ensure_preferences(settings.preferences_path)
    with TestClient(create_app(settings=settings, sources=None, enable_scheduler=False)) as client:
        page = client.get("/alerts").text

    already = page[page.index('class="already-list"') : page.index("</p>", page.index('class="already-list"'))]
    assert ">Zillow<" in already, "Zillow is not shown among the sources that already work"

    # And it is gone from the four the page still asks somebody to connect.
    steps = page[page.index("already-list") :]
    offered = _re.findall(r'<summary>.*?>([A-Za-z. ]+)</span>\s*<span>about', steps, _re.S)
    assert "Zillow" not in offered, offered


def test_zillow_needs_no_detail_fetch() -> None:
    assert ZillowSource.detail_budget == 0
    assert not hasattr(ZillowSource, "enrich")


def test_the_source_keeps_no_per_deal_state() -> None:
    assert vars(ZillowSource()) == {}


def test_being_turned_away_is_reported_as_rate_limiting_not_breakage(preferences) -> None:
    """A 403 and a page whose shape changed are different problems with
    different answers -- wait, or fix the parser. Read as a page of zero
    results both come out as "the format may have changed", which sends
    somebody to look at code that is fine."""
    with pytest.raises(SourceError) as refused:
        ZillowSource().search(FakeClient(FakeResponse("", 429)), preferences)
    assert "rate-limiting rather than broken" in str(refused.value)

    with pytest.raises(SourceError) as changed:
        ZillowSource().search(
            FakeClient(FakeResponse("<html><body>hello</body></html>")), preferences
        )
    assert "format may have changed" in str(changed.value)


def test_zillow_runs_before_the_sources_that_fetch_a_page_per_building(
    repository, preferences
) -> None:
    """Six search pages and no detail reads, and the most reliable of the
    large portals. It must not queue behind the ones that fetch a page per
    building."""
    from sf_housing.scanner import Scanner
    from sf_housing.sources import ApartmentListSource, RentComSource

    scanner = Scanner(
        repository,
        lambda: preferences,
        [RentComSource(), ApartmentListSource(), ZillowSource()],
        detail_delay_seconds=0,
    )
    order = [item.platform for item in scanner._eligible_sources("scheduled")]

    assert order.index("Zillow") < order.index("Apartment List")
    assert order.index("Zillow") < order.index("Rent.com")
