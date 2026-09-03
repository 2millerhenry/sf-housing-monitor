from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from html import unescape
from typing import Protocol
from urllib.parse import unquote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .apify import ApifyTokenStore
from .classification import ROOM, UNKNOWN, WHOLE_UNIT
from .deal_profile import SF_NEIGHBORHOODS
from .connectors import gmail_provider_key
from .gmail_alerts import AlertEmail, GmailAlertMailbox
from .location import declared_outside_sf_area_hint
from .models import ListingCandidate
from .preferences import Preferences


class SourceError(RuntimeError):
    pass


# These are source-integrity signals, not rent preferences.  A public search
# card can be live while its detail page has been removed or is part of a
# repeated malformed-post campaign, so the detail page is the authority before
# a Craigslist whole unit reaches the shortlist.
_CRAIGSLIST_REMOVED_PATTERN = re.compile(
    r"posting has (?:been )?(?:flagged for removal|expired|deleted|removed)",
    re.IGNORECASE,
)
_CRAIGSLIST_BEDROOM_PATTERN = re.compile(
    r"\b(?:(?P<studio>studio)|(?P<number>[0-3])\s*(?:bd|br|bed(?:room)?s?)|"
    r"(?P<word>one|two|three)[- ]bed(?:room)?s?)\b",
    re.IGNORECASE,
)
_CRAIGSLIST_SUSPECT_PROFILE_PATTERN = re.compile(
    r"\b(?:mark\s+robart|license\s+(?:jane|jessica))\s+properties\b",
    re.IGNORECASE,
)
_CRAIGSLIST_OFF_MARKET_LOCATION_PATTERN = re.compile(
    r"\b(?:chicago|rogers\s+park|metra|morse\s+station|henry\s+cowell|"
    r"roaring\s+camp)\b",
    re.IGNORECASE,
)


def _craigslist_bedroom_count(text: str | None) -> int | None:
    """Read one explicit advertised bedroom count without guessing from prose."""
    if not text:
        return None
    match = _CRAIGSLIST_BEDROOM_PATTERN.search(text)
    if not match:
        return None
    if match.group("studio"):
        return 0
    if match.group("number") is not None:
        return int(match.group("number"))
    return {"one": 1, "two": 2, "three": 3}.get(str(match.group("word")).casefold())


def _clean_text(value: object, limit: int | None = None) -> str:
    # Structured data legitimately publishes booleans and numbers where earlier
    # pages published strings -- Apartment List sends petsAllowed as "Yes" on
    # some buildings and as true on others. A source must not lose its whole
    # detail fetch to that. Falsy values keep their long-standing empty result.
    if not isinstance(value, str):
        value = "" if not value else str(value)
    text = re.sub(r"\s+", " ", value).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _parse_price(value: str | None, *, require_currency: bool = False) -> int | None:
    if not value:
        return None
    # Card text can begin with a street number. Prefer an explicitly dollar-marked
    # amount before falling back to a source's already-price-only field.
    match = re.search(r"\$\s*([0-9][0-9,]*)", value)
    if not match and not require_currency:
        match = re.search(r"\$?\s*([0-9][0-9,]*)", value)
    return int(match.group(1).replace(",", "")) if match else None


def _source_id(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    # HotPads listing URLs end in the shared literal `/pad`; use the full
    # canonical URL in that one case so distinct listings never collide.
    if slug == "pad":
        return hashlib.sha256(url.encode()).hexdigest()[:20]
    return slug or hashlib.sha256(url.encode()).hexdigest()[:20]


# Facebook's Property Rentals cards often contain only a latitude/longitude in
# `locationText`.  These are deliberately small, supplied-search bounding boxes
# rather than a broad attempt to reverse-geocode all of San Francisco.  A point
# outside them remains unknown (or outside), which is safer than inventing an
# attractive neighborhood from a city-wide pin.
_FACEBOOK_NEIGHBORHOOD_BOXES: tuple[tuple[str, float, float, float, float], ...] = (
    ("Duboce Triangle", 37.761558, 37.772550, -122.438848, -122.424128),
    ("Mission Dolores", 37.752995, 37.774980, -122.440882, -122.411442),
    # A verified public Potrero result used this pin:
    # 37.75780, -122.40093. This conservative box covers Potrero Hill without
    # swallowing the Mission Dolores search area above.
    ("Potrero Hill", 37.747500, 37.770500, -122.419000, -122.384000),
    ("Bernal Heights", 37.729124, 37.751115, -122.429802, -122.400363),
    ("Noe Valley", 37.736680, 37.758669, -122.447998, -122.418559),
    ("Marina", 37.797000, 37.810000, -122.455000, -122.421000),
)

# These labels are only accepted when the seller actually writes the place name
# in the card.  They make broad city-level cards more useful without pretending
# that a nearby landmark reveals an exact address.
_VISIBLE_SF_AREA_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Mission Dolores", ("mission dolores", "dolores park")),
    ("Duboce Triangle", ("duboce triangle",)),
    ("Potrero Hill", ("potrero hill",)),
    ("Noe Valley", ("noe valley",)),
    ("Bernal Heights", ("bernal heights",)),
    ("Dolores Heights", ("dolores heights",)),
    ("Mission District", ("mission district", "the mission")),
    ("Haight-Ashbury", ("haight ashbury", "haight-ashbury")),
    ("Hayes Valley", ("hayes valley",)),
    ("Lower Haight", ("lower haight",)),
    ("NOPA", ("north of panhandle", "nopa")),
    ("Panhandle", ("the panhandle", "panhandle home", "panhandle house")),
    ("Eureka Valley", ("eureka valley",)),
    ("Castro", ("the castro", "castro with", "castro room", "castro district")),
    ("Cole Valley", ("cole valley",)),
    ("Inner Richmond", ("inner richmond",)),
    ("Dogpatch", ("dogpatch",)),
    ("Mission Bay", ("mission bay",)),
    ("SoMa", ("south of market", "soma", "fidi")),
    ("Financial District", ("financial district",)),
    ("North Beach", ("north beach",)),
    ("Marina", ("marina district", "marina room", "in the marina")),
    ("Diamond Heights", ("diamond heights",)),
    ("Sunnyside", ("sunnyside",)),
    ("Outer Sunset", ("outer sunset",)),
    ("Parkside", ("parkside",)),
    ("Bayview", ("bayview",)),
    ("Excelsior", ("excelsior",)),
    ("Portola", ("portola district",)),
    ("Menlo Park (outside SF)", ("menlo park",)),
    ("Near Golden Gate Park", ("golden gate park",)),
    ("Near Ocean Beach", ("ocean beach",)),
    ("Near Lands End", ("lands end",)),
)


def visible_sf_area_hint(text: str | None) -> str | None:
    """Return a location phrase explicitly visible in a public card's text.

    This is deliberately not a geocoder: an unlabelled city-wide map pin stays
    unknown, because assigning a desirable neighborhood from it would be a
    guess.  Landmark labels are clearly marked ``Near …`` for the same reason.
    """
    normalized = _normal_text(text or "")
    for area, phrases in _VISIBLE_SF_AREA_HINTS:
        for phrase in phrases:
            pattern = rf"(?<!\w){re.escape(_normal_text(phrase))}(?!\w)"
            if re.search(pattern, normalized):
                return area
    return None


def declared_sf_area_hint(text: str | None) -> str | None:
    """Return an area only when prose explicitly describes the home's location."""
    normalized = _normal_text(text or "")
    location_prefix = r"(?:located|situated|available(?:\s+for\s+rent)?|for\s+rent)\s+(?:in|at)\s+"
    for area, phrases in _VISIBLE_SF_AREA_HINTS:
        for phrase in phrases:
            value = re.escape(_normal_text(phrase))
            if re.search(
                rf"{location_prefix}(?:san\s+francisco\s+)?(?:the\s+)?[^.!?]{{0,32}}(?<!\w){value}(?!\w)",
                normalized,
            ):
                return area
            if re.search(rf"(?<!\w){value}(?!\w)(?:\s*/\s*[a-z ]+)?\s+(?:district|neighbou?rhood)\b", normalized):
                return area
    return None


def sf_area_from_slug(url: str | None) -> str | None:
    """Recover a neighborhood that a listing URL names in its path.

    Zumper builds its slugs as building-name, neighborhood, city, state, so the
    area is right there even though the structured data only ever says "San
    Francisco". Matching against the canonical list rather than guessing which
    trailing words are the neighborhood keeps one-word areas (SoMa) and
    multi-word ones (Potrero Hill) equally correct, and the longest match wins
    so "Mission Bay" is never read as "Mission".
    """
    path = _normal_text(str(url or "").replace("-", " ").replace("/", " "))
    if not path:
        return None
    best: str | None = None
    for area in SF_NEIGHBORHOODS:
        needle = _normal_text(area)
        if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", path):
            if best is None or len(needle) > len(_normal_text(best)):
                best = area
    return best


def sf_target_coordinate_neighborhood(latitude: object, longitude: object) -> str | None:
    """Return a target area only when a source map pin is unambiguous."""
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    for name, south, north, west, east in _FACEBOOK_NEIGHBORHOOD_BOXES:
        if south <= latitude <= north and west <= longitude <= east:
            return name
    return None


def facebook_coordinate_neighborhood(location: str | None) -> str | None:
    """Return a target area when Facebook's coordinate-only pin is unambiguous."""
    match = re.search(r"\b(37\.\d{4,})\s+(-122\.\d{4,})\b", location or "")
    if not match:
        return None
    return sf_target_coordinate_neighborhood(match.group(1), match.group(2))


class ListingSource(Protocol):
    platform: str
    mode: str
    search_url: str
    manual_reason: str | None
    detail_budget: int

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]: ...

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate: ...


