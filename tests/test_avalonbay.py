"""AvalonBay's San Francisco page, which ships its own data.

The fixture is a real capture, trimmed and kept deliberately awkward: it holds
the San Bruno and Pacifica units the city filter has to drop, both operators
(AvalonBay's own buildings and the Equity Residential stock it markets), every
bedroom size, and both furnish states.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from sf_housing.classification import WHOLE_UNIT, classify_listing
from sf_housing.database import canonicalize_url
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.scoring import _available_on
from sf_housing.sources import AvalonBaySource, SourceError, default_sources
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"


def page() -> str:
    return (FIXTURES / "avalonbay_search.html").read_text(encoding="utf-8")


def blob(text: str | None = None) -> dict:
    return json.loads(AvalonBaySource.BLOB.search(text or page()).group(1))


def rewrap(payload: dict) -> str:
    return (
        "<html><head></head><body><script>Fusion.globalContent="
        + json.dumps(payload, separators=(",", ":"))
        + ";</script></body></html>"
    )


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.response


def deal(*paths: str) -> Preferences:
    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": list(paths),
                    "budgets": {
                        path: {"maximum_monthly": 9000, "minimum_monthly": 100, "occupants": 2}
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


def found(prefs: Preferences | None = None, text: str | None = None):
    client = FakeClient(FakeResponse(text or page()))
    return AvalonBaySource().search(client, prefs or deal("studio", "one_bedroom", "two_bedroom")), client


# --------------------------------------------------------------------------
# the city, which the page's own title gets wrong
# --------------------------------------------------------------------------


def test_only_san_francisco_is_kept() -> None:
    """The page is titled San Francisco and answers with San Bruno and
    Pacifica in it: thirty-seven of a hundred and thirty-seven, measured."""
    units = blob()["unitResults"]["items"]
    outside = [u for u in units if u["address"]["city"] != "San Francisco"]
    assert len(outside) >= 5, "fixture must carry the out-of-city units"

    listings, _ = found()

    assert listings
    assert len(listings) == len(units) - len(outside)
    for listing in listings:
        assert "San Bruno" not in listing.summary and "Pacifica" not in listing.summary


# --------------------------------------------------------------------------
# one link per home, which is what lets more than one be stored
# --------------------------------------------------------------------------


def test_every_home_gets_a_link_of_its_own() -> None:
    """listings.canonical_url is UNIQUE. Pointing every unit at the search page
    would file the first home and silently discard the rest."""
    listings, _ = found()

    canonical = [canonicalize_url(listing.original_url) for listing in listings]
    assert len(set(canonical)) == len(canonical) > 1
    for url in canonical:
        assert "unit=" in url


def test_a_marketing_tag_is_not_part_of_a_home_s_identity() -> None:
    """AvalonBay hangs campaign parameters off its Equity Residential links.
    Left on, the same home would arrive as a new one whenever they changed."""
    listings, _ = found()
    tagged = [x for x in listings if "utm_" in x.original_url]

    for listing in listings:
        assert "utm_" not in canonicalize_url(listing.original_url)
    if tagged:
        assert canonicalize_url(tagged[0].original_url) != tagged[0].original_url


def test_the_id_is_the_feed_s_own() -> None:
    listings, _ = found()
    for listing in listings:
        assert listing.source_id.startswith("AVB-"), listing.source_id


# --------------------------------------------------------------------------
# what a unit says
# --------------------------------------------------------------------------


def test_a_unit_arrives_complete(preferences) -> None:
    listings, client = found(preferences)

    assert len(client.requested) == 1, "one request for the whole city"
    assert listings
    for listing in listings:
        assert listing.platform == "AvalonBay"
        assert listing.housing_kind == WHOLE_UNIT
        assert listing.price and listing.price > 0
        assert "bedrooms" in listing.metadata
        assert listing.metadata["address"]


def test_the_move_in_date_is_one_the_scoring_can_read() -> None:
    """An ISO date under a key of its own is a fact nothing consults."""
    listings, _ = found()

    dated = [x for x in listings if x.metadata.get("available_on")]
    assert len(dated) == len(listings), "every unit publishes a date"
    for listing in dated:
        assert _available_on(listing) is not None


def test_the_unfurnished_price_is_the_one_quoted() -> None:
    """A furnished quote is a different product at a different rent, and
    "OnDemand" means furnishing is offered, not that the home comes with it."""
    units = {u["unitId"]: u for u in blob()["unitResults"]["items"]}
    listings, _ = found()

    for listing in listings:
        unit = units[listing.source_id]
        assert listing.price == int(unit["startingAtPricesUnfurnished"]["prices"]["price"])


def test_a_home_too_small_for_the_deal_is_dropped() -> None:
    wide, _ = found(deal("studio", "one_bedroom", "two_bedroom"))
    two, _ = found(deal("two_bedroom"))

    assert len(two) < len(wide)
    for listing in two:
        assert listing.metadata["bedrooms"] >= 2


def test_a_size_the_feed_states_is_the_size_it_is_scored_as() -> None:
    listings, _ = found()
    studios = [x for x in listings if x.metadata["bedrooms"] == 0]

    assert studios
    assert classify_listing(studios[0]).unit_type == "studio"


def test_the_unit_list_is_avalonbays_own_stock() -> None:
    """Eight of the sixteen buildings are Equity Residential's, and it was
    worth a line saying so until the feed was counted: they contribute no
    units. Asserting it here so that if AvalonBay ever does start listing
    them, this fails and somebody decides what to say about them."""
    payload = blob()
    eqr = {
        c["communityId"]
        for c in payload["communityResults"]["communities"]["items"]
        if c.get("communityType") == "EQR"
    }
    assert eqr, "the page still lists another operator's buildings"

    borrowed = [u for u in payload["unitResults"]["items"] if u["communityId"] in eqr]

    assert not borrowed, "AvalonBay has started publishing units for buildings it does not own"


# --------------------------------------------------------------------------
# how it fails
# --------------------------------------------------------------------------


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
        AvalonBaySource().search(FakeClient(response), preferences)


def test_a_blob_that_stopped_being_json_is_not_an_empty_city(preferences) -> None:
    broken = "<html><script>Fusion.globalContent={not json at all};</script></html>"
    with pytest.raises(SourceError, match="valid JSON"):
        AvalonBaySource().search(FakeClient(FakeResponse(broken)), preferences)


def test_a_blob_without_units_is_named_as_that(preferences) -> None:
    payload = blob()
    del payload["unitResults"]["items"]
    with pytest.raises(SourceError, match="no unit list"):
        AvalonBaySource().search(FakeClient(FakeResponse(rewrap(payload))), preferences)


def test_a_page_with_no_homes_is_empty_and_not_an_error(preferences) -> None:
    payload = blob()
    payload["unitResults"]["items"] = []
    assert AvalonBaySource().search(FakeClient(FakeResponse(rewrap(payload))), preferences) == []


def test_no_detail_pages_are_read() -> None:
    assert AvalonBaySource.detail_budget == 0
    assert not hasattr(AvalonBaySource, "enrich")


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_it_runs_without_setup_in_every_branch() -> None:
    class Mailbox:
        pass

    for sources in (default_sources(), default_sources(Mailbox())):
        registered = [x for x in sources if x.platform == "AvalonBay"]
        assert len(registered) == 1
        assert registered[0].mode == "automatic"
        assert registered[0].manual_reason is None
        assert getattr(registered[0], "connector_key", None) is None


def test_the_ready_check_still_probes_the_same_four() -> None:
    probed = [
        source.platform
        for source in default_sources()
        if getattr(source, "mode", "setup") == "automatic"
        and not getattr(source, "connector_key", None)
    ][:4]
    assert probed == ["Craigslist", "Listings Project", "Abacus (small buildings)", "SpareRoom"]
