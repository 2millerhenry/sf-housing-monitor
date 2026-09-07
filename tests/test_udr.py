"""UDR, which prices a building by bedroom size rather than by home."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sf_housing.classification import WHOLE_UNIT, classify_listing
from sf_housing.database import canonicalize_url
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import SourceError, UDRSource, default_sources
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"


def page() -> str:
    return (FIXTURES / "udr_search.html").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response, self.requested = response, []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.response


def deal(*paths: str) -> Preferences:
    return parse_preferences(
        yaml.safe_dump({"profile_version": 1, "profile": {
            "state": "active", "enabled_paths": list(paths),
            "budgets": {p: {"maximum_monthly": 9000, "minimum_monthly": 100, "occupants": 2} for p in paths},
            "geography": {"anywhere_in_sf": True}}}))


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


def found(prefs: Preferences | None = None, text: str | None = None):
    client = FakeClient(FakeResponse(text or page()))
    return UDRSource().search(client, prefs or deal("studio", "one_bedroom", "two_bedroom")), client


def test_a_building_becomes_one_candidate_per_size() -> None:
    """A building offering a studio and a one-bedroom is two things a reader
    judges separately; a single "from" price hides the one they want."""
    listings, _ = found()

    assert len(listings) >= 12
    by_building: dict[str, set[int]] = {}
    for listing in listings:
        slug, size = listing.source_id.split(":")
        by_building.setdefault(slug, set()).add(int(size))
    assert any(len(sizes) > 1 for sizes in by_building.values())


def test_a_building_is_not_named_after_a_marketing_page() -> None:
    """Links end /399-fremont/specials/ and /2000-post/specials/. Named on the
    last segment, five buildings were all called "specials" and four of them
    were deduplicated away in silence."""
    listings, _ = found()
    slugs = {listing.source_id.split(":")[0] for listing in listings}

    assert "specials" not in slugs
    assert len(slugs) >= 5, f"every building keeps its own name: {slugs}"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("https://www.udr.com/a/b/399-fremont/specials/", "399-fremont"),
        ("https://www.udr.com/a/b/388-beale/", "388-beale"),
        ("https://www.udr.com/x/west-soma/hq-apartments/specials/", "hq-apartments"),
        ("https://www.udr.com/a/edgewater/floorplans/", "edgewater"),
    ],
)
def test_the_building_name_survives_a_marketing_suffix(path, expected) -> None:
    assert UDRSource._building_slug(path) == expected


def test_a_size_with_nothing_free_is_not_a_free_home() -> None:
    """"0 Available Apartments" sits where the rent goes. Read as a number that
    is a home going for nothing."""
    assert "0 Available Apartments" in page(), "the fixture must carry one"
    listings, _ = found()

    for listing in listings:
        assert listing.price and listing.price > 1000
    # HQ publishes no one-bedroom; it must not appear at any price.
    hq = {listing.source_id for listing in listings if listing.source_id.startswith("hq")}
    assert "hq-apartments:1" not in hq


def test_every_candidate_gets_a_link_of_its_own() -> None:
    listings, _ = found()
    canonical = [canonicalize_url(listing.original_url) for listing in listings]

    assert len(set(canonical)) == len(canonical)
    assert len({listing.source_id for listing in listings}) == len(listings)


def test_only_san_francisco_is_kept() -> None:
    listings, _ = found()
    assert listings
    assert not any("across the bay" in listing.title for listing in listings)
    for listing in listings:
        assert listing.platform == "UDR"
        assert listing.housing_kind == WHOLE_UNIT


def test_the_summary_says_it_is_a_building_rate() -> None:
    """A starting rent for a size is not one home's rent, and a reader acting
    on it needs to know which they have."""
    listings, _ = found()
    assert all("not one home" in listing.summary for listing in listings)


def test_a_size_too_small_for_the_deal_is_dropped() -> None:
    wide, _ = found(deal("studio", "one_bedroom", "two_bedroom"))
    two, _ = found(deal("two_bedroom"))

    assert len(two) < len(wide)
    for listing in two:
        assert listing.metadata["bedrooms"] >= 2


def test_a_size_is_scored_as_that_size() -> None:
    listings, _ = found()
    studios = [x for x in listings if x.metadata["bedrooms"] == 0]
    assert studios
    assert classify_listing(studios[0]).unit_type == "studio"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse("", 202), "HTTP 202"),
        (FakeResponse("   ", 200), "empty page"),
        (FakeResponse("nope", 429), "turned away"),
        (FakeResponse("<html><body>Apartments</body></html>"), "format may have changed"),
        (FakeResponse("<html>press & hold</html>"), "bot check"),
    ],
)
def test_an_answer_that_is_not_the_page_says_which(response, expected, preferences) -> None:
    with pytest.raises(SourceError, match=expected):
        UDRSource().search(FakeClient(response), preferences)


def test_no_detail_pages_are_read() -> None:
    assert UDRSource.detail_budget == 0
    assert not hasattr(UDRSource, "enrich")


def test_it_runs_without_setup_in_every_branch() -> None:
    class Mailbox:
        pass

    for sources in (default_sources(), default_sources(Mailbox())):
        registered = [x for x in sources if x.platform == "UDR"]
        assert len(registered) == 1
        assert registered[0].mode == "automatic"
        assert getattr(registered[0], "connector_key", None) is None


def test_the_ready_check_still_probes_the_same_four() -> None:
    probed = [
        source.platform for source in default_sources()
        if getattr(source, "mode", "setup") == "automatic" and not getattr(source, "connector_key", None)
    ][:4]
    assert probed == ["Craigslist", "Listings Project", "Abacus (small buildings)", "SpareRoom"]


def test_an_available_size_with_no_rent_is_not_a_two_dollar_home() -> None:
    """A size with nothing free reads "0 Available Apartments"; a size that has
    some but publishes no rent reads "3 Available Apartments". Read as a
    number, the second is a home going for three dollars a month -- and the
    zero is caught by being falsy, which is luck rather than a guard."""
    assert "3 Available Apartments" in page(), "the fixture must carry one"

    listings, _ = found()
    priceless = [x for x in listings if x.source_id.startswith("priceless-place")]

    assert priceless, "the building's other sizes are still read"
    assert "priceless-place:1" not in {x.source_id for x in listings}
    for listing in listings:
        assert listing.price and listing.price > 1000, listing.title


def test_a_building_in_another_city_is_dropped_on_the_city() -> None:
    """The out-of-city card is a building of its own, with its own link. Sharing
    a link with a San Francisco building would have it deduplicated away
    instead, which looks like the filter working and is not."""
    assert "1200 Broadway" in page() and "Oakland, CA" in page()

    listings, _ = found()

    assert not any(x.source_id.startswith("1200-broadway") for x in listings)
    assert not any("across the bay" in x.title for x in listings)
    assert listings, "and the San Francisco buildings are still read"