class GmailHousingAlertSource:
    """Turn first-party saved-search email cards into ordinary listing candidates."""

    mode = "setup"
    provider = "gmail"
    connector_key = "gmail"
    detail_budget = 0
    opaque_alert_message: str | None = None

    def __init__(
        self,
        platform: str,
        search_url: str,
        manual_reason: str,
        query: str,
        hostname: str,
        url_path_pattern: str,
        mailbox: GmailAlertMailbox,
        canonical_hostname: str | None = None,
    ):
        self.platform = platform
        self.search_url = search_url
        self._manual_reason = manual_reason
        self.query = query
        self.hostname = hostname
        self.url_path_pattern = url_path_pattern
        self.mailbox = mailbox
        self.canonical_hostname = canonical_hostname or f"www.{hostname}"
        self.empty_result_message: str | None = None
        self.last_alert_count = 0
        self.connector_state_key = gmail_provider_key(platform)

    @property
    def mode(self) -> str:
        return "automatic" if self.mailbox.is_connected else "setup"

    @property
    def manual_reason(self) -> str:
        if self.mailbox.is_connected:
            return ""
        return self._manual_reason

    def _direct_url(self, href: str) -> str | None:
        decoded = unescape(href)
        candidates = [decoded]
        for _ in range(3):
            decoded = unquote(decoded)
            candidates.append(decoded)
        if self.hostname == "facebook.com":
            pattern = re.compile(
                r"https?://(?:www\.)?facebook\.com/marketplace/item/(\d+)", re.IGNORECASE
            )
            for candidate in candidates:
                match = pattern.search(candidate)
                if match:
                    return f"https://www.facebook.com/marketplace/item/{match.group(1)}/"
            return None
        pattern = re.compile(
            rf"https?://(?:www\.)?{re.escape(self.hostname)}"
            rf"(?P<path>{self.url_path_pattern}[^\s\"'<>?&]*)",
            re.IGNORECASE,
        )
        for candidate in candidates:
            match = pattern.search(candidate)
            if match:
                path = match.group("path").rstrip(".,)")
                return f"https://{self.canonical_hostname}{path}"
        return None

    def _listing_source_id(self, original_url: str, title: str, card_text: str) -> str:
        """Provide a stable identity even when an alert provider wraps a card link."""
        return _source_id(original_url)

    def _is_listing_card(
        self, original_url: str, title: str, card_text: str, subject: str, anchor: object | None = None
    ) -> bool:
        """Reject navigation and preference links mixed into alert email cards."""
        return True

    def _listing_neighborhood(
        self, card_text: str, subject: str, original_url: str, title: str, preferences: Preferences
    ) -> str | None:
        return self._neighborhood(card_text, preferences)

    @staticmethod
    def _card_text(anchor) -> str:
        node = anchor
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            value = _clean_text(node.get_text(" ", strip=True), 520)
            if 35 <= len(value) <= 520:
                return value
        return _clean_text(anchor.get_text(" ", strip=True), 520)

    @staticmethod
    def _neighborhood(text: str, preferences: Preferences) -> str | None:
        normalized = _normal_text(text)
        aliases = preferences.mapping_value("neighborhood_aliases")
        for name in (
            preferences.list_value("ideal_neighborhoods")
            + preferences.list_value("preferred_neighborhoods")
            + preferences.list_value("acceptable_neighborhoods")
        ):
            if any(_normal_text(phrase) in normalized for phrase in [name, *aliases.get(name, [])]):
                return name
        return None

    def _from_email(self, email: AlertEmail, preferences: Preferences) -> list[ListingCandidate]:
        soup = BeautifulSoup(email.html, "html.parser")
        anchors = soup.select("a[href]")
        if not anchors:
            # Zillow and Facebook generally use HTML cards. A text-only email can
            # still be useful when it contains a direct listing URL.
            anchors = []
            for url in re.findall(r"https?://[^\s<>]+", f"{email.text} {email.html}"):
                parsed = self._direct_url(url)
                if parsed:
                    anchors.append((parsed, email.subject, email.text or email.subject))

        listings: list[ListingCandidate] = []
        seen_listing_ids: set[str] = set()
        for anchor in anchors:
            if isinstance(anchor, tuple):
                original_url, title, card_text = anchor
            else:
                original_url = self._direct_url(str(anchor.get("href", "")))
                if not original_url:
                    continue
                title = _clean_text(anchor.get_text(" ", strip=True)) or email.subject
                card_text = self._card_text(anchor) or email.subject
            source_id = self._listing_source_id(original_url, title, card_text)
            if not self._is_listing_card(original_url, title, card_text, email.subject, anchor):
                continue
            if source_id in seen_listing_ids:
                continue
            seen_listing_ids.add(source_id)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=source_id,
                    title=_clean_text(title, 180) or f"{self.platform} alert",
                    original_url=original_url,
                    price=_parse_price(card_text, require_currency=True),
                    neighborhood=self._listing_neighborhood(
                        card_text, email.subject, original_url, title, preferences
                    ),
                    listing_type=None,
                    summary=_clean_text(card_text, 420),
                    metadata={"alert_message_id": email.message_id, "alert_subject": email.subject},
                )
            )
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return self._search_query(preferences, self.query)

    def search_for_trigger(
        self,
        client: httpx.Client,
        preferences: Preferences,
        trigger: str,
    ) -> list[ListingCandidate]:
        query = self.query
        if trigger == "initial_discovery":
            query = re.sub(r"\bnewer_than:\d+d\b", "newer_than:5d", query)
        return self._search_query(preferences, query)

    def _search_query(self, preferences: Preferences, query: str) -> list[ListingCandidate]:
        listings: list[ListingCandidate] = []
        seen_listing_ids: set[str] = set()
        self.empty_result_message = None
        self.last_alert_count = 0
        emails = self.mailbox.messages(query, max_results=50)
        self.last_alert_count = len(emails)
        for email in emails:
            for listing in self._from_email(email, preferences):
                if listing.source_id not in seen_listing_ids:
                    listings.append(listing)
                    seen_listing_ids.add(listing.source_id)
        if emails and not listings:
            if self.opaque_alert_message:
                self.empty_result_message = self.opaque_alert_message
                return []
            raise SourceError(
                f"{self.platform} alert emails were found, but none contained a direct listing link. "
                "The email format or notification settings may need attention."
            )
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        # The saved-search email is the source of data. We deliberately do not
        # fetch Zillow/Facebook pages here; doing so would reintroduce bot blocks.
        return listing


def _normal_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


class ZillowAlertSource(GmailHousingAlertSource):
    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Zillow",
            search_url="https://www.zillow.com/myzillow/savedsearches/",
            manual_reason="Set up Zillow Instant saved-search email alerts, then connect Gmail from Alerts.",
            # Zillow's current rental alerts use listing-address subjects such
            # as "606 Capp St #105 just listed", not the older generic
            # "saved search" / "new listings" wording. The card-link parser
            # below remains the admission gate, so ordinary Zillow mail stays
            # out even with this wider mailbox query.
            query="from:(zillow.com) newer_than:90d",
            hostname="zillow.com",
            url_path_pattern=r"/(?:homedetails|b)/",
            mailbox=mailbox,
        )

    def _direct_url(self, href: str) -> str | None:
        direct = super()._direct_url(href)
        if direct:
            return direct

        # USC's Proofpoint mail gateway wraps Zillow's click-tracking card
        # links. Keep that protected URL intact: opening it in the user's
        # browser follows the normal security check and Zillow redirect. We do
        # not resolve it during a scan, which avoids generating tracking opens
        # or crawling protected Zillow pages.
        decoded = unescape(href)
        if (
            "urldefense.com/" in decoded.casefold()
            and "click.mail.zillow.com/" in decoded.casefold()
        ):
            return decoded
        return None

    def _listing_source_id(self, original_url: str, title: str, card_text: str) -> str:
        if "click.mail.zillow.com/" in original_url.casefold():
            # The protection wrapper is unique to each email delivery. Build a
            # repeatable ID from the card's own visible address copy, ignoring price
            # so an advertised rent correction updates the same home.
            identity = re.sub(r"\$\s*[0-9][0-9,]*(?:\s*/\s*(?:mo|month))?", "", title)
            return "zillow-email-" + hashlib.sha256(_normal_text(identity).encode()).hexdigest()[:20]
        return super()._listing_source_id(original_url, title, card_text)

    @staticmethod
    def _subject_address(subject: str) -> str | None:
        match = re.match(r"\s*(?P<address>.+?)\s+just\s+listed\b", subject, re.IGNORECASE)
        if not match:
            match = re.match(r"\s*new\s+for\s+rent:\s*(?P<address>.+?)\s*(?:,\s*san\s+francisco.*)?$", subject, re.IGNORECASE)
        return _normal_text(match.group("address")) if match else None

    def _is_listing_card(
        self, original_url: str, title: str, card_text: str, subject: str, anchor: object | None = None
    ) -> bool:
        if "click.mail.zillow.com/" not in original_url.casefold():
            return True
        # A parent node can contain an entire alert email, so judge protected
        # card links from their own visible label. Zillow places the price and
        # bedroom/room detail on the address link, while status and footer
        # links have neither.
        text = title
        has_listing_facts = bool(
            _parse_price(text, require_currency=True) is not None
            and re.search(r"\b(?:[0-9]+\s*(?:bd|br)|studio|bed(?:room)?|private\s+room|room\s+for\s+rent)\b", text, re.IGNORECASE)
        )
        subject_address = self._subject_address(subject)
        matches_subject_address = bool(subject_address and subject_address in _normal_text(title))
        previous_anchor = anchor.find_previous("a") if hasattr(anchor, "find_previous") else None
        previous_label = (
            _normal_text(previous_anchor.get_text(" ", strip=True))
            if previous_anchor is not None and hasattr(previous_anchor, "get_text")
            else ""
        )
        # Zillow places a small "For rent New" link immediately before each
        # actual saved-search card. Similar cards below it are recommendations,
        # so never treat them as matching results simply because they share the
        # same protected link format.
        marked_new = "for rent" in previous_label and "new" in previous_label
        return has_listing_facts and (matches_subject_address or marked_new)

    def _listing_neighborhood(
        self, card_text: str, subject: str, original_url: str, title: str, preferences: Preferences
    ) -> str | None:
        neighborhood = super()._listing_neighborhood(
            card_text, subject, original_url, title, preferences
        )
        if neighborhood or "click.mail.zillow.com/" not in original_url.casefold():
            return neighborhood
        return self._neighborhood(subject, preferences)


class HotPadsAlertSource(GmailHousingAlertSource):
    """Import first-party HotPads saved-search alert cards through Gmail."""

    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="HotPads",
            search_url="https://hotpads.com/san-francisco-ca/apartments-for-rent",
            manual_reason="Save a HotPads search with email alerts, then connect Gmail from Alerts.",
            # HotPads permits the alert cadence to be selected by the account
            # owner. Filtering by sender and then requiring a direct /pad link
            # keeps normal product mail out of the monitor.
            query="from:(hotpads.com) newer_than:90d",
            hostname="hotpads.com",
            url_path_pattern=r"/[^/\s\"'<>?&]+/pad",
            mailbox=mailbox,
            canonical_hostname="hotpads.com",
        )


class RoomiesAlertSource(GmailHousingAlertSource):
    """Import Roomies listing alerts without scraping its protected search page."""

    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Roomies",
            search_url="https://www.roomies.com/rooms/san-francisco-ca",
            manual_reason="Set Roomies Listing Alerts to Realtime or Daily, then connect Gmail from Alerts.",
            # Individual public Roomies listings use /rooms/<numeric-id>.
            # This intentionally rejects location and filter pages such as
            # /rooms/san-francisco-ca.
            query="from:(roomies.com) newer_than:90d",
            hostname="roomies.com",
            url_path_pattern=r"/rooms/[0-9]+",
            mailbox=mailbox,
        )


class ZumperAlertSource(GmailHousingAlertSource):
    """Import Zumper's own saved-search emails without scraping its protected site."""

    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Zumper",
            search_url="https://www.zumper.com/apartments-for-rent/san-francisco-ca",
            manual_reason="Save Zumper searches with email alerts, then connect Gmail from Alerts.",
            # Zumper's individual listings use /address/ or /apartment-buildings/.
            # Excluding broad /apartments-for-rent/ routes keeps alert-management
            # and search-result links out of the shortlist.
            query="from:(zumper.com) newer_than:90d",
            hostname="zumper.com",
            url_path_pattern=r"/(?:address|apartment-buildings)/",
            mailbox=mailbox,
        )


class ApartmentsComAlertSource(GmailHousingAlertSource):
    """Import Apartments.com saved-search links with their stable listing ID."""

    opaque_alert_message = (
        "No direct listing links were found. Apartments.com may be sending opaque tracking links; "
        "forward its original alert email to this Gmail inbox unchanged."
    )

    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Apartments.com",
            search_url="https://www.apartments.com/san-francisco-ca/",
            manual_reason="Save Apartments.com searches with email alerts, then connect Gmail from Alerts.",
            # Individual Apartments.com homes use a descriptive path followed by
            # a stable short listing ID, unlike city and filter result pages.
            query="from:(apartments.com) newer_than:90d",
            hostname="apartments.com",
            url_path_pattern=r"/[^/]+/[a-z0-9]{7,}",
            mailbox=mailbox,
        )


