"""The two zero-setup sources added so a new install sees more than Craigslist.

Both fixtures are real captured payloads, trimmed: the SF portal one keeps four
rentals plus one ownership record, and the Apartment List one keeps the page's
real schema.org block.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sf_housing.classification import ROOM, UNKNOWN, WHOLE_UNIT
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import ApartmentListSource, SFHousingPortalSource, SourceError
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, payload: object = None, text: str = "", status: int = 200):
        self._payload = payload
        self.text = text
        self.status = status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError(f"unexpected status {self.status}")


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.response


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


def portal_payload() -> dict:
    return json.loads((FIXTURES / "sf_portal_listings.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# SF Housing Portal (DAHLIA)
# --------------------------------------------------------------------------


def test_portal_returns_rentals_with_real_rents(preferences: Preferences) -> None:
    source = SFHousingPortalSource()
    client = FakeClient(FakeResponse(payload=portal_payload()))

    listings = source.search(client, preferences)

    assert listings, "the fixture contains rentals, so the parser must return some"
    assert client.requested == [SFHousingPortalSource.api_url]
    for listing in listings:
        assert listing.platform == "SF Housing Portal"
        assert listing.price and listing.price > 0
        assert listing.original_url.startswith("https://housing.sfgov.org/listings/")
        assert listing.metadata["below_market_rate"] is True


def test_portal_skips_ownership_records(preferences: Preferences) -> None:
    """Resale and new-sale rows are homes to buy, not rent."""
    payload = portal_payload()
    tenures = {str(r.get("Tenure")) for r in payload["listings"]}
    assert any("ale" in t for t in tenures), "fixture must contain an ownership row to prove the filter"

    listings = SFHousingPortalSource().search(FakeClient(FakeResponse(payload=payload)), preferences)

    sale_ids = {r["Id"] for r in payload["listings"] if "rental" not in str(r.get("Tenure", "")).casefold()}
    for listing in listings:
        assert listing.source_id.split(":")[0] not in sale_ids


def test_portal_skips_listings_outside_san_francisco(preferences: Preferences) -> None:
    payload = portal_payload()
    for record in payload["listings"]:
        record["Building_City"] = "Daly City"

    assert SFHousingPortalSource().search(FakeClient(FakeResponse(payload=payload)), preferences) == []


def test_portal_carries_a_timestamp_so_the_first_scan_can_bound_it(preferences: Preferences) -> None:
    """This is one of the few sources that publishes a usable date."""
    from datetime import datetime

    listings = SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=portal_payload())), preferences
    )

    stamped = [l for l in listings if l.metadata.get("listing_timestamp")]
    assert stamped, "the portal publishes LastModifiedDate; it must survive into metadata"
    for listing in stamped:
        # Must be parseable by Scanner._within_initial_window.
        datetime.fromisoformat(str(listing.metadata["listing_timestamp"]))


def test_portal_maps_its_own_unit_vocabulary(preferences: Preferences) -> None:
    listings = SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=portal_payload())), preferences
    )
    by_type = {str(l.metadata.get("sf_portal_unit_type")).casefold(): l for l in listings}

    if "studio" in by_type:
        assert by_type["studio"].metadata["unit_type"] == "studio"
        assert by_type["studio"].housing_kind == WHOLE_UNIT
    if "1 br" in by_type:
        assert by_type["1 br"].metadata["unit_type"] == "1 bedroom"
    if "sro" in by_type:
        # A single-room-occupancy is a room, not a home to split.
        assert by_type["sro"].housing_kind == ROOM
        assert "unit_type" not in by_type["sro"].metadata


def test_portal_keeps_the_cheapest_rent_per_unit_type() -> None:
    """A building can publish one unit type in both buckets; keep the better rent."""
    payload = {
        "listings": [
            {
                "Id": "a1",
                "Name": "Test Commons",
                "Tenure": "Re-rental",
                "Building_City": "San Francisco",
                "Building_Street_Address": "1 Test St",
                "unitSummaries": {
                    "general": [{"unitType": "1 BR", "minMonthlyRent": 2400.0, "totalUnits": 2}],
                    "reserved": [{"unitType": "1 BR", "minMonthlyRent": 1500.0, "totalUnits": 1}],
                },
            }
        ]
    }
    listings = SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=payload)), parse_preferences(TEST_PREFERENCES)
    )

    assert len(listings) == 1, "one unit type must not become two rows"
    assert listings[0].price == 1500


def test_portal_ignores_units_without_a_rent() -> None:
    payload = {
        "listings": [
            {
                "Id": "a1",
                "Name": "No Rent Published",
                "Tenure": "New rental",
                "Building_City": "San Francisco",
                "unitSummaries": {"general": [{"unitType": "2 BR", "minMonthlyRent": None}]},
            }
        ]
    }

    assert SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=payload)), parse_preferences(TEST_PREFERENCES)
    ) == []


def test_portal_reports_a_changed_api_instead_of_returning_nothing(preferences: Preferences) -> None:
    """Silence would look identical to 'no homes available'."""
    with pytest.raises(SourceError, match="listings array"):
        SFHousingPortalSource().search(FakeClient(FakeResponse(payload={"data": []})), preferences)

    with pytest.raises(SourceError, match="non-JSON"):
        SFHousingPortalSource().search(FakeClient(FakeResponse(payload=None)), preferences)


def test_portal_accepts_an_empty_but_valid_response(preferences: Preferences) -> None:
    """No open lotteries is a real answer, not a failure."""
    assert SFHousingPortalSource().search(FakeClient(FakeResponse(payload={"listings": []})), preferences) == []


# --------------------------------------------------------------------------
# Apartment List
# --------------------------------------------------------------------------


def apartment_list_document() -> str:
    return (FIXTURES / "apartment_list_search.html").read_text(encoding="utf-8")


def test_apartment_list_reads_buildings_and_starting_rent(preferences: Preferences) -> None:
    source = ApartmentListSource()
    client = FakeClient(FakeResponse(text=apartment_list_document()))

    listings = source.search(client, preferences)

    assert listings
    for listing in listings:
        assert listing.platform == "Apartment List"
        assert listing.original_url.startswith("https://www.apartmentlist.com/")
        assert listing.metadata["building_listing"] is True


def test_apartment_list_never_guesses_the_home_size(preferences: Preferences) -> None:
    """The feed publishes no unit mix, so the workflow must stay unknown."""
    listings = ApartmentListSource().search(
        FakeClient(FakeResponse(text=apartment_list_document())), preferences
    )

    assert listings
    for listing in listings:
        assert listing.housing_kind == UNKNOWN
        assert "unit_type" not in listing.metadata
        assert "unconfirmed" in listing.summary


def test_apartment_list_reports_a_format_change(preferences: Preferences) -> None:
    with pytest.raises(SourceError, match="structured building data"):
        ApartmentListSource().search(
            FakeClient(FakeResponse(text="<html><body>nothing here</body></html>")), preferences
        )


def test_apartment_list_survives_malformed_structured_data(preferences: Preferences) -> None:
    document = (
        '<html><head>'
        '<script type="application/ld+json">{not json at all}</script>'
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Real Building","url":"https://www.apartmentlist.com/ca/x",'
        '"offers":{"@type":"AggregateOffer","lowPrice":2100,"highPrice":3400}}'
        "</script></head></html>"
    )

    listings = ApartmentListSource().search(FakeClient(FakeResponse(text=document)), parse_preferences(TEST_PREFERENCES))

    assert len(listings) == 1
    assert listings[0].price == 2100
    assert listings[0].metadata["price_high"] == 3400
    assert "$2,100 to $3,400" in listings[0].summary


def test_apartment_list_skips_entries_without_a_usable_url() -> None:
    document = (
        '<html><head><script type="application/ld+json">'
        '[{"@type":"Product","name":"No URL"},'
        '{"@type":"Product","name":"Insecure","url":"http://example.test/x"},'
        '{"@type":"Product","name":"Good","url":"https://www.apartmentlist.com/ca/good"}]'
        "</script></head></html>"
    )

    listings = ApartmentListSource().search(
        FakeClient(FakeResponse(text=document)), parse_preferences(TEST_PREFERENCES)
    )

    assert [l.title for l in listings] == ["Good"]


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_both_sources_are_automatic_and_in_the_default_set() -> None:
    from sf_housing.sources import default_sources

    platforms = {s.platform: s for s in default_sources()}

    for name in ("SF Housing Portal", "Apartment List"):
        assert name in platforms, f"{name} must work on a fresh install with no setup"
        assert platforms[name].mode == "automatic"
        assert platforms[name].manual_reason is None


def test_two_unit_types_in_one_building_survive_storage(tmp_path: Path) -> None:
    """Regression: the portal serves one page per building, not per unit.

    With a shared URL the repository's canonical-URL dedup collapsed every unit
    type in a building into a single row, so a cheap studio vanished behind the
    two-bedroom that happened to be written last.
    """
    from sf_housing.database import Repository, canonicalize_url
    from sf_housing.models import ScoreResult

    payload = {
        "listings": [
            {
                "Id": "b7",
                "Name": "Garden Court",
                "Tenure": "Re-rental",
                "Building_City": "San Francisco",
                "Building_Street_Address": "1637 15th St",
                "unitSummaries": {
                    "general": [
                        {"unitType": "Studio", "minMonthlyRent": 1626.0, "totalUnits": 3},
                        {"unitType": "1 BR", "minMonthlyRent": 1847.0, "totalUnits": 3},
                    ]
                },
            }
        ]
    }

    listings = SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=payload)), parse_preferences(TEST_PREFERENCES)
    )
    assert len(listings) == 2

    canonical = {canonicalize_url(l.original_url) for l in listings}
    assert len(canonical) == 2, "each unit type needs a distinct canonical URL"

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    for listing in listings:
        repository.upsert_listing(listing, ScoreResult(70, ["fits"], "", {}))

    import sqlite3

    stored = sqlite3.connect(tmp_path / "housing.sqlite3").execute(
        "SELECT price FROM listings ORDER BY price"
    ).fetchall()
    assert [row[0] for row in stored] == [1626, 1847], "both homes must survive the write"
