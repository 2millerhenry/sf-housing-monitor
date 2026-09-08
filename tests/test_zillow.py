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

import httpx
import pytest
import yaml

from sf_housing.classification import ROOM, WHOLE_UNIT
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import (
    DEEP_SWEEP_TRIGGER,
    SourceError,
    ZillowSource,
    default_sources,
)
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


def test_the_nightly_sweep_reads_at_least_as_deep_as_a_waiting_scan() -> None:
    """The page claims 2,568 rentals and hands over about a thousand: page 25
    is refused however patiently it is asked. So the sweep's job here is to
    probe a little past today's wall, not to chase a number paging cannot
    reach."""
    from sf_housing.sources import DEEP_SWEEP_TRIGGER, _pages_for_trigger

    source = ZillowSource()
    assert _pages_for_trigger(source, "scheduled") == source.max_pages
    assert _pages_for_trigger(source, DEEP_SWEEP_TRIGGER) >= source.max_pages


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


# --------------------------------------------------------------------------
# how deep the search actually goes
# --------------------------------------------------------------------------


class Refused:
    """A refusal that raises the way httpx does, rather than asserting."""

    status_code = 400
    text = ""

    def raise_for_status(self):
        raise httpx.HTTPStatusError("400", request=None, response=None)


def renamed(page: str, suffix: str) -> str:
    """The same page with every id changed, standing in for a later page.

    Both keys, because the reader takes whichever it finds first and a page
    whose homes are all already seen ends the search on its own.
    """
    for key in ("id", "zpid"):
        page = re.sub(
            rf'"{key}":"([^"]+)"', lambda m: f'"{key}":"{m.group(1)}{suffix}"', page
        )
    return page


def test_the_wall_at_the_end_of_the_results_is_not_an_error(preferences) -> None:
    """Zillow serves about a thousand homes and then refuses the next page
    outright rather than answering with an empty one. Raising there would
    throw away every home already read in order to report the page after the
    last one."""
    page_one = FakeResponse(search_page())
    page_two = FakeResponse(renamed(search_page(), "b"))
    wall = Refused()
    client = FakeClient(page_one, page_two, wall)

    listings = ZillowSource().search(client, preferences)

    assert listings, "the homes read before the wall have to survive it"
    assert len(client.requested) == 3, "it stops asking once it is refused"


def test_a_refusal_on_the_very_first_page_is_still_a_failure(preferences) -> None:
    """Nothing has been read yet, so this is Zillow turning the app away
    rather than the end of the results, and it has to be reported."""
    client = FakeClient(Refused())

    with pytest.raises((SourceError, httpx.HTTPStatusError)):
        ZillowSource().search(client, preferences)


def test_the_search_reads_far_enough_to_reach_what_zillow_serves() -> None:
    """Six pages was 246 homes out of the roughly one thousand Zillow will
    actually hand over, so three quarters of the reachable inventory was
    never asked for."""
    source = ZillowSource()

    assert source.max_pages >= 24, "the reachable pages are not being read"
    # Page 25 is refused, so aiming far past it only buys refused requests.
    assert source.deep_max_pages <= 40
    assert source.deep_max_pages >= source.max_pages


# --------------------------------------------------------------------------
# past the cap: several narrower searches instead of one wide one
# --------------------------------------------------------------------------


class BandClient:
    """Answers per URL, so a test can give each band its own results."""

    def __init__(self, pages: dict[str, FakeResponse] | None = None, default: FakeResponse | None = None):
        self.pages = pages or {}
        self.default = default or FakeResponse(search_page())
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        for fragment, response in self.pages.items():
            if fragment in url:
                return response
        return self.default


def band_state(url: str) -> dict:
    """The query state Zillow is actually being asked for."""
    from urllib.parse import parse_qs, urlsplit

    raw = parse_qs(urlsplit(url).query).get("searchQueryState", [""])[0]
    return json.loads(raw) if raw else {}


def test_a_check_somebody_pressed_reads_one_plain_search(preferences) -> None:
    """Slicing costs several times the requests. A person waiting on a spinner
    is not who should pay for the long tail."""
    client = BandClient()
    ZillowSource().search_for_trigger(client, preferences, "scheduled")

    assert all("searchQueryState" not in url for url in client.requested), client.requested[:3]