class FacebookMarketplaceAlertSource(GmailHousingAlertSource):
    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Facebook Marketplace",
            search_url="https://www.facebook.com/marketplace/you/alerts/",
            manual_reason=(
                "Turn on Marketplace saved-search email notifications, then connect Gmail from Alerts. "
                "The source activates only if the alerts contain direct listing links."
            ),
            query="from:(facebookmail.com) Marketplace newer_than:90d",
            hostname="facebook.com",
            url_path_pattern=r"/marketplace/item/",
            mailbox=mailbox,
        )


class ListingsProjectSource:
    """Read the small, public SF feed from Listings Project.

    The source page is a curated weekly collection rather than a syndication
    network.  We retain only cards that explicitly say San Francisco and link
    to Listings Project's own individual listing page, so Oakland/Bay Area
    cards and generic collection links never leak into the monitor.
    """

    platform = "Listings Project"
    mode = "automatic"
    search_url = "https://www.listingsproject.com/real-estate/san-francisco-bay-area"
    manual_reason = None
    detail_budget = 0
    empty_result_message = "The current Bay Area issue has no explicit San Francisco rentals in supported categories."

    _residential_categories = (
        "rooms for rent",
        "rooms for sublet",
        "apartments for rent",
        "apartments for sublet",
        "houses for rent",
        "houses for sublet",
        "homes for rent",
        "homes for sublet",
        "condos for rent",
        "condos for sublet",
    )

    @staticmethod
    def _card_for(anchor):
        parent = anchor.parent
        while parent is not None:
            classes = set(parent.get("class", [])) if getattr(parent, "get", None) else set()
            if parent.name == "div" and {"flex", "flex-col", "mb-8"}.issubset(classes):
                return parent
            parent = parent.parent
        return None

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        response = client.get(self.search_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        listings: list[ListingCandidate] = []
        seen_urls: set[str] = set()
        for anchor in soup.select('h4 a[href^="/listings/"]'):
            card = self._card_for(anchor)
            if card is None:
                continue
            card_text = _clean_text(card.get_text(" ", strip=True), 1200)
            # The SF Bay Area collection includes East Bay cards.  City level is
            # written on every card; accept only an explicit SF address instead
            # of guessing from a regional collection.
            if "san francisco" not in _normal_text(card_text):
                continue
            category_node = card.select_one("div.text-grey-dark.mb-2.text-smish")
            category_text = _clean_text(category_node.get_text(" ", strip=True)) if category_node else ""
            category = _clean_text(category_text.rsplit("|", 1)[-1]) if "|" in category_text else ""
            if not category:
                category_match = re.search(
                    r"\b(?:rooms|apartments|houses|homes|condos)\s+for\s+(?:rent|sublet)\b",
                    card_text,
                    re.IGNORECASE,
                )
                category = _clean_text(category_match.group(0)) if category_match else ""
            if not any(name in _normal_text(category) for name in self._residential_categories):
                continue
            original_url = urljoin(self.search_url, str(anchor.get("href", "")))
            if original_url in seen_urls:
                continue
            seen_urls.add(original_url)
            title = _clean_text(anchor.get_text(" ", strip=True), 180)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(original_url),
                    title=title or "Listings Project rental",
                    original_url=original_url,
                    price=_parse_price(card_text, require_currency=True),
                    neighborhood=visible_sf_area_hint(card_text),
                    listing_type=category or None,
                    summary=card_text,
                    # The source's own listing page contains the lister's
                    # contact details, so it is a higher-value first-contact
                    # route without storing anyone's contact data locally.
                    metadata={"direct_lister": True, "source_collection": "SF Bay Area"},
                )
            )
        if not listings and soup.select('h4 a[href^="/listings/"]'):
            return []
        if not listings and not soup.select('h4 a[href^="/listings/"]'):
            raise SourceError("Listings Project returned no recognizable listing cards; its page format may have changed.")
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class AbacusSource:
    """Read a local small-property manager's public AppFolio availability feed.

    Abacus publishes its own direct application links.  The business describes
    its portfolio as single homes, condos, and small multi-unit buildings, but
    a card without an explicit unit count remains *unknown* so the dashboard's
    <=50-unit rule stays intact.
    """

    platform = "Abacus (small buildings)"
    mode = "automatic"
    search_url = "https://abacus.appfolio.com/listings"
    manual_reason = None
    detail_budget = 10
    empty_result_message = "Abacus currently reports no available rental properties."

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        if not set(preferences.deal_profile.enabled_paths).intersection(
            {"studio", "one_bedroom", "two_bedroom", "three_bedroom"}
        ):
            self.empty_result_message = "Skipped because entire homes are not enabled in Your deal."
            return []
        response = client.get(self.search_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.select(".js-listing-item")
        if not cards:
            no_inventory = soup.select_one(".listings__no-vacancies:not(.u-hidden)")
            if no_inventory and "no available properties" in _normal_text(no_inventory.get_text(" ", strip=True)):
                return []
            raise SourceError("Abacus returned no recognizable availability cards; its page format may have changed.")
        listings: list[ListingCandidate] = []
        for card in cards:
            address = _clean_text(
                (card.select_one(".js-listing-address") or card).get_text(" ", strip=True), 220
            )
            if "san francisco" not in _normal_text(address):
                continue
            title_anchor = card.select_one(".js-listing-title a[href]")
            if title_anchor is None:
                continue
            original_url = urljoin(self.search_url, str(title_anchor.get("href", "")))
            facts = _clean_text(card.get_text(" ", strip=True), 1000)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(original_url),
                    title=_clean_text(title_anchor.get_text(" ", strip=True), 180) or address,
                    original_url=original_url,
                    price=_parse_price(facts, require_currency=True),
                    # A street address is not a neighborhood.  Leave it unknown
                    # unless the manager itself writes a target area.
                    neighborhood=visible_sf_area_hint(facts),
                    listing_type=_clean_text(
                        (card.select_one(".js-listing-blurb-bed-bath") or card).get_text(" ", strip=True),
                        80,
                    ),
                    summary=facts,
                    metadata={"address": address, "small_manager": True},
                )
            )
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        response = client.get(listing.original_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        title = _clean_text((soup.select_one(".js-show-title") or soup.title).get_text(" ", strip=True), 180)
        summary = _clean_text(
            " ".join(
                item.get_text(" ", strip=True)
                for item in soup.select(".header__summary, .description, .js-show-description, .list__item")
            ),
            1800,
        )
        apply_anchor = soup.select_one("a.js-apply-now[href]")
        metadata = dict(listing.metadata)
        if apply_anchor is not None:
            metadata["application_url"] = urljoin(listing.original_url, str(apply_anchor.get("href", "")))
        return replace(
            listing,
            title=title or listing.title,
            price=_parse_price(summary, require_currency=True) or listing.price,
            summary=summary or listing.summary,
            listing_type=(
                _clean_text((soup.select_one(".header__summary") or "").get_text(" ", strip=True), 120)
                or listing.listing_type
            ),
            neighborhood=listing.neighborhood or visible_sf_area_hint(summary),
            metadata=metadata,
        )


def _pets_allowed(value: object) -> bool | None:
    """schema.org petsAllowed arrives as a boolean or as Yes/No text."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"yes", "true"}:
        return True
    if text in {"no", "false"}:
        return False
    return None


def _json_ld_blocks(document: str) -> list[dict]:
    """Every schema.org object on a page, flattened out of its script tags."""
    soup = BeautifulSoup(document, "html.parser")
    blocks: list[dict] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            parsed = json.loads(node.string or node.get_text() or "")
        except (ValueError, json.JSONDecodeError):
            continue
        for entry in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(entry, dict):
                blocks.append(entry)
    return blocks


def _schema_types(node: dict) -> set[str]:
    raw = node.get("@type")
    return {str(value) for value in (raw if isinstance(raw, list) else [raw]) if value}


def _offer_price(node: object, key: str = "lowPrice") -> int | None:
    if not isinstance(node, dict):
        return None
    for candidate in (key, "price", "lowPrice"):
        value = node.get(candidate)
        if isinstance(value, (int, float)) and value > 0:
            return int(round(value))
    return None


class SFHousingPortalSource:
    """Read the City of San Francisco's own below-market-rate rental portal.

    DAHLIA is public government data served as plain JSON, so unlike every
    other source here it needs no key, conflicts with no site's terms, and
    cannot quietly start refusing unattended requests. Each record is a
    building offering several unit types, so a building with a studio and a
    one-bedroom becomes two candidates the deal profile can judge separately.
    """

    platform = "SF Housing Portal"
    mode = "automatic"
    search_url = "https://housing.sfgov.org/listings"
    api_url = "https://housing.sfgov.org/api/v1/listings.json"
    manual_reason = None
    detail_budget = 0
    empty_result_message = "The city portal currently lists no open below-market rentals."

    # DAHLIA's own unit vocabulary, mapped to the metadata hint that
    # classification.py already understands. SRO is a single room, not a home.
    UNIT_TYPES = {
        "studio": ("studio", WHOLE_UNIT),
        "sro": (None, ROOM),
        "1 br": ("1 bedroom", WHOLE_UNIT),
        "2 br": ("2 bedroom", WHOLE_UNIT),
        "3 br": ("3 bedroom", WHOLE_UNIT),
        "4 br": (None, WHOLE_UNIT),
    }

    @staticmethod
    def _timestamp(value: object) -> str | None:
        """Normalise Salesforce's +0000 offset to something fromisoformat accepts."""
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("+0000"):
            text = f"{text[:-5]}+00:00"
        return text

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        response = client.get(self.api_url, headers={"Accept": "application/json"})
        response.raise_for_status()
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SourceError("The SF housing portal returned a non-JSON response.") from exc
        records = payload.get("listings") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise SourceError(
                "The SF housing portal response did not contain a listings array; its API may have changed."
            )
        listings: list[ListingCandidate] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            # Resale and new-sale rows are ownership opportunities, not rentals.
            if "rental" not in str(record.get("Tenure") or "").casefold():
                continue
            if _normal_text(record.get("Building_City")) != "san francisco":
                continue
            listings.extend(self._candidates(record))
        return listings

    def _candidates(self, record: dict) -> list[ListingCandidate]:
        listing_id = _clean_text(str(record.get("Id") or record.get("listingID") or ""), 60)
        name = _clean_text(record.get("Name"), 180)
        if not listing_id or not name:
            return []
        address = _clean_text(record.get("Building_Street_Address"), 220)
        modified = self._timestamp(record.get("LastModifiedDate"))
        due = self._timestamp(record.get("Application_Due_Date"))
        units_available = record.get("Units_Available")
        reserved_for = _clean_text(record.get("Reserved_community_type"), 60)
        summaries = record.get("unitSummaries")
        if not isinstance(summaries, dict):
            return []

        # A building can publish the same unit type in both the general and the
        # income-reserved bucket. Keep the lowest rent offered for each type
        # rather than emitting two near-identical rows.
        cheapest: dict[str, tuple[str, float, object]] = {}
        for bucket in ("general", "reserved"):
            for unit in summaries.get(bucket) or []:
                if not isinstance(unit, dict):
                    continue
                raw_type = _clean_text(unit.get("unitType"), 40)
                rent = unit.get("minMonthlyRent")
                if not raw_type or not isinstance(rent, (int, float)) or rent <= 0:
                    continue
                key = raw_type.casefold()
                if key not in cheapest or rent < cheapest[key][1]:
                    cheapest[key] = (raw_type, float(rent), unit.get("totalUnits"))

        listings: list[ListingCandidate] = []
        for key, (raw_type, rent, total_units) in cheapest.items():
            unit_hint, housing_kind = self.UNIT_TYPES.get(key, (None, UNKNOWN))
            # The portal gives one page per building, but each unit type is a
            # different home at a different rent. Without a distinct URL the
            # repository's canonical-URL dedup treats them as one listing and
            # the cheaper studio disappears behind the two-bedroom. The portal
            # ignores the extra parameter and serves the same page.
            unit_slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "unit"
            detail = [f"Below-market-rate {raw_type} through the San Francisco housing portal."]
            if address:
                detail.append(f"Building at {address}.")
            if isinstance(total_units, (int, float)) and total_units:
                detail.append(f"{int(total_units)} {raw_type} units in this listing.")
            if due:
                detail.append(f"Applications due {due[:10]}.")
            if reserved_for:
                detail.append(f"Reserved for {reserved_for.casefold()} applicants.")
            metadata: dict[str, object] = {
                "address": address,
                "below_market_rate": True,
                "application_due_date": due,
                "units_available": units_available,
                "sf_portal_unit_type": raw_type,
            }
            if unit_hint:
                metadata["unit_type"] = unit_hint
            if modified:
                metadata["listing_timestamp"] = modified
            if reserved_for:
                metadata["reserved_community_type"] = reserved_for
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=f"{listing_id}:{key}",
                    title=f"{name} - {raw_type}",
                    original_url=f"{self.search_url}/{listing_id}?unit={unit_slug}",
                    price=int(round(rent)),
                    # A street address is not a neighborhood; only take one the
                    # portal actually names.
                    neighborhood=visible_sf_area_hint(f"{name} {address}"),
                    listing_type=raw_type,
                    summary=_clean_text(" ".join(detail), 1000),
                    metadata=metadata,
                    housing_kind=housing_kind,
                )
            )
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class ApartmentListSource:
    """Read Apartment List's published schema.org data for San Francisco.

    The search feed is deliberately thin: a name, a link, an image and a price
    range, with no bedroom count and no address. On its own that is not enough
    to place a building in any of the three shortlists, so each result is
    completed from its own page, which publishes one ``Apartment`` block per
    unit type along with the building's address, amenities, unit count and a
    last-modified date.

    A building is represented by its cheapest available unit, because that is
    the one that decides whether the building is worth opening at all. The
    other unit types are named in the summary so nothing is hidden.
    """

    platform = "Apartment List"
    mode = "automatic"
    search_url = "https://www.apartmentlist.com/ca/san-francisco"
    manual_reason = None
    # The search feed cannot classify a building on its own, so these are worth
    # completing generously; the scanner still stops at its own deadline.
    detail_budget = 20
    empty_result_message = "Apartment List published no San Francisco buildings in its structured data."

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        response = client.get(self.search_url)
        response.raise_for_status()
        products = [block for block in _json_ld_blocks(response.text) if "Product" in _schema_types(block)]
        if not products:
            raise SourceError(
                "Apartment List returned no structured building data; its page format may have changed."
            )
        listings: list[ListingCandidate] = []
        for product in products:
            name = _clean_text(product.get("name"), 180)
            url = str(product.get("url") or product.get("@id") or "").strip()
            if not name or not url.startswith("https://www.apartmentlist.com/"):
                continue
            offers = product.get("offers") if isinstance(product.get("offers"), dict) else {}
            price = _offer_price(offers)
            high = offers.get("highPrice") if isinstance(offers.get("highPrice"), (int, float)) else None
            detail = [f"{name} on Apartment List."]
            if price and high and high > price:
                detail.append(f"Published rents run from ${price:,} to ${int(round(high)):,} a month.")
            elif price:
                detail.append(f"Published rent from ${price:,} a month.")
            detail.append("Home size is unconfirmed until this building's own page is read.")
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(url),
                    title=name,
                    original_url=url,
                    price=price,
                    neighborhood=visible_sf_area_hint(name),
                    listing_type="Apartment building",
                    summary=_clean_text(" ".join(detail), 700),
                    metadata={
                        "building_listing": True,
                        "price_low": price,
                        "price_high": int(round(high)) if high else None,
                    },
                    # Apartment List rents whole apartments, never a room in
                    # someone's home, so the workflow is known even when the
                    # number of bedrooms is not.
                    housing_kind=WHOLE_UNIT,
                )
            )
        return listings

    @staticmethod
    def _representative_unit(blocks: list[dict]) -> tuple[dict, int | None] | None:
        """The unit that decides whether this building is worth opening.

        A priced home wins, cheapest first. Smaller buildings often publish
        their unit mix with no rents at all; rather than leave those
        unclassified and invisible, the smallest home stands in and the
        building's own starting rent is kept.
        """
        units = [block for block in blocks if "Apartment" in _schema_types(block)]
        priced = [
            (unit, price)
            for unit, price in ((unit, _offer_price(unit.get("offers"))) for unit in units)
            if price is not None
        ]
        if priced:
            return min(priced, key=lambda pair: pair[1])
        sized = [unit for unit in units if isinstance(unit.get("numberOfBedrooms"), (int, float))]
        if sized:
            return min(sized, key=lambda unit: unit["numberOfBedrooms"]), None
        return None

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        """Complete a building from its own page: size, address, date, amenities."""
        response = client.get(listing.original_url)
        response.raise_for_status()
        blocks = _json_ld_blocks(response.text)
        complexes = [b for b in blocks if "ApartmentComplex" in _schema_types(b)]
        building = complexes[0] if complexes else {}

        metadata = dict(listing.metadata)
        detail = [f"{listing.title} on Apartment List."]

        address = building.get("address") if isinstance(building.get("address"), dict) else {}
        street = _clean_text(address.get("streetAddress"), 160)
        if street:
            metadata["address"] = street
            detail.append(f"Address: {street}.")

        # The <=50-unit rule needs a real count; an unknown one stays unknown.
        units = building.get("numberOfAvailableAccommodationUnits")
        building_units = None
        if isinstance(units, dict) and isinstance(units.get("value"), (int, float)):
            building_units = int(units["value"])
        elif isinstance(units, (int, float)):
            building_units = int(units)

        representative = self._representative_unit(blocks)
        price = listing.price
        unit_label = ""
        if representative is not None:
            unit, unit_price = representative
            price = unit_price or listing.price
            bedrooms = unit.get("numberOfBedrooms")
            if isinstance(bedrooms, (int, float)):
                metadata["bedrooms"] = int(bedrooms)
                unit_label = "studio" if int(bedrooms) == 0 else f"{int(bedrooms)}-bedroom"
            floor = unit.get("floorSize")
            if isinstance(floor, dict) and isinstance(floor.get("value"), (int, float)):
                metadata["floor_size_sqft"] = int(floor["value"])
            if unit_price and unit_label:
                detail.append(f"Cheapest available home is a {unit_label} from ${unit_price:,} a month.")
            elif unit_price:
                detail.append(f"Cheapest available home is from ${unit_price:,} a month.")
            elif unit_label:
                detail.append(
                    f"Smallest home listed is a {unit_label}; this building publishes no rent per home."
                )

        other = sorted(
            {
                _clean_text(block.get("name"), 60).split(" - ")[-1]
                for block in blocks
                if "Apartment" in _schema_types(block) and block.get("name")
            }
        )
        if len(other) > 1:
            detail.append("This building also lists: " + ", ".join(other) + ".")

        amenities = [
            _clean_text(feature.get("name"), 48)
            for feature in building.get("amenityFeature") or []
            if isinstance(feature, dict) and feature.get("name")
        ]
        if amenities:
            detail.append("Amenities: " + ", ".join(amenities[:12]) + ".")
        if _pets_allowed(building.get("petsAllowed")) is True:
            detail.append("Pets allowed.")

        for block in blocks:
            if block.get("@type") == "WebPage" and block.get("dateModified"):
                metadata["listing_timestamp"] = _clean_text(block["dateModified"], 40)
                break

        return replace(
            listing,
            price=price or listing.price,
            neighborhood=(
                listing.neighborhood
                or sf_target_coordinate_neighborhood(
                    (building.get("geo") or {}).get("latitude"),
                    (building.get("geo") or {}).get("longitude"),
                )
                or sf_area_from_slug(listing.original_url)
                or visible_sf_area_hint(f"{listing.title} {street}")
            ),
            listing_type=f"{unit_label.capitalize()} apartment" if unit_label else listing.listing_type,
            summary=_clean_text(" ".join(detail), 1200),
            building_units=building_units,
            metadata=metadata,
        )


