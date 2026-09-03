from __future__ import annotations

from sf_housing.apify import ApifyTokenStore
from sf_housing.sources import FurnishedFinderSource


VALID_TOKEN = "apify_api_abcdefghijklmnopqrstuvwxyz123456"


class FurnishedFinderResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return [
            {
                "id": "1006574_1",
                "url": "https://www.furnishedfinder.com/property/1006574_1?ref=search",
                "title": "Sunny furnished room near Dolores Park",
                "propertyType": "Room - House",
                "monthlyPrice": 1650,
                "latitude": 37.77032,
                "longitude": -122.41304,
                "availableOnDate": "2026-08-01",
                "minimumStayDays": 90,
                "bathroomType": "Private Bath",
                "amenities": ["Washer/Dryer", "Garden", "WiFi"],
                "description": "Bright room in a quiet shared Victorian near Dolores Park.",
                "contactName": "A host name we deliberately ignore",
                "contactEmail": "not-stored@example.com",
            },
            {
                "id": "bad-url",
                "url": "https://www.furnishedfinder.com/housing/us--ca--san-francisco",
                "title": "This search page must not become a listing",
            },
        ]


class FurnishedFinderClient:
    def __init__(self):
        self.call = None

    def post(self, url, **kwargs):
        self.call = (url, kwargs)
        return FurnishedFinderResponse()


def test_furnished_finder_private_rooms_are_normalized_without_host_data(tmp_path, preferences) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)
    source = FurnishedFinderSource(tokens)
    client = FurnishedFinderClient()

    listings = source.search(client, preferences)

    assert len(listings) == 1
    listing = listings[0]
    assert listing.platform == "Furnished Finder"
    assert listing.source_id == "1006574_1"
    assert listing.original_url == "https://www.furnishedfinder.com/property/1006574_1"
    assert listing.price == 1650
    assert listing.neighborhood == "Mission Dolores"
    assert listing.listing_type == "Private room · Room - House"
    assert "Minimum stay: 90 days" in listing.summary
    assert "Garden" in listing.summary
    assert "contactName" not in listing.metadata
    assert "contactEmail" not in listing.metadata
    assert listing.metadata["property_type"] == "Room - House"

    url, request = client.call
    assert VALID_TOKEN not in url
    assert request["headers"] == {"Authorization": f"Bearer {VALID_TOKEN}"}
    assert request["timeout"] == 50.0
    assert request["json"] == {
        "city": "San Francisco",
        "state": "CA",
        "maxItems": 5,
        "minPrice": 1000,
        "maxPrice": 2000,
        "propertyType": ["room"],
        "moreDetails": False,
        "includeReviews": False,
    }


def test_furnished_finder_budget_reservation_fails_closed_at_monthly_cap(tmp_path) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")

    assert tokens.reserve_monthly_furnished_finder_listings(5, limit=10) is True
    assert tokens.reserve_monthly_furnished_finder_listings(5, limit=10) is True
    assert tokens.reserve_monthly_furnished_finder_listings(1, limit=10) is False


def test_furnished_finder_stays_out_of_automatic_scans_until_a_fast_provider_is_verified(tmp_path) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)

    assert FurnishedFinderSource(tokens).mode == "setup"
    assert FurnishedFinderSource(tokens, enabled=True).mode == "setup"
