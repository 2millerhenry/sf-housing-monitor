"""A university off-campus housing board.

Every other source here reads a commercial listing site. This one is a student
board, and the fixture is a real capture kept deliberately awkward: it holds
the Berkeley, Oakland and BERKELEY cards the city check has to reject, the
"San francisco" card its case-folding has to keep, a card with no rent at all,
a studio whose size is never followed by the word "Bed", and a building that
quotes one rent for homes of several sizes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from bs4 import BeautifulSoup

from sf_housing.classification import ROOM, WHOLE_UNIT, classify_listing
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import SourceError, UloopSource, default_sources
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"


def board_page() -> str:
    return (FIXTURES / "uloop_board.html").read_text(encoding="utf-8")


def cards(page: str | None = None):
    return BeautifulSoup(page or board_page(), "html.parser").select(UloopSource.CARD)


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class FakeClient:
    def __init__(self, *pages: FakeResponse):
        self.pages = list(pages) or [FakeResponse("<html></html>")]
        self.requested: list[str] = []
        self.headers_seen: list[dict | None] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        self.headers_seen.append(kwargs.get("headers"))
        return self.pages[min(len(self.requested) - 1, len(self.pages) - 1)]


def deal(*paths: str) -> Preferences:
    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": list(paths),
                    "budgets": {
                        path: {"maximum_monthly": 4000, "minimum_monthly": 100, "occupants": 2}
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


def found(page: str | None = None, prefs: Preferences | None = None, *pages: FakeResponse):
    client = FakeClient(FakeResponse(page or board_page()), *(pages or (FakeResponse("<html></html>"),)))
    return UloopSource().search(client, prefs or deal("studio", "one_bedroom", "two_bedroom", "three_bedroom")), client


# --------------------------------------------------------------------------
# the city, which is what this source lives or dies on
# --------------------------------------------------------------------------


def test_only_san_francisco_is_kept() -> None:
    listings, _ = found()

    assert listings, "the fixture holds real San Francisco cards"
    for listing in listings:
        assert "uloop.com/housing/view.php/" in listing.original_url
        assert listing.platform == "Uloop"


def test_the_east_bay_is_rejected() -> None:
    """The board is a school's board, not a city's: a UCSF student is shown
    Berkeley and Oakland alongside San Francisco."""
    page = board_page()
    outside = [
        card for card in cards(page)
        if re.search(r",\s*(Berkeley|BERKELEY|Oakland|Emeryville),", card.get_text(" ", strip=True))
    ]
    assert len(outside) >= 5, "fixture must carry the out-of-city cards this check exists for"

    listings, _ = found(page)
    kept = {listing.original_url for listing in listings}
    for card in outside:
        assert card.find("a", href=True)["href"] not in kept


def test_the_city_is_matched_however_it_is_spelled() -> None:
    """The board really contains "San francisco" with a lowercase f, and
    "BERKELEY" in capitals. An exact string check silently drops the first;
    a list of cities to reject lets the next unlisted one through."""
    page = board_page()
    lower = [c for c in cards(page) if ", San francisco," in c.get_text(" ", strip=True)]
    upper = [c for c in cards(page) if ", BERKELEY," in c.get_text(" ", strip=True)]
    assert lower and upper, "fixture must carry both case oddities"

    kept = {listing.original_url for listing, in [(x,) for x in found(page)[0]]}

    assert lower[0].find("a", href=True)["href"] in kept
    assert upper[0].find("a", href=True)["href"] not in kept


# --------------------------------------------------------------------------
# identity, which is what makes one board the right number of boards
# --------------------------------------------------------------------------


def test_a_home_is_identified_by_its_slug_not_its_number() -> None:
    """Every San Francisco school board carries the same homes under ids of its
    own: Parkmerced is 2569628208 on UCSF and 2569628209 on USF. Filed by that
    number, one home is stored once per board."""
    listings, _ = found()

    for listing in listings:
        assert not listing.source_id.isdigit(), f"{listing.source_id} is a per-board number"
        assert listing.source_id in listing.original_url


def test_the_same_home_from_two_boards_is_one_home() -> None:
    ucsf = board_page()
    usfca = ucsf.replace("ucsf.uloop.com", "usfca.uloop.com")
    usfca = re.sub(r"/housing/view\.php/(\d+)/", lambda m: f"/housing/view.php/{int(m.group(1)) + 1}/", usfca)

    first, _ = found(ucsf)
    second, _ = found(usfca)

    assert {x.source_id for x in first} == {x.source_id for x in second}
    assert {x.original_url for x in first} != {x.original_url for x in second}


def test_only_one_board_is_read() -> None:
    """Reading all five would be five times the requests for one board's homes.

    Counted, not de-duplicated: two registrations of the same board are as
    wrong as two of different ones, and a set of subdomains would hide it."""
    class Mailbox:
        pass

    # Both branches. default_sources() with no mailbox is the one tests reach
    # and the one the product never takes, so checking only that would let a
    # second board ship in the branch that actually runs.
    for label, sources in (("no mailbox", default_sources()), ("with mailbox", default_sources(Mailbox()))):
        registered = [source for source in sources if source.platform == "Uloop"]
        assert len(registered) == 1, f"{label}: {len(registered)} boards are registered"
        assert registered[0].search_url.endswith("uloop.com/housing/index.php/available")
        # Not uloop.com/housing/san-francisco-ca/, which despite its name
        # returned twenty-three cards and not one of them in San Francisco.
        assert "san-francisco" not in registered[0].search_url


# --------------------------------------------------------------------------
# what a card says
# --------------------------------------------------------------------------


def test_a_rent_is_read_from_the_card_not_from_the_prose() -> None:
    """The figures are a fixed prefix and the rest is the lister writing. Read
    from the whole card, a description mentioning "utilities are about $50 a
    month" becomes the rent."""
    page = board_page().replace(
        "This remodeled Noe Valley Victorian",
        "Utilities are about $50 per month. This remodeled Noe Valley Victorian",
    )
    listings, _ = found(page)
    noe = next(x for x in listings if "Noe Valley" in x.title)

    assert noe.price == 9300


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("$1,350 · 3 Beds A single furnished bedroom", (1350, (3, 3))),
        ("$2,892 - $5,216 · 1-3 Beds Apartment living", (2892, (1, 3))),
        ("$2,247 - $7,280 · Studio, 1-5 Beds Now Leasing", (2247, (0, 5))),
        ("$3,950 · Studio Space Modern Glen Park studio", (3950, (0, 0))),
        ("Studio FOUND Study Southside Berkeley", (None, None)),
    ],
)
def test_every_shape_the_board_writes_an_offer_in(said, expected) -> None:
    """"Studio, 1-5 Beds" offers a studio too, which the digits alone do not
    say; and a studio-only card never reaches the word "Bed" to anchor on."""
    card = BeautifulSoup(f'<div><div class="description">{said}</div></div>', "html.parser").div
    assert UloopSource._offer(card) == expected