class ZumperSource:
    """Read the schema.org search feed Zumper publishes for San Francisco.

    Zumper was previously listed as blocked, but it answers ordinary requests
    and publishes a full ``SearchResultsPage`` with a bedroom count, address,
    amenities and — unusually among the free sources — a real ``datePosted``.

    What it does not publish on the search page is rent: only individual-unit
    listings carry an offer, while apartment buildings load their price after
    the page renders. Those buildings are enriched one at a time within the
    scanner's detail budget, and until that happens their rent stays unknown
    rather than being guessed at.
    """

    platform = "Zumper"
    mode = "automatic"
    search_url = "https://www.zumper.com/apartments-for-rent/san-francisco-ca"
    manual_reason = None
    detail_budget = 10
    empty_result_message = "Zumper published no San Francisco results in its structured data."

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        response = client.get(self.search_url)
        response.raise_for_status()
        entries: list[dict] = []
        for block in _json_ld_blocks(response.text):
            if block.get("@type") != "SearchResultsPage":
                continue
            main = block.get("mainEntity")
            if isinstance(main, dict):
                entries = [e for e in main.get("itemListElement") or [] if isinstance(e, dict)]
            break
        if not entries:
            raise SourceError(
                "Zumper returned no structured search results; its page format may have changed."
            )

        listings: list[ListingCandidate] = []
        for entry in entries:
            item = entry.get("item")
            if not isinstance(item, dict) or item.get("@type") != "RealEstateListing":
                continue
            url = str(item.get("url") or item.get("@id") or "").strip()
            name = _clean_text(item.get("name"), 180)
            if not name or not url.startswith("https://www.zumper.com/"):
                continue
            about = item.get("about") if isinstance(item.get("about"), dict) else {}
            address = about.get("address") if isinstance(about.get("address"), dict) else {}
            locality = _normal_text(address.get("addressLocality"))
            # The feed is city-scoped, but a neighbouring city slipping in would
            # quietly break the whole point of the search.
            if locality and locality != "san francisco":
                continue

            street = _clean_text(address.get("streetAddress"), 160)
            amenities = [
                _clean_text(feature.get("name"), 48)
                for feature in about.get("amenityFeature") or []
                if isinstance(feature, dict) and feature.get("value") and feature.get("name")
            ]
            bedrooms = about.get("numberOfBedrooms")
            pets = _pets_allowed(about.get("petsAllowed"))

            detail = [f"{name} listed on Zumper."]
            if street:
                detail.append(f"Address: {street}.")
            if amenities:
                # Amenities are scored from the listing text, so they belong in
                # the summary rather than in a metadata key nothing reads.
                detail.append("Amenities: " + ", ".join(amenities[:12]) + ".")
            if pets is True:
                detail.append("Pets allowed.")
            elif pets is False:
                detail.append("No pets.")

            price = _offer_price(item.get("offers"))
            if price is None:
                detail.append("Rent is not published in the search feed and is still unconfirmed.")

            metadata: dict[str, object] = {"address": street, "zumper_amenities": amenities}
            if isinstance(bedrooms, (int, float, str)) and str(bedrooms).strip() != "":
                metadata["bedrooms"] = bedrooms
            posted = _clean_text(item.get("datePosted"), 40)
            if posted:
                metadata["listing_timestamp"] = posted

            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(url),
                    title=name,
                    original_url=url,
                    price=price,
                    neighborhood=sf_area_from_slug(url) or visible_sf_area_hint(f"{name} {street}"),
                    listing_type="Apartment rental",
                    summary=_clean_text(" ".join(detail), 1200),
                    metadata=metadata,
                    housing_kind=WHOLE_UNIT,
                )
            )
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        """Fetch the building page only to recover a rent the search feed omitted."""
        if listing.price:
            return listing
        response = client.get(listing.original_url)
        response.raise_for_status()
        price = None
        for block in _json_ld_blocks(response.text):
            price = price or _offer_price(block.get("offers"))
        if price is None:
            return listing
        metadata = dict(listing.metadata)
        metadata["zumper_price_source"] = "building page"
        summary = listing.summary.replace(
            " Rent is not published in the search feed and is still unconfirmed.", ""
        )
        return replace(
            listing,
            price=price,
            summary=_clean_text(f"{summary} Rents from ${price:,} a month.", 1200),
            metadata=metadata,
        )


