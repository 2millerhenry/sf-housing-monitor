"""Normalize visible Furnished Finder browser cards for the local monitor.

The Chrome extension is intentionally dumb: it sends only public card text and
property links that a signed-out person can already see in their browser.  This
module is the trust boundary that validates that small payload before it enters
the shared listing database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from urllib.parse import urlsplit

from .classification import WHOLE_UNIT, classify_listing
from .models import ListingCandidate
from .preferences import Preferences
from .sources import (
    GmailHousingAlertSource,
    _clean_text,
    _parse_price,
    _source_id,
    sf_target_coordinate_neighborhood,
    visible_sf_area_hint,
)


PLATFORM = "Furnished Finder"
BRIDGE_VERSION = "1.4.0"
# Five result pages is enough depth to uncover more rooms without turning a
# scheduled browser check into a multi-minute crawl of every whole-home card.
MAX_CARDS_PER_IMPORT = 180
SEARCH_PREFIX = "/housing/"
PROPERTY_PREFIX = "/property/"


class FurnishedFinderBridgeError(ValueError):
    """The browser bridge supplied an invalid or unusable card payload."""


def _property_url(value: object) -> str | None:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {
        "furnishedfinder.com",
        "www.furnishedfinder.com",
    }:
        return None
    if not parsed.path.casefold().startswith(PROPERTY_PREFIX):
        return None
    return f"https://www.furnishedfinder.com{parsed.path.rstrip('/')}"


def _search_url(value: object) -> str | None:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {
        "furnishedfinder.com",
        "www.furnishedfinder.com",
    }:
        return None
    if not parsed.path.casefold().startswith(SEARCH_PREFIX):
        return None
    return f"https://www.furnishedfinder.com{parsed.path.rstrip('/')}" + (
        f"?{parsed.query}" if parsed.query else ""
    )


def validated_search_url(value: object) -> str:
    url = _search_url(value)
    if not url:
        raise FurnishedFinderBridgeError("The saved search must be a Furnished Finder housing-results URL.")
    return url


def _is_room_card(title: str, listing_type: str, summary: str) -> bool:
    combined = " ".join((title, listing_type, summary)).casefold()
    if "shared room" in combined or "shared bedroom" in combined:
        return False
    # Furnished Finder exposes the structured card type as e.g. "Room - House".
    # Require that fact instead of guessing from words such as "roomy" or a
    # whole-apartment title mentioning its number of bedrooms.
    return bool(re.match(r"^room\s*-\s*", listing_type.strip(), re.IGNORECASE))


def _display_type(listing_type: str) -> str:
    cleaned = _clean_text(listing_type, 100)
    if not cleaned:
        return "Private room"
    if "room" not in cleaned.casefold():
        return f"Private room · {cleaned}"
    if "-" in cleaned:
        home_type = _clean_text(cleaned.rsplit("-", 1)[-1], 70)
        if home_type:
            return f"Private room · {home_type}"
    return "Private room"


def cards_to_candidates(cards: object, preferences: Preferences) -> list[ListingCandidate]:
    """Turn at most 180 visible public cards into supported candidates.

    Missing location, lease, and household details stay unknown for scoring.  The
    bridge does *not* turn a generic furnished apartment into a studio or
    one-bedroom, two-bedroom, or three-bedroom without explicit evidence.
    """
    if not isinstance(cards, Sequence) or isinstance(cards, (str, bytes, bytearray)):
        raise FurnishedFinderBridgeError("The Chrome bridge did not send a list of visible cards.")
    # Browser pages can briefly retain cards from the previous result page
    # during a client-side pagination transition. Keep the import bounded, but
    # never discard an otherwise useful scan merely because of that transient.
    if len(cards) > MAX_CARDS_PER_IMPORT:
        cards = cards[:MAX_CARDS_PER_IMPORT]

    listings: list[ListingCandidate] = []
    seen_urls: set[str] = set()
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        original_url = _property_url(card.get("url"))
        title = _clean_text(str(card.get("title") or ""), 220)
        listing_type = _clean_text(str(card.get("listing_type") or ""), 100)
        summary = _clean_text(str(card.get("summary") or ""), 1_400)
        if not original_url or not title or original_url in seen_urls:
            continue
        seen_urls.add(original_url)
        is_room = _is_room_card(title, listing_type, summary)

        location_text = " ".join((title, listing_type, summary))
        latitude, longitude = card.get("latitude"), card.get("longitude")
        coordinate_area = sf_target_coordinate_neighborhood(latitude, longitude)
        neighborhood = (
            GmailHousingAlertSource._neighborhood(location_text, preferences)
            or coordinate_area
            or visible_sf_area_hint(location_text)
        )
        price = _parse_price(str(card.get("price") or summary), require_currency=True)
        candidate = classify_listing(
            ListingCandidate(
                platform=PLATFORM,
                source_id=_source_id(original_url),
                title=title,
                original_url=original_url,
                price=price,
                neighborhood=neighborhood,
                listing_type=_display_type(listing_type) if is_room else listing_type or "Furnished rental",
                summary=summary or title,
                metadata={
                    "bridge": "furnished_finder_chrome",
                    "furnished_finder_card_type": listing_type or None,
                    "furnished_finder_map_latitude": latitude if coordinate_area else None,
                    "furnished_finder_map_longitude": longitude if coordinate_area else None,
                    "furnished_finder_location_evidence": "Map pin" if coordinate_area else None,
                },
                housing_kind="room" if is_room else "unknown",
            )
        )
        if not is_room and candidate.housing_kind != WHOLE_UNIT:
            continue
        listings.append(candidate)
    return listings