def test_the_nightly_sweep_asks_every_band(preferences) -> None:
    """One search reaches 984 homes of the 2,565 Zillow states it holds. The
    rest are only reachable by asking narrower questions."""
    source = ZillowSource()
    client = BandClient()
    source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    asked = [band_state(url)["filterState"]["mp"] for url in client.requested if "searchQueryState" in url]
    for low, high in source.PRICE_BANDS:
        wanted = {}
        if low is not None:
            wanted["min"] = low
        if high is not None:
            wanted["max"] = high
        assert wanted in asked, f"band {low}-{high} was never asked for"


def test_every_band_lands_under_the_cap_zillow_enforces() -> None:
    """The whole point is that each question is narrow enough to be answered
    in full. Measured against live Zillow, the largest band holds 671 homes
    against a cap of about 984; a band over it loses its overflow in exactly
    the silence this exists to end."""
    source = ZillowSource()
    reachable = source.max_pages * 41

    assert source.PRICE_BANDS[0][0] is None, "the cheapest homes need no floor"
    assert source.PRICE_BANDS[-1][1] is None, "the dearest need no ceiling"
    edges = [high for _, high in source.PRICE_BANDS[:-1]]
    assert edges == sorted(edges), "the bands have to climb"
    for index, (low, high) in enumerate(source.PRICE_BANDS[1:], start=1):
        assert low == source.PRICE_BANDS[index - 1][1], "a gap between bands is homes nobody asks for"
    assert reachable >= 900, "the cap this is measured against moved"


def test_a_band_asks_zillow_for_rentals_only(preferences) -> None:
    """Without it the bands fill with homes for sale, whose prices mean
    something else entirely and would land in the pool as rents."""
    state = band_state(ZillowSource()._band_url((2000, 3000), 1))

    assert state["filterState"]["fr"] == {"value": True}
    assert state["filterState"]["fsba"] == {"value": False}


def test_a_band_is_never_asked_for_as_a_path_filter() -> None:
    """Zillow accepts "2500-3500_price/" and "2500-3500_mp/" and ignores both,
    answering 200 with the same unfiltered page -- 12 of 94 prices inside the
    band asked for. Slicing on those is eight copies of one search that all
    look right."""
    url = ZillowSource()._band_url((2500, 3500), 1)

    assert "_price/" not in url and "_mp/" not in url
    assert "searchQueryState=" in url


def test_page_two_of_a_band_asks_for_page_two_of_that_band() -> None:
    """The page number lives inside the query state here, not in the path, so
    a band that paginated the old way would read its first page nine times."""
    source = ZillowSource()

    assert "pagination" not in band_state(source._band_url((2000, 3000), 1))
    assert band_state(source._band_url((2000, 3000), 2))["pagination"] == {"currentPage": 2}


def test_a_home_in_two_bands_reaches_the_pool_once(preferences) -> None:
    """A building whose rents straddle a boundary is returned on both sides of
    it, and canonical_url is UNIQUE."""
    client = BandClient()
    listings = ZillowSource().search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    identifiers = [item.source_id for item in listings]
    assert len(identifiers) == len(set(identifiers)), "the same home came back twice"


def test_a_band_reads_its_own_pages_even_where_an_earlier_band_overlapped(
    preferences,
) -> None:
    """Whether a page is a band's last is a question about that band's own
    pages. Asked of every home found so far, a band stopped at its first page
    the moment that page held anything an earlier band already had -- so the
    homes deeper in it were never reached. That cost about a thousand homes
    against live Zillow before it was measured.

    Here every band's first page is identical, which is the worst case: with
    the question asked globally, only the first band ever gets past page one.
    """
    source = ZillowSource()
    # The same first page for every band, and a second page of new homes.
    client = BandClient(pages={"%22currentPage%22%3A2": FakeResponse(renamed(search_page(), "p2"))})

    source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    reached_page_two = [url for url in client.requested if "%22currentPage%22%3A2" in url]
    assert len(reached_page_two) == len(source.PRICE_BANDS), (
        "a band stopped at page one because an earlier band had already seen its homes"
    )


def test_a_refused_band_keeps_the_homes_the_others_found(preferences) -> None:
    """Zillow refuses outright past a search's last page. With bands, raising
    there would throw away the bands still to come as well."""
    source = ZillowSource()
    client = BandClient(pages={"%22min%22%3A4000": Refused()})

    listings = source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    assert listings, "one refused band lost every other band's homes"


class RateLimited:
    """Zillow turning an unattended request away, as httpx reports it."""

    status_code = 403
    text = ""

    def raise_for_status(self):  # pragma: no cover - _require_page acts first
        raise httpx.HTTPStatusError("403", request=None, response=None)