class ApifyFacebookMarketplaceSource:
    """Optional low-volume Facebook automation that does not use a FB login."""

    platform = "Facebook Marketplace"
    mode = "automatic"
    detail_budget = 0
    provider = "apify"
    connector_key = "apify"
    scheduled_only = True
    manual_scan_enabled = True
    results_limit = 5
    monthly_run_limit = 60
    request_timeout_seconds = 75.0
    actor_url = "https://api.apify.com/v2/acts/apify~facebook-marketplace-scraper/run-sync-get-dataset-items"
    search_url = "https://www.facebook.com/marketplace/114952118516947/propertyrentals/"
    manual_reason = None

    def __init__(self, tokens: ApifyTokenStore):
        self.tokens = tokens

    @staticmethod
    def _marketplace_url(preferences: Preferences) -> str:
        budget = preferences.section("budget")
        whole_unit = preferences.section("whole_unit")
        two_bedroom = preferences.section("two_bedroom")
        three_bedroom = preferences.section("three_bedroom")
        query: dict[str, str | int] = {
            # The normal keyword search leaks a large number of irrelevant Bay
            # Area products. The city-scoped Property Rentals feed is where
            # Marketplace's room listings, including the user's verified
            # examples, are actually published.
            "sortBy": "creation_time_descend",
            "radius": 10,
            "exact": "false",
        }
        if budget.get("min_monthly") is not None:
            query["minPrice"] = max(0, round(int(budget["min_monthly"]) * 0.95))
        if budget.get("max_monthly") is not None:
            maximum = int(budget["max_monthly"])
            if whole_unit.get("enabled", True) is True:
                maximum = max(maximum, int(whole_unit.get("max_monthly", 3000)))
            if two_bedroom.get("enabled", True) is True:
                shared_total = int(two_bedroom.get("occupants", 2)) * int(
                    two_bedroom.get("max_per_person", 2700)
                )
                maximum = max(maximum, shared_total)
            if three_bedroom.get("enabled", True) is True:
                shared_total = int(three_bedroom.get("occupants", 3)) * int(
                    three_bedroom.get("max_per_person", 2500)
                )
                maximum = max(maximum, shared_total)
            query["maxPrice"] = round(maximum * 1.05)
        return (
            "https://www.facebook.com/marketplace/114952118516947/propertyrentals/?"
            + urlencode(query)
        )

    @staticmethod
    def _flatten(value, limit: int = 420) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return _clean_text(value, limit)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return _clean_text(" ".join(ApifyFacebookMarketplaceSource._flatten(item) for item in value), limit)
        if isinstance(value, dict):
            preferred = ["text", "display_name", "formatted_amount", "formatted", "name", "value"]
            ordered = [value[key] for key in preferred if key in value]
            if not ordered:
                ordered = list(value.values())
            return _clean_text(" ".join(ApifyFacebookMarketplaceSource._flatten(item) for item in ordered), limit)
        return ""

    @staticmethod
    def _price(item: dict) -> int | None:
        for field in ("listingPrice", "listing_price", "min_listing_price", "max_listing_price"):
            value = item.get(field)
            if value is None:
                continue
            formatted = ApifyFacebookMarketplaceSource._flatten(value)
            price = _parse_price(formatted, require_currency=True)
            if price is not None:
                return price
            if isinstance(value, dict):
                raw = value.get("amount", value.get("value"))
            else:
                raw = value
            try:
                numeric = float(str(raw).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if numeric > 10_000:
                numeric /= 100
            return round(numeric)
        return None

    @staticmethod
    def _direct_item_url(item: dict) -> str | None:
        for field in ("itemUrl", "listingUrl", "listing_url", "url"):
            match = re.search(r"https?://(?:www\.)?facebook\.com/marketplace/item/(\d+)", str(item.get(field, "")))
            if match:
                return f"https://www.facebook.com/marketplace/item/{match.group(1)}/"
        return None

    @classmethod
    def _location_text(cls, item: dict) -> str:
        """Prefer Facebook's structured reverse-geocode over a raw coordinate blob."""
        location = item.get("location")
        parts: list[str] = []
        if isinstance(location, dict):
            detailed = location.get("reverse_geocode_detailed")
            reverse = location.get("reverse_geocode")
            for value in (detailed, reverse, location):
                if isinstance(value, dict):
                    city = value.get("city")
                    state = value.get("state")
                    postal = value.get("postal_code")
                    if city:
                        parts.append(str(city))
                    if state:
                        parts.append(str(state))
                    if postal:
                        parts.append(str(postal))
            latitude, longitude = location.get("latitude"), location.get("longitude")
            if latitude is not None and longitude is not None:
                parts.append(f"{latitude} {longitude}")
        raw = cls._flatten(item.get("locationText"))
        if raw:
            parts.append(raw)
        return _clean_text(" ".join(dict.fromkeys(parts)), 180)

    @classmethod
    def _detail_text(cls, item: dict) -> str:
        """Flatten Property Rentals' structured facts into scoreable, user-visible text."""
        return cls._flatten(item.get("details"), 1_400)

    @staticmethod
    def _is_san_francisco(location: str) -> bool:
        """Keep the city feed honest when Marketplace leaks nearby Bay Area cards."""
        text = _normal_text(location)
        if "san francisco" in text:
            return True
        # A source-provided city beats a nearby latitude/longitude. The south
        # city boundary is close enough to Daly City that coordinate-only
        # containment cannot safely distinguish every address.
        nearby_cities = ("daly city", "brisbane", "south san francisco", "pacifica", "colma")
        if any(city in text for city in nearby_cities):
            return False
        match = re.search(r"\b(37\.\d{4,})\s+(-122\.\d{4,})\b", location)
        if not match:
            return False
        latitude, longitude = (float(match.group(1)), float(match.group(2)))
        # City-level containment only; neighborhood ranking happens separately.
        return 37.7060 <= latitude <= 37.8325 and -122.5300 <= longitude <= -122.3500

    def parse_rows(self, rows: list[object], preferences: Preferences) -> list[ListingCandidate]:
        """Normalize a completed actor dataset without issuing another actor run."""
        listings: list[ListingCandidate] = []
        actor_errors = 0
        for item in rows:
            if not isinstance(item, dict):
                continue
            if item.get("error") or item.get("errorDescription"):
                actor_errors += 1
                continue
            if item.get("is_sold") is True or item.get("isSold") is True:
                continue
            if item.get("is_live") is False or item.get("isLive") is False:
                continue
            original_url = self._direct_item_url(item)
            title = _clean_text(
                str(
                    item.get("listingTitle")
                    or item.get("marketplace_listing_title")
                    or item.get("custom_title")
                    or item.get("customTitle")
                    or ""
                ),
                180,
            )
            if not original_url or not title:
                continue
            description = self._flatten(item.get("description"), 1_200)
            location = self._location_text(item)
            detail_text = self._detail_text(item)
            if not self._is_san_francisco(location):
                # The Facebook category's radius is advisory, not a guaranteed
                # boundary. Do not clutter the local archive with Daly City,
                # Richmond, Oakland, or San Rafael cards.
                continue
            context = " ".join(part for part in (location, title, description, detail_text) if part)
            coordinate_neighborhood = facebook_coordinate_neighborhood(location)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(original_url),
                    title=title,
                    original_url=original_url,
                    price=self._price(item),
                    neighborhood=(
                        GmailHousingAlertSource._neighborhood(context, preferences)
                        or coordinate_neighborhood
                        or location
                        or None
                    ),
                    listing_type="Marketplace rental",
                    summary=_clean_text(" ".join(part for part in (description, detail_text) if part), 1_400)
                    or _clean_text(context, 420),
                    metadata={
                        "apify_actor": "apify/facebook-marketplace-scraper",
                        "listing_timestamp": item.get("timestamp"),
                    },
                )
            )
        if not listings:
            detail = f" ({actor_errors} actor error row(s))" if actor_errors else ""
            raise SourceError(f"The Facebook helper returned no parseable active San Francisco listing links{detail}.")
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        token = self.tokens.token
        if not token:
            raise SourceError("The Apify token is missing or invalid; reconnect it from Alerts.")
        if not self.tokens.reserve_monthly_run(limit=self.monthly_run_limit):
            raise SourceError(
                "The local 60-run monthly Facebook safety cap was reached (or its usage file is invalid)."
            )
        self.search_url = self._marketplace_url(preferences)
        whole_unit = preferences.section("whole_unit")
        results_limit = self.results_limit
        if whole_unit.get("enabled", True) is True:
            try:
                results_limit = int(
                    preferences.section("sources").get("facebook_marketplace_results_per_scan", 10)
                )
            except (TypeError, ValueError) as exc:
                raise SourceError("The Facebook result limit must be a whole number.") from exc
            results_limit = max(self.results_limit, min(results_limit, 10))
        payload = {
            "startUrls": [{"url": self.search_url}],
            # Ten newest detailed rental cards twice a day cover all housing
            # workflows while keeping this personal monitor deliberately small.
            "resultsLimit": results_limit,
            "includeListingDetails": True,
        }
        try:
            response = client.post(
                self.actor_url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SourceError(
                f"The Facebook helper exceeded its {int(self.request_timeout_seconds)}-second time limit."
            ) from exc
        if response.status_code in {401, 403}:
            raise SourceError("Apify rejected the API token; reconnect it from Alerts.")
        if response.status_code in {402, 429}:
            raise SourceError("The Apify free-credit or rate limit was reached; no charge was attempted here.")
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError("The Facebook helper returned invalid JSON.") from exc
        if not isinstance(rows, list):
            raise SourceError("The Facebook helper returned an unexpected result format.")
        return self.parse_rows(rows, preferences)

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class FacebookMarketplaceSource:
    """Use native Marketplace alerts when present; otherwise retain the capped fallback."""

    platform = "Facebook Marketplace"
    detail_budget = 0
    manual_scan_enabled = True

    def __init__(self, mailbox: GmailAlertMailbox, tokens: ApifyTokenStore):
        self.alerts = FacebookMarketplaceAlertSource(mailbox)
        self.apify = ApifyFacebookMarketplaceSource(tokens)
        self.tokens = tokens
        self.last_provider = "gmail_alerts"

    @property
    def connector_key(self) -> str:
        return "gmail" if self.last_provider == "gmail_alerts" else "apify"

    @property
    def connector_state_key(self) -> str:
        return self.alerts.connector_state_key if self.last_provider == "gmail_alerts" else "apify"

    @property
    def last_alert_count(self) -> int:
        return self.alerts.last_alert_count if self.last_provider == "gmail_alerts" else 0

    @property
    def mode(self) -> str:
        if self.tokens.is_configured:
            return "automatic"
        return self.alerts.mode

    @property
    def scheduled_only(self) -> bool:
        return self.tokens.is_configured

    @property
    def search_url(self) -> str:
        return self.apify.search_url if self.tokens.is_configured else self.alerts.search_url

    @property
    def manual_reason(self) -> str:
        if self.tokens.is_configured:
            return ""
        if self.alerts.mode == "setup":
            return "Connect the capped Apify free-tier helper (recommended) or Gmail alert fallback from Alerts."
        return self.alerts.manual_reason

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        # Native saved-search email carries the user's exact Marketplace search
        # and does not consume a third-party scraping run. Keep Apify available
        # until actual direct-link alert emails arrive, since a newly connected
        # Gmail account may legitimately have no Facebook alerts yet.
        if self.alerts.mode == "automatic":
            try:
                native_listings = self.alerts.search(client, preferences)
            except SourceError:
                native_listings = []
            if native_listings:
                self.last_provider = "gmail_alerts"
                return native_listings
        if self.tokens.is_configured:
            self.last_provider = "apify"
            return self.apify.search(client, preferences)
        self.last_provider = "gmail_alerts"
        return self.alerts.search(client, preferences)

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class FacebookGroupsSource:
    """Monitor configured *public* housing groups with a capped Apify actor call."""

    platform = "Facebook Groups"
    provider = "apify"
    connector_key = "apify"
    detail_budget = 0
    scheduled_only = True
    manual_scan_enabled = True
    results_limit = 5
    manual_results_limit = 20
    monthly_post_limit = 400
    request_timeout_seconds = 75.0
    actor_url = "https://api.apify.com/v2/acts/apify~facebook-groups-scraper/run-sync-get-dataset-items"

    _HOUSING_TERMS = re.compile(
        r"\b(?:room|bedroom|sublet|sublease|lease\s*takeover|roommate|housing|rental|rent)\b",
        re.IGNORECASE,
    )
    _STRONG_OFFER_TERMS = re.compile(
        r"(?:"
        r"^[^a-z0-9]{0,12}(?:private\s+)?(?:room|bedroom|studio|apartment|flat|house|unit)\b[^.!?]{0,40}\b(?:available|for\s+rent)\b|"
        r"\b(?:i|we)\s+have\s+(?:a\s+)?(?:private\s+)?(?:room|bedroom|studio|apartment|flat|house|unit)\b|"
        r"\b(?:my|our)\s+(?:room|bedroom|studio|apartment|flat|house|unit|lease)\b[^.!?]{0,60}\b(?:available|rent|sublet|sublease|takeover)\b|"
        r"\brenting\s+out\b|\broommate\s+(?:wanted|needed)\b|"
        r"\blooking\s+for\s+(?:someone|a\s+tenant|a\s+roommate|roommates?)[^.]{0,100}"
        r"\b(?:take\s+over|fill|rent|sublet|share\s+my|move\s+into)\b"
        r")",
        re.IGNORECASE,
    )
    _WANTED_TERMS = re.compile(
        r"\b(?:looking|searching|seeking)\s+(?:for|to\s+(?:find|join|move))\b|"
        r"\bneed\s+(?:a|an)\s+(?:room|sublet|place|apartment|house)\b|"
        r"\blooking\s+to\s+(?:move|join\s+a\s+lease)\b",
        re.IGNORECASE,
    )

    def __init__(self, tokens: ApifyTokenStore):
        self.tokens = tokens
        self.search_url = "https://www.facebook.com/groups/"

    @staticmethod
    def _group_urls(preferences: Preferences) -> list[str]:
        configured = preferences.section("sources").get("facebook_group_urls", [])
        if not isinstance(configured, list):
            return []
        urls: list[str] = []
        for value in configured:
            parsed = urlsplit(str(value).strip())
            if parsed.scheme == "https" and parsed.netloc.casefold() in {"facebook.com", "www.facebook.com"}:
                if parsed.path.casefold().startswith("/groups/"):
                    urls.append(f"https://www.facebook.com{parsed.path.rstrip('/')}/")
        return list(dict.fromkeys(urls))

    @property
    def mode(self) -> str:
        return "automatic" if self.tokens.is_configured else "setup"

    @property
    def manual_reason(self) -> str:
        if not self.tokens.is_configured:
            return "Connect Apify before monitoring the configured public Facebook group."
        return ""

    @classmethod
    def _is_housing_offer(cls, text: str) -> bool:
        if not cls._HOUSING_TERMS.search(text):
            return False
        if not cls._WANTED_TERMS.search(text):
            # Ambiguous housing posts remain reviewable so terse real offers
            # are not lost. Wanted ads need explicit proof that a home exists.
            return True
        return bool(cls._STRONG_OFFER_TERMS.search(text))

    @staticmethod
    def _post_url(item: dict) -> str | None:
        value = str(item.get("url") or item.get("postUrl") or "")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc.casefold() not in {"facebook.com", "www.facebook.com"}:
            return None
        path = parsed.path.rstrip("/")
        if "/groups/" not in path.casefold() or not any(part in path.casefold() for part in ("/permalink/", "/posts/")):
            return None
        return f"https://www.facebook.com{path}/"

    @staticmethod
    def _title(text: str) -> str:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return _clean_text(first_line, 180) or "Facebook group housing post"

    def parse_rows(self, rows: list[object], preferences: Preferences) -> list[ListingCandidate]:
        listings: list[ListingCandidate] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            post_url = self._post_url(item)
            text = _clean_text(str(item.get("text") or item.get("caption") or ""), 1_400)
            if not post_url or not text or not self._is_housing_offer(text):
                continue
            if declared_outside_sf_area_hint(text):
                continue
            group_url = str(item.get("inputUrl") or item.get("facebookUrl") or "")
            group_title = _clean_text(str(item.get("groupTitle") or ""), 180)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=hashlib.sha256(post_url.encode("utf-8")).hexdigest()[:20],
                    title=self._title(text),
                    original_url=post_url,
                    price=_parse_price(text, require_currency=True),
                    neighborhood=GmailHousingAlertSource._neighborhood(text, preferences),
                    listing_type="Group housing post",
                    summary=text,
                    metadata={
                        "listing_timestamp": item.get("time") or item.get("timestamp"),
                        "facebook_group_title": group_title,
                        "facebook_group_url": group_url,
                    },
                )
            )
        return listings

    def _search(
        self,
        client: httpx.Client,
        preferences: Preferences,
        *,
        results_limit: int,
    ) -> list[ListingCandidate]:
        token = self.tokens.token
        if not token:
            raise SourceError("The Apify token is missing or invalid; reconnect it from Alerts.")
        group_urls = self._group_urls(preferences)
        if not group_urls:
            return []
        self.search_url = group_urls[0]
        if not self.tokens.reserve_monthly_group_posts(results_limit, self.monthly_post_limit):
            raise SourceError(
                f"The local {self.monthly_post_limit}-post monthly Facebook Groups safety cap was reached."
            )
        payload = {
            "startUrls": [{"url": url} for url in group_urls],
            "resultsLimit": results_limit,
            "viewOption": "CHRONOLOGICAL",
            "onlyPostsNewerThan": "14 days",
        }
        try:
            response = client.post(
                self.actor_url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SourceError(
                f"The Facebook Groups helper exceeded its {int(self.request_timeout_seconds)}-second time limit."
            ) from exc
        if response.status_code in {401, 403}:
            raise SourceError("Apify rejected the token or the configured group is private. Only public groups can run here.")
        if response.status_code in {402, 429}:
            raise SourceError("The Apify free-credit or rate limit was reached; no additional group posts were imported.")
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError("The Facebook Groups helper returned invalid JSON.") from exc
        if not isinstance(rows, list):
            raise SourceError("The Facebook Groups helper returned an unexpected result format.")
        return self.parse_rows(rows, preferences)

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return self._search(client, preferences, results_limit=self.results_limit)

    def search_for_trigger(
        self,
        client: httpx.Client,
        preferences: Preferences,
        trigger: str,
    ) -> list[ListingCandidate]:
        results_limit = (
            self.manual_results_limit
            if trigger in {"manual", "facebook_groups_test"}
            else self.results_limit
        )
        return self._search(client, preferences, results_limit=results_limit)

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class FurnishedFinderSource:
    """Legacy bounded Furnished Finder connector kept disabled by default.

    The actual supported path is the free Chrome bridge, which imports the
    results a normal browser has rendered. This class remains in the source
    list only so the dashboard can explain that setup before the first bridge
    import; it never spends Apify credit or runs unattended requests.
    """

    platform = "Furnished Finder"
    detail_budget = 0
    scheduled_only = True
    results_limit = 5
    monthly_listing_limit = 300
    request_timeout_seconds = 50.0
    actor_url = "https://api.apify.com/v2/acts/crawlerbros~furnished-finder-scraper/run-sync-get-dataset-items"
    search_url = "https://www.furnishedfinder.com/housing/us--ca--san-francisco"

    def __init__(self, tokens: ApifyTokenStore, *, enabled: bool = False):
        self.tokens = tokens
        # The current community actor was verified against the live account and
        # did not finish inside the product's two-minute scan budget. Leave it
        # opt-in until a provider proves it can do so reliably.
        self.enabled = enabled

    @property
    def mode(self) -> str:
        return "setup"

    @property
    def manual_reason(self) -> str:
        return "Install the free Furnished Finder Chrome bridge and sync one saved search from Alerts."

    @staticmethod
    def _property_url(value: object) -> str | None:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme != "https" or parsed.netloc.casefold() not in {
            "furnishedfinder.com",
            "www.furnishedfinder.com",
        }:
            return None
        if not parsed.path.casefold().startswith("/property/"):
            return None
        return f"https://www.furnishedfinder.com{parsed.path.rstrip('/')}"

    @staticmethod
    def _source_id(row: dict[str, object], url: str) -> str:
        for key in ("id", "listingId", "propertyId"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return _source_id(url)

    @staticmethod
    def _coordinate_neighborhood(row: dict[str, object]) -> str | None:
        try:
            latitude = float(row.get("latitude"))
            longitude = float(row.get("longitude"))
        except (TypeError, ValueError):
            return None
        return facebook_coordinate_neighborhood(f"{latitude} {longitude}")

    @staticmethod
    def _text_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [_clean_text(str(item), 120) for item in value if _clean_text(str(item), 120)]

    @classmethod
    def _summary(cls, row: dict[str, object]) -> str:
        parts: list[str] = []
        for key in ("description", "spaceDescription", "neighborhoodDescription"):
            value = _clean_text(str(row.get(key) or ""), 520)
            if value:
                parts.append(value)
        if row.get("availableOnDate") or row.get("availableFromDate"):
            parts.append(f"Available: {row.get('availableOnDate') or row.get('availableFromDate')}")
        if row.get("minimumStayDays") not in (None, ""):
            parts.append(f"Minimum stay: {row['minimumStayDays']} days")
        if row.get("bathroomType"):
            parts.append(f"Bath: {row['bathroomType']}")
        amenities = cls._text_list(row.get("amenities"))
        if amenities:
            parts.append("Amenities: " + ", ".join(amenities[:12]))
        house_rules = cls._text_list(row.get("houseRules"))
        if house_rules:
            parts.append("House rules: " + ", ".join(house_rules[:8]))
        return _clean_text(". ".join(parts), 1_400)

    def parse_rows(self, rows: list[object], preferences: Preferences) -> list[ListingCandidate]:
        listings: list[ListingCandidate] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            row: dict[str, object] = item
            original_url = self._property_url(row.get("url") or row.get("listingUrl"))
            title = _clean_text(str(row.get("title") or row.get("propertyName") or ""), 220)
            if not original_url or not title:
                continue
            source_id = self._source_id(row, original_url)
            summary = self._summary(row)
            location_text = " ".join(
                str(row.get(key) or "") for key in ("address", "city", "state", "zip", "neighborhoodDescription")
            )
            neighborhood = self._coordinate_neighborhood(row) or GmailHousingAlertSource._neighborhood(
                location_text + " " + title + " " + summary,
                preferences,
            )
            property_type = _clean_text(
                str(row.get("propertyType") or row.get("propertyTypeClass") or "Furnished rental"),
                100,
            )
            price_value = row.get("monthlyPrice") or row.get("monthlyRent") or row.get("price")
            price = _parse_price(str(price_value), require_currency=False)
            if isinstance(price_value, (int, float)):
                price = int(price_value)
            metadata = {
                key: value
                for key, value in {
                    "available_from": row.get("availableOnDate") or row.get("availableFromDate"),
                    "minimum_stay_days": row.get("minimumStayDays"),
                    "furnished_finder_city": row.get("city"),
                    "furnished_finder_state": row.get("state"),
                    "latitude": row.get("latitude"),
                    "longitude": row.get("longitude"),
                }.items()
                if value not in (None, "")
            }
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=source_id,
                    title=title,
                    original_url=original_url,
                    price=price,
                    neighborhood=neighborhood,
                    # The actor's `room` filter is a structured private-room
                    # fact. Spell it out for the source-neutral scorer, rather
                    # than making an otherwise valid room look merely unknown.
                    listing_type=f"Private room · {property_type}",
                    summary=summary or title,
                    metadata={**metadata, "property_type": property_type},
                )
            )
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        token = self.tokens.token
        if not token:
            raise SourceError("The Apify token is missing or invalid; reconnect it from Alerts.")
        budget = preferences.section("budget")
        min_price = int(budget.get("min_monthly", 800))
        max_price = int(budget.get("max_monthly", 2700))
        source_settings = preferences.section("sources")
        try:
            results_limit = int(source_settings.get("furnished_finder_results_per_scan", self.results_limit))
            monthly_listing_limit = int(
                source_settings.get("furnished_finder_monthly_listing_limit", self.monthly_listing_limit)
            )
        except (TypeError, ValueError) as exc:
            raise SourceError("Furnished Finder limits in preferences must be whole numbers.") from exc
        # Keep scheduled work small even if someone hand-edits the advanced file.
        results_limit = max(1, min(results_limit, 10))
        monthly_listing_limit = max(results_limit, min(monthly_listing_limit, 300))
        if not self.tokens.reserve_monthly_furnished_finder_listings(
            results_limit,
            monthly_listing_limit,
        ):
            raise SourceError("The local 300-listing monthly Furnished Finder free-credit cap was reached.")
        payload = {
            "city": "San Francisco",
            "state": "CA",
            "maxItems": results_limit,
            "minPrice": min_price,
            "maxPrice": max_price,
            "propertyType": ["room"],
            "moreDetails": False,
            "includeReviews": False,
        }
        try:
            response = client.post(
                self.actor_url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SourceError("The Furnished Finder helper exceeded its 50-second time limit.") from exc
        if response.status_code in {401, 403}:
            raise SourceError("Apify rejected the token or the Furnished Finder helper is unavailable.")
        if response.status_code in {402, 429}:
            raise SourceError("The Apify free-credit or rate limit was reached; no Furnished Finder listings were imported.")
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError("The Furnished Finder helper returned invalid JSON.") from exc
        if not isinstance(rows, list):
            raise SourceError("The Furnished Finder helper returned an unexpected result format.")
        return self.parse_rows(rows, preferences)

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class CraigslistSource:
    platform = "Craigslist"
    mode = "automatic"
    room_search_url = "https://sfbay.craigslist.org/search/sfc/roo"
    sublet_search_url = "https://sfbay.craigslist.org/search/sfc/sub"
    unit_search_url = "https://sfbay.craigslist.org/search/sfc/apa"
    two_bedroom_search_url = unit_search_url
    three_bedroom_search_url = unit_search_url
    base_search_url = room_search_url
    manual_reason = None

    def __init__(self) -> None:
        self.search_url = f"{self.base_search_url}?sort=date"
        self.detail_budget = 10

    def _room_url(self, preferences: Preferences) -> str:
        query: dict[str, str | int] = {"sort": "date"}
        budget = preferences.section("budget")
        if budget.get("min_monthly") is not None:
            query["min_price"] = int(budget["min_monthly"])
        if budget.get("max_monthly") is not None:
            query["max_price"] = int(budget["max_monthly"])
        return f"{self.room_search_url}?{urlencode(query)}"

    def _unit_url(self, preferences: Preferences) -> str:
        settings = preferences.section("whole_unit")
        query: dict[str, str | int] = {
            "sort": "date",
            "max_price": int(settings.get("max_monthly", 3000)),
            "min_bedrooms": 0,
            "max_bedrooms": 1,
        }
        return f"{self.unit_search_url}?{urlencode(query)}"

    def _sublet_url(self, preferences: Preferences) -> str:
        """Search Craigslist's dedicated sublet category, then classify by home shape."""
        enabled = set(preferences.deal_profile.enabled_paths)
        ceilings: list[int] = []
        if "private_room" in enabled:
            ceilings.append(int(preferences.section("budget").get("max_monthly", 2700)))
        whole_unit = preferences.section("whole_unit")
        two_bedroom = preferences.section("two_bedroom")
        three_bedroom = preferences.section("three_bedroom")
        if enabled.intersection({"studio", "one_bedroom"}):
            ceilings.append(int(whole_unit.get("max_monthly", 3000)))
        if "two_bedroom" in enabled:
            ceilings.append(int(two_bedroom.get("occupants", 2)) * int(two_bedroom.get("max_per_person", 2700)))
        if "three_bedroom" in enabled:
            ceilings.append(int(three_bedroom.get("occupants", 3)) * int(three_bedroom.get("max_per_person", 2500)))
        maximum = max(ceilings or [3000])
        query: dict[str, str | int] = {"sort": "date", "max_price": maximum}
        budget = preferences.section("budget")
        if budget.get("min_monthly") is not None:
            query["min_price"] = int(budget["min_monthly"])
        return f"{self.sublet_search_url}?{urlencode(query)}"

    def _two_bedroom_url(self, preferences: Preferences) -> str:
        """Build an exact 2-bedroom search with its own hard cap."""
        two_bedroom = preferences.section("two_bedroom")
        two_bedroom_max = int(two_bedroom.get("occupants", 2)) * int(
            two_bedroom.get("max_per_person", 2700)
        )
        query: dict[str, str | int] = {
            "sort": "date",
            "max_price": two_bedroom_max,
            "min_bedrooms": 2,
            "max_bedrooms": 2,
        }
        return f"{self.two_bedroom_search_url}?{urlencode(query)}"

    def _three_bedroom_url(self, preferences: Preferences) -> str:
        """Return exact 3-bedroom inventory so 2-bedroom volume cannot hide it."""
        settings = preferences.section("three_bedroom")
        max_monthly = int(settings.get("occupants", 3)) * int(
            settings.get("max_per_person", 2500)
        )
        query: dict[str, str | int] = {
            "sort": "date",
            "max_price": max_monthly,
            "min_bedrooms": 3,
            "max_bedrooms": 3,
        }
        return f"{self.three_bedroom_search_url}?{urlencode(query)}"

    @staticmethod
    def _parse_search(
        response: httpx.Response,
        *,
        max_results: int,
        search_kind: str,
    ) -> list[ListingCandidate]:
        soup = BeautifulSoup(response.text, "html.parser")
        result_nodes = soup.select("li.cl-static-search-result")
        if not result_nodes:
            raise SourceError("Craigslist returned no recognizable result cards; its page format may have changed.")

        listings: list[ListingCandidate] = []
        for node in result_nodes[:max_results]:
            anchor = node.select_one("a[href]")
            title_node = node.select_one(".title")
            if not anchor or not title_node:
                continue
            original_url = urljoin(str(response.url), anchor.get("href", ""))
            title = _clean_text(title_node.get_text(" ", strip=True))
            location_node = node.select_one(".location")
            price_node = node.select_one(".price")
            if not original_url or not title:
                continue
            listings.append(
                ListingCandidate(
                    platform="Craigslist",
                    source_id=_source_id(original_url),
                    title=title,
                    original_url=original_url,
                    price=_parse_price(price_node.get_text(" ", strip=True) if price_node else None),
                    neighborhood=_clean_text(location_node.get_text(" ", strip=True)) if location_node else None,
                    listing_type=(
                        "Room/share"
                        if search_kind == ROOM
                        else "Sublet / temporary rental"
                        if search_kind == "sublet"
                        else "Apartment rental"
                    ),
                    summary=title,
                    metadata={"craigslist_search_kind": search_kind},
                    housing_kind=ROOM if search_kind == ROOM else UNKNOWN if search_kind == "sublet" else WHOLE_UNIT,
                )
            )
        if not listings:
            raise SourceError("Craigslist result cards were present but none could be parsed.")
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        source_settings = preferences.section("sources")
        enabled = set(preferences.deal_profile.enabled_paths)
        room_enabled = "private_room" in enabled
        unit_enabled = bool(enabled.intersection({"studio", "one_bedroom"}))
        two_enabled = "two_bedroom" in enabled
        three_enabled = "three_bedroom" in enabled
        room_details = int(source_settings.get("craigslist_detail_pages_per_scan", 10))
        unit_details = int(source_settings.get("craigslist_unit_detail_pages_per_scan", 10))
        two_bedroom_details = int(source_settings.get("craigslist_two_bedroom_detail_pages_per_scan", 10))
        three_bedroom_details = int(
            source_settings.get("craigslist_three_bedroom_detail_pages_per_scan", 25)
        )
        sublet_details = int(source_settings.get("craigslist_sublet_detail_pages_per_scan", 15))
        room_details = max(0, min(room_details, 50))
        unit_details = max(0, min(unit_details, 50))
        two_bedroom_details = max(0, min(two_bedroom_details, 50))
        three_bedroom_details = max(0, min(three_bedroom_details, 50))
        sublet_details = max(0, min(sublet_details, 50))
        room_details = room_details if room_enabled else 0
        unit_details = unit_details if unit_enabled else 0
        two_bedroom_details = two_bedroom_details if two_enabled else 0
        three_bedroom_details = three_bedroom_details if three_enabled else 0
        self.detail_budget = (
            room_details + unit_details + two_bedroom_details + three_bedroom_details + sublet_details
        )
        max_results = int(source_settings.get("max_results_per_source", 120))
        room_url = self._room_url(preferences)
        unit_url = self._unit_url(preferences)
        two_bedroom_url = self._two_bedroom_url(preferences)
        three_bedroom_url = self._three_bedroom_url(preferences)
        sublet_url = self._sublet_url(preferences)
        search_requests = []
        if three_enabled:
            search_requests.append(("three_bedroom", three_bedroom_url))
        if two_enabled:
            search_requests.append(("two_bedroom", two_bedroom_url))
        if unit_enabled:
            search_requests.append(("whole_unit", unit_url))
        # The dedicated sublet feed can contain any enabled home shape.
        search_requests.append(("sublet", sublet_url))
        if room_enabled:
            search_requests.append(("room", room_url))
        self.search_url = search_requests[0][1]
        parsed: dict[str, list[ListingCandidate]] = {
            "three_bedroom": [],
            "two_bedroom": [],
            "whole_unit": [],
            "sublet": [],
            "room": [],
        }
        errors: list[str] = []
        for search_kind, url in search_requests:
            try:
                response = client.get(url)
                response.raise_for_status()
                parsed[search_kind] = self._parse_search(
                    response,
                    max_results=max_results,
                    search_kind=search_kind,
                )
            except Exception as exc:
                errors.append(f"{search_kind}: {exc}")
        three_bedroom_listings = parsed["three_bedroom"]
        two_bedroom_listings = parsed["two_bedroom"]
        unit_listings = parsed["whole_unit"]
        sublet_listings = parsed["sublet"]
        room_listings = parsed["room"]
        if not three_bedroom_listings and not two_bedroom_listings and not unit_listings and not sublet_listings and not room_listings:
            raise SourceError("All Craigslist searches failed: " + "; ".join(errors))
        target_phrases = [
            phrase
            for area in (
                preferences.list_value("ideal_neighborhoods")
                + preferences.list_value("preferred_neighborhoods")
                + preferences.list_value("acceptable_neighborhoods")
            )
            for phrase in (area, *preferences.mapping_value("neighborhood_aliases").get(area, []))
            if _normal_text(phrase)
        ]

        def target_area_first(listing: ListingCandidate) -> int:
            location = _normal_text(listing.neighborhood or "")
            return 0 if any(
                re.search(rf"(?<!\w){re.escape(_normal_text(phrase))}(?!\w)", location)
                for phrase in target_phrases
            ) else 1

        # Detail requests are the scarce part of a scan. Preserve Craigslist's
        # newest-first order within each group, but enrich target-area cards
        # before generic or out-of-area cards.
        three_bedroom_listings.sort(key=target_area_first)
        two_bedroom_listings.sort(key=target_area_first)
        unit_listings.sort(key=target_area_first)
        sublet_listings.sort(key=target_area_first)
        room_listings.sort(key=target_area_first)
        def interleave(groups: tuple[list[ListingCandidate], ...]) -> list[ListingCandidate]:
            combined: list[ListingCandidate] = []
            for index in range(max((len(group) for group in groups), default=0)):
                for group in groups:
                    if index < len(group):
                        combined.append(group[index])
            return combined

        # Put each mode's configured detail quota first. This makes the knobs
        # real rather than merely summing them while a three-way interleave
        # accidentally allocates equal shares. Remaining cards are still
        # imported afterward with their thinner search-card facts.
        selected_groups = (
            three_bedroom_listings[:three_bedroom_details],
            two_bedroom_listings[:two_bedroom_details],
            unit_listings[:unit_details],
            sublet_listings[:sublet_details],
            room_listings[:room_details],
        )
        remaining_groups = (
            three_bedroom_listings[three_bedroom_details:],
            two_bedroom_listings[two_bedroom_details:],
            unit_listings[unit_details:],
            sublet_listings[sublet_details:],
            room_listings[room_details:],
        )
        listings = interleave(selected_groups) + interleave(remaining_groups)
        deduplicated: dict[str, ListingCandidate] = {}
        for listing in listings:
            deduplicated.setdefault(listing.original_url, listing)
        return list(deduplicated.values())

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        response = client.get(listing.original_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        metadata = dict(listing.metadata)
        if soup.select_one(".removed") or _CRAIGSLIST_REMOVED_PATTERN.search(response.text):
            metadata.update(
                {
                    "craigslist_detail_checked": True,
                    "verified_inactive": True,
                    "verification_concern": "Verified inactive: Craigslist has removed or flagged this post.",
                }
            )
            return replace(listing, metadata=metadata, original_url=str(response.url))
        posted = soup.select_one("time.date[datetime], time[datetime]")
        if posted is not None:
            stamp = _clean_text(posted.get("datetime"), 40)
            if stamp:
                metadata["listing_timestamp"] = stamp
        description_meta = soup.select_one('meta[name="description"]')
        body = soup.select_one("#postingbody")
        body_text = _clean_text(body.get_text(" ", strip=True) if body else "")
        summary = _clean_text(
            body_text or description_meta.get("content", "") if description_meta else body_text,
            420,
        )
        attributes = [_clean_text(node.get_text(" ", strip=True)) for node in soup.select(".attrgroup .attr")]
        housing = soup.select_one(".postingtitle .housing")
        metadata["craigslist_detail_checked"] = True
        if attributes:
            metadata["attributes"] = " | ".join(attributes)
        attributes_text = " ".join(attributes)

        # Do not promote detail pages whose source facts contradict one another
        # or whose content proves it belongs to another metro.  This stays
        # intentionally narrow: a clean but sparse post remains reviewable;
        # only concrete conflicts are rejected.
        published_bedrooms = _craigslist_bedroom_count(housing.get_text(" ", strip=True) if housing else None)
        described_bedrooms = _craigslist_bedroom_count(" ".join(filter(None, [listing.title, body_text])))
        if (
            published_bedrooms is not None
            and described_bedrooms is not None
            and published_bedrooms != described_bedrooms
        ):
            metadata.update(
                {
                    "craigslist_content_rejected": True,
                    "verification_concern": (
                        "Rejected: the Craigslist bedroom count conflicts with the title or detail text."
                    ),
                }
            )
        elif _CRAIGSLIST_OFF_MARKET_LOCATION_PATTERN.search(body_text):
            metadata.update(
                {
                    "craigslist_content_rejected": True,
                    "verification_concern": (
                        "Rejected: the detail text references a location or transit system outside San Francisco."
                    ),
                }
            )
        elif _CRAIGSLIST_SUSPECT_PROFILE_PATTERN.search(attributes_text):
            metadata.update(
                {
                    "craigslist_content_rejected": True,
                    "verification_concern": (
                        "Rejected: recurring malformed lister-profile pattern seen in invalid Craigslist posts."
                    ),
                }
            )
        outside_area = declared_outside_sf_area_hint(body_text)
        declared_area = declared_sf_area_hint(body_text)
        resolved_area = outside_area or declared_area
        if resolved_area:
            metadata["detail_declared_neighborhood"] = resolved_area
        return replace(
            listing,
            summary=summary or listing.summary,
            neighborhood=resolved_area or listing.neighborhood,
            listing_type=_clean_text(housing.get_text(" ", strip=True)) if housing else listing.listing_type,
            metadata=metadata,
            original_url=str(response.url),
        )


class SpareRoomSource:
    platform = "SpareRoom"
    mode = "automatic"
    search_url = "https://www.spareroom.com/rooms-for-rent/san_francisco?sort_by=last_updated"
    manual_reason = None
    detail_budget = 0

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        if "private_room" not in preferences.deal_profile.enabled_paths:
            self.empty_result_message = "Skipped because private rooms are not enabled in Your deal."
            return []
        max_results = int(preferences.section("sources").get("max_results_per_source", 120))
        response = client.get(self.search_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        result_nodes = soup.select("li.listing-result")
        if not result_nodes:
            raise SourceError("SpareRoom returned no recognizable result cards; its page format may have changed.")
        listings: list[ListingCandidate] = []
        for node in result_nodes[:max_results]:
            anchor = node.select_one("a.listing-card__link[href]")
            if not anchor:
                continue
            original_url = urljoin(str(response.url), anchor.get("href", ""))
            listing_id = node.get("data-listing-id") or _source_id(original_url)
            title_node = node.select_one(".listing-card__title")
            title = _clean_text(
                title_node.get_text(" ", strip=True) if title_node else node.get("data-listing-title", "")
            )
            if not title or not original_url:
                continue
            description = node.select_one(".listing-card__short_description")
            room_type = node.select_one(".listing-card__room-type")
            metadata = {
                key: value
                for key, value in {
                    "property_type": node.get("data-listing-property-type"),
                    "rooms_in_property": node.get("data-listing-rooms-in-property"),
                    "available_now": node.get("data-listing-available-now"),
                    "advertiser_role": node.get("data-listing-advertiser-role"),
                }.items()
                if value not in (None, "")
            }
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=str(listing_id),
                    title=title,
                    original_url=original_url,
                    price=_parse_price(str(node.get("data-listing-ad-rate-normalised", ""))),
                    neighborhood=_clean_text(str(node.get("data-listing-neighbourhood", ""))) or None,
                    listing_type=_clean_text(room_type.get_text(" ", strip=True)) if room_type else "Room/share",
                    summary=_clean_text(description.get_text(" ", strip=True), 420) if description else title,
                    metadata=metadata,
                )
            )
        if not listings:
            raise SourceError("SpareRoom result cards were present but none could be parsed.")
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class ManualSource:
    mode = "manual"
    detail_budget = 0

    def __init__(self, platform: str, search_url: str, reason: str):
        self.platform = platform
        self.search_url = search_url
        self.manual_reason = reason

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return []

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


def default_sources(
    mailbox: GmailAlertMailbox | None = None,
    apify_tokens: ApifyTokenStore | None = None,
) -> list[ListingSource]:
    blocked_reason = "Public pages reject unattended requests (HTTP 403); use the direct search link."
    # Named rather than indexed: the dashboard's ordering below interleaves these
    # with the setup-required sources, and positional slicing made adding a
    # source silently reorder the page.
    craigslist = CraigslistSource()
    listings_project = ListingsProjectSource()
    abacus = AbacusSource()
    spareroom = SpareRoomSource()
    sf_portal = SFHousingPortalSource()
    zumper = ZumperSource()
    apartment_list = ApartmentListSource()
    free_sources: list[ListingSource] = [craigslist, listings_project, abacus]
    manual_sources: list[ListingSource] = [
        ManualSource("HotPads", "https://hotpads.com/san-francisco-ca/apartments-for-rent", blocked_reason),
        ManualSource("Apartments.com", "https://www.apartments.com/san-francisco-ca/", blocked_reason),
        ManualSource("Roomies", "https://www.roomies.com/rooms/san-francisco-ca", blocked_reason),
    ]
    if mailbox:
        # Preserve the dashboard's requested source ordering.
        facebook: ListingSource = (
            FacebookMarketplaceSource(mailbox, apify_tokens)
            if apify_tokens is not None
            else FacebookMarketplaceAlertSource(mailbox)
        )
        return [
            facebook,
            FacebookGroupsSource(apify_tokens) if apify_tokens is not None else ManualSource(
                "Facebook Groups",
                "https://www.facebook.com/groups/",
                "Connect Apify before monitoring a configured public Facebook group.",
            ),
            *free_sources,
            FurnishedFinderSource(apify_tokens) if apify_tokens is not None else ManualSource(
                "Furnished Finder",
                FurnishedFinderSource.search_url,
                "Connect Apify before checking Furnished Finder's blocked public search.",
            ),
            ZillowAlertSource(mailbox),
            HotPadsAlertSource(mailbox),
            ApartmentsComAlertSource(mailbox),
            spareroom,
            sf_portal,
            apartment_list,
            zumper,
            RoomiesAlertSource(mailbox),
        ]
    return [
        ManualSource(
            "Facebook Marketplace",
            "https://www.facebook.com/marketplace/you/alerts/",
            "Marketplace saved-search alert email setup is not connected yet.",
        ),
        ManualSource(
            "Facebook Groups",
            "https://www.facebook.com/groups/",
            "Connect Apify before monitoring a configured public Facebook group.",
        ),
        *free_sources,
        ManualSource(
            "Furnished Finder",
            FurnishedFinderSource.search_url,
            "Connect Apify before checking Furnished Finder's blocked public search.",
        ),
        ManualSource("Zillow", "https://www.zillow.com/myzillow/savedsearches/", "Zillow alert email setup is not connected yet."),
        *manual_sources[:1],
        spareroom,
        sf_portal,
        apartment_list,
        zumper,
        *manual_sources[1:],
    ]