def test_a_posting_date_is_kept_because_this_one_is_real() -> None:
    """Almost nothing here publishes when a home was posted. This does, on
    every card, so it belongs in listing_timestamp where the shortlist reads
    it as "Posted" and sorts newest by it."""
    listings, _ = found()

    stamped = [x for x in listings if x.metadata.get("listing_timestamp")]
    assert len(stamped) == len(listings), "every card on this board carries a date"
    for listing in stamped:
        assert re.match(r"\d{4}-\d{2}-\d{2}T", str(listing.metadata["listing_timestamp"]))


def test_a_room_stays_a_room() -> None:
    """Half this board is rooms in shared flats. Asserting a housing_kind here
    would overrule the one reader that can tell them apart."""
    listings, _ = found()
    kinds = {classify_listing(x).housing_kind for x in listings}

    assert ROOM in kinds and WHOLE_UNIT in kinds


def test_a_building_that_lets_several_sizes_does_not_price_the_largest_cheaply() -> None:
    """One rent is quoted for a building letting 1 through 3 bedrooms, and it
    belongs to the one-bedroom."""
    listings, _ = found(prefs=deal("three_bedroom"))

    ranged = [x for x in listings if x.metadata["bedrooms_low"] != x.metadata["bedrooms_high"]]
    assert ranged, "the fixture must contain a building letting a range of sizes"
    for listing in ranged:
        assert listing.price is None
        assert listing.metadata["price_from"] > 0
        assert "is not published" in listing.summary
        # Said in words, never as a structured bedroom count: on this board a
        # bed count can mean rooms offered rather than the size of a home.
        assert "bedrooms" not in listing.metadata
        assert "3-bedroom" in listing.summary


def test_a_home_too_small_for_the_deal_is_dropped() -> None:
    everything, _ = found()
    three, _ = found(prefs=deal("three_bedroom"))

    assert len(three) < len(everything)
    for listing in three:
        assert listing.metadata["bedrooms_high"] >= 3


def test_a_neighbourhood_is_read_from_the_street() -> None:
    listings, _ = found()
    areas = {x.neighborhood for x in listings if x.neighborhood}
    assert {"Noe Valley", "Bernal Heights"} <= areas


# --------------------------------------------------------------------------
# how it fails
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse("", 202), "HTTP 202"),
        (FakeResponse("   ", 200), "empty page"),
        (FakeResponse("<html>press & hold</html>"), "bot check"),
        (FakeResponse("<html><body>Housing</body></html>"), "format may have changed"),
        (FakeResponse("<html>nope</html>", 429), "turned away"),
    ],
)
def test_a_page_that_is_not_a_board_says_so(response, expected, preferences) -> None:
    """This is markup, not a contract. The day a selector stops matching, the
    scan has to say so rather than report a city with no homes in it."""
    with pytest.raises(SourceError, match=expected):
        UloopSource().search(FakeClient(response), preferences)