def test_being_turned_away_is_not_read_as_the_end_of_the_results(preferences) -> None:
    """The wall past a search's last page is a 400. A 403 is Zillow declining
    to serve us at all, and reading it as an ending would collect a little,
    report success, and come back tomorrow to be turned away again -- the
    backoff would never hear about it."""
    source = ZillowSource()
    client = BandClient(pages={"%22min%22%3A4000": RateLimited()})

    with pytest.raises(SourceError) as refused:
        source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    assert "403" in str(refused.value)


def test_pages_are_spaced_so_a_sweep_is_not_a_burst(preferences, monkeypatch) -> None:
    """Zillow blocks by address and it lasts: about three hundred requests over
    half an hour drew a 403 that outlived six hours, on a browser string and an
    honest one alike. Unpaced, eight bands is fifty-two requests inside a
    minute -- a burst rate several times the average that drew it."""
    import sf_housing.sources as sources

    waited: list[float] = []
    monkeypatch.setattr(sources.time, "sleep", lambda seconds: waited.append(seconds))
    client = BandClient()

    ZillowSource().search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    assert waited, "the pages go out as fast as the network allows"
    assert len(waited) == len(client.requested) - 1, "every page after the first waits"
    assert all(pause == ZillowSource.PAGE_PAUSE_SECONDS for pause in waited)


def test_the_pause_carries_across_bands(preferences, monkeypatch) -> None:
    """The rate Zillow sees is from this address. It does not reset because a
    new band started, so neither does the count."""
    import sf_housing.sources as sources

    waited: list[float] = []
    monkeypatch.setattr(sources.time, "sleep", lambda seconds: waited.append(seconds))
    client = BandClient()

    ZillowSource().search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    # One un-paused request in total, not one per band.
    assert len(client.requested) - len(waited) == 1


def test_the_pacing_still_fits_inside_both_ceilings() -> None:
    """A pause that outlasts the ceiling abandons the source, which loses more
    than a block would."""
    from sf_housing.scanner import (
        DEEP_SOURCE_CEILING_SECONDS,
        SOURCE_HARD_CEILING_SECONDS,
    )

    source = ZillowSource()
    interactive = source.max_pages * source.PAGE_PAUSE_SECONDS
    sweep = 52 * source.PAGE_PAUSE_SECONDS

    assert source.PAGE_PAUSE_SECONDS > 0, "the requests go out as one burst"
    assert interactive < SOURCE_HARD_CEILING_SECONDS / 2, "no room left to actually read"
    assert sweep < DEEP_SOURCE_CEILING_SECONDS / 2


def test_zillow_asks_not_to_be_read_again_within_the_quarter_hour() -> None:
    """Zillow blocks by address for hours, on rate. Twenty-four pages a press,
    pressed while waiting, is how a person earns that. A floor caps it at four
    reads an hour however often the button is pressed."""
    source = ZillowSource()
    floor_minutes = source.min_seconds_between_reads / 60
    reads_per_hour = 60 / floor_minutes
    worst_case_per_minute = reads_per_hour * source.max_pages / 60

    assert floor_minutes >= 10, "a press still costs a full read"
    # Roughly ten a minute sustained is what drew the block being defended against.
    assert worst_case_per_minute < 3, worst_case_per_minute


def test_the_page_leads_with_the_names_somebody_recognises(tmp_path) -> None:
    """The order on the page is not the order the sources are read in. Scan
    order opened the pitch with Craigslist, Listings Project and a small
    landlord nobody has heard of, and put Zillow fourteenth."""
    import re as _re

    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.preferences import ensure_preferences
    from sf_housing.settings import Settings
    from tests.test_named_checks import PREFERENCES

    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )
    data.mkdir(parents=True, exist_ok=True)
    (data / "config").mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(PREFERENCES, encoding="utf-8")
    ensure_preferences(settings.preferences_path)
    with TestClient(create_app(settings=settings, sources=None, enable_scheduler=False)) as client:
        page = client.get("/alerts").text

    start = page.index('class="already-list"')
    already = page[start : page.index("</p>", start)]
    # The name is the chip's own trailing text, after its logo or letter mark.
    shown = [n.strip() for n in _re.findall(r">\s*([A-Za-z][^<>]*?)\s*</span>", already) if n.strip()]

    assert shown[:3] == ["Zillow", "Trulia", "Redfin"], shown[:6]
    assert shown.index("Zillow") < shown.index("AvalonBay"), shown