def test_pages_are_read_until_they_stop_being_new(preferences) -> None:
    """San Francisco homes are scattered across the pages rather than clustered
    on the first, so paging is worth it -- and the board answers a page past
    the last one with content it has already served."""
    client = FakeClient(FakeResponse(board_page()), FakeResponse(board_page()))
    UloopSource().search(client, preferences)

    assert client.requested[0] == UloopSource.search_url
    assert client.requested[1].endswith("?page=2")
    assert len(client.requested) == 2, "a page adding nothing new ends the read"


def test_a_real_second_page_is_read(preferences) -> None:
    """Homes genuinely new to the read keep it going. Note what does *not*
    count as new: renumbering the ids while leaving the slugs alone is the same
    board again, which is exactly what a second school's board would be."""
    import re as _re

    renumbered = _re.sub(r"/housing/view\.php/(\d+)/", r"/housing/view.php/9\1/", board_page())
    renamed = _re.sub(r"/housing/view\.php/(\d+)/([^\"?#]+)", r"/housing/view.php/\1/z-\2", board_page())

    same = FakeClient(FakeResponse(board_page()), FakeResponse(renumbered), FakeResponse("<html></html>"))
    UloopSource().search(same, preferences)
    assert len(same.requested) == 2, "a board that only renumbered is not a new page"

    fresh = FakeClient(FakeResponse(board_page()), FakeResponse(renamed), FakeResponse("<html></html>"))
    listings = UloopSource().search(fresh, preferences)
    assert len(fresh.requested) == 3
    assert len(listings) == 2 * len(found()[0])


def test_no_detail_pages_are_read() -> None:
    """The card already carries the rent, the size, the date and a description."""
    assert UloopSource.detail_budget == 0
    assert not hasattr(UloopSource, "enrich")


def test_the_board_is_asked_in_the_monitors_own_name(preferences) -> None:
    """Unlike Redfin and Rent.com, this one answers the app honestly, so it
    must not borrow a browser's name for no reason."""
    _, client = found()
    assert all(headers is None for headers in client.headers_seen)


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_it_runs_without_setup_in_every_branch() -> None:
    class Mailbox:
        pass

    for sources in (default_sources(), default_sources(Mailbox())):
        source = next(x for x in sources if x.platform == "Uloop")
        assert source.mode == "automatic"
        assert source.manual_reason is None
        assert getattr(source, "connector_key", None) is None


def test_the_ready_check_still_probes_the_same_four() -> None:
    probed = [
        source.platform
        for source in default_sources()
        if getattr(source, "mode", "setup") == "automatic"
        and not getattr(source, "connector_key", None)
    ][:4]
    assert probed == ["Craigslist", "Listings Project", "Abacus (small buildings)", "SpareRoom"]


def test_the_scanner_runs_it_early_because_it_is_light() -> None:
    import inspect

    from sf_housing.scanner import Scanner

    body = inspect.getsource(Scanner)
    order = {
        name: int(value)
        for name, value in re.findall(r'"([^"]+)": (\d+),', body.split("priority = {")[1].split("}")[0])
    }
    assert order["Uloop"] < order["Redfin"] < order["Rent.com"]


def test_a_bed_count_is_never_asserted_as_a_home_size() -> None:
    """"$3,089 · 1 Bed" against "Furnished Master Bedroom" means one room in a
    shared flat. A structured bedroom count would overrule the one reader that
    weighs that, and every room on the board would file as a whole home."""
    listings, _ = found()

    assert listings
    for listing in listings:
        assert "bedrooms" not in listing.metadata
        assert listing.unit_type is None
        assert listing.housing_kind == "unknown"


def test_the_board_is_left_to_say_which_homes_are_rooms() -> None:
    listings, _ = found()
    kinds = {classify_listing(x).housing_kind for x in listings}

    assert kinds >= {ROOM, WHOLE_UNIT}, f"the board carries both, classification saw {kinds}"


def test_a_truncated_read_says_so(preferences, caplog) -> None:
    """The board is deeper than the page limit, and a truncated result that
    says nothing reads exactly like complete coverage."""
    import logging

    import re as _re

    # Distinct slugs, not distinct ids: the slug is the identity, so renumbering
    # would make every page the same page and the read would stop at two.
    pages = [
        FakeResponse(_re.sub(r"(/housing/view\.php/\d+/)", rf"\g<1>p{n}-", board_page()))
        for n in range(9)
    ]
    with caplog.at_level(logging.INFO, logger="sf_housing.sources"):
        UloopSource().search(FakeClient(*pages), preferences)

    assert any("page limit" in record.getMessage() for record in caplog.records)


def test_a_read_that_reached_the_end_says_nothing(preferences, caplog) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="sf_housing.sources"):
        UloopSource().search(FakeClient(FakeResponse(board_page()), FakeResponse("<html></html>")), preferences)

    assert not any("page limit" in record.getMessage() for record in caplog.records)
