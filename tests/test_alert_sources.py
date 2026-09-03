from __future__ import annotations

from sf_housing.gmail_alerts import AlertEmail
from sf_housing.sources import (
    ApartmentsComAlertSource,
    FacebookMarketplaceAlertSource,
    HotPadsAlertSource,
    RoomiesAlertSource,
    ZumperAlertSource,
    ZillowAlertSource,
)


class FakeMailbox:
    is_connected = True

    def __init__(self, emails: list[AlertEmail]):
        self.emails = emails
        self.queries: list[str] = []

    def messages(self, query: str, max_results: int = 100) -> list[AlertEmail]:
        self.queries.append(query)
        return self.emails


def test_zillow_alert_email_becomes_a_scored_candidate(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="zillow-1",
                subject="New listing for your NOPA search",
                html=(
                    '<table><tr><td><a href="https://www.zillow.com/homedetails/'
                    '123-NOPA-San-Francisco-CA-94107/123_zpid/">'
                    '123 NOPA, San Francisco</a><p>$1,500/mo · Sunny rental</p></td></tr></table>'
                ),
                text="",
            )
        ]
    )

    listings = ZillowAlertSource(mailbox).search(None, preferences)

    assert len(listings) == 1
    assert listings[0].platform == "Zillow"
    assert listings[0].price == 1500
    assert listings[0].neighborhood == "NOPA"
    assert listings[0].original_url.startswith("https://www.zillow.com/homedetails/")
    assert mailbox.queries


def test_zillow_alert_accepts_a_proofpoint_protected_card_link(preferences) -> None:
    first = AlertEmail(
        message_id="zillow-proofpoint-1",
        subject="123 NOPA Ave just listed in 'San Francisco rentals'",
        html=(
            '<a href="https://urldefense.com/v3/__https://click.mail.zillow.com/f/a/status-card**A">'
            '● For rent New</a>'
            '<a href="https://urldefense.com/v3/__https://click.mail.zillow.com/f/a/first-card**A">'
            '$2,400/mo · 1 bd | 1 ba · 123 NOPA Ave, San Francisco, CA</a>'
            '<a href="https://urldefense.com/v3/__https://click.mail.zillow.com/f/a/other-rental**A">'
            '$2,700/mo · 1 bd | 1 ba · 400 Elsewhere St, San Francisco, CA</a>'
            '<a href="https://urldefense.com/v3/__https://click.mail.zillow.com/f/a/learn-more**A">Learn more</a>'
        ),
        text="",
    )
    repeated = AlertEmail(
        message_id="zillow-proofpoint-2",
        subject="123 NOPA Ave just listed in 'San Francisco rentals'",
        html=(
            '<a href="https://urldefense.com/v3/__https://click.mail.zillow.com/f/a/repeated-card**A">'
            '$2,500/mo · 1 bd | 1 ba · 123 NOPA Ave, San Francisco, CA</a>'
        ),
        text="",
    )

    listings = ZillowAlertSource(FakeMailbox([first, repeated])).search(None, preferences)

    assert len(listings) == 1
    assert listings[0].price == 2400
    assert listings[0].neighborhood == "NOPA"
    assert "urldefense.com" in listings[0].original_url


def test_facebook_alert_requires_a_direct_marketplace_listing_link(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="facebook-1",
                subject="Marketplace saved search update",
                html=(
                    '<a href="https://www.facebook.com/marketplace/item/123456789/">'
                    'Sunny private room in NOPA</a><p>$1,550 per month</p>'
                    '<a href="https://www.facebook.com/marketplace/you/alerts/">Manage alerts</a>'
                ),
                text="",
            )
        ]
    )

    listings = FacebookMarketplaceAlertSource(mailbox).search(None, preferences)

    assert len(listings) == 1
    assert listings[0].platform == "Facebook Marketplace"
    assert listings[0].price == 1550
    assert listings[0].neighborhood == "NOPA"
    assert listings[0].original_url.endswith("/123456789/")


def test_alert_address_number_is_not_mistaken_for_rent(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="zillow-no-price",
                subject="New listing",
                html=(
                    '<a href="https://www.zillow.com/homedetails/'
                    '123-Main-St-San-Francisco-CA/987_zpid/">123 Main Street</a>'
                ),
                text="",
            )
        ]
    )

    listing = ZillowAlertSource(mailbox).search(None, preferences)[0]

    assert listing.price is None


def test_facebook_tracking_wrapper_becomes_a_stable_direct_link(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="facebook-wrapped",
                subject="Marketplace update",
                html=(
                    '<a href="https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com%2F'
                    'marketplace%2Fitem%2F123456789%2F%3Fref%3Demail&amp;h=tracking">'
                    'Private room</a><p>$1,500</p>'
                ),
                text="",
            )
        ]
    )

    listing = FacebookMarketplaceAlertSource(mailbox).search(None, preferences)[0]

    assert listing.original_url == "https://www.facebook.com/marketplace/item/123456789/"
    assert listing.source_id == "123456789"


def test_hotpads_alert_keeps_a_direct_listing_link_and_unique_identifier(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="hotpads-1",
                subject="New rental matches your search",
                html=(
                    '<a href="https://hotpads.com/sunny-victorian-near-dolores-park-san-francisco-ca/pad">'
                    'Sunny private room near Dolores Park</a><p>$1,650 / month · Victorian</p>'
                ),
                text="",
            )
        ]
    )

    listing = HotPadsAlertSource(mailbox).search(None, preferences)[0]

    assert listing.platform == "HotPads"
    assert listing.price == 1650
    assert listing.original_url.endswith("/pad")
    assert listing.source_id != "pad"
    assert mailbox.queries == ["from:(hotpads.com) newer_than:90d"]


def test_roomies_alert_rejects_search_links_and_imports_numeric_listing(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="roomies-1",
                subject="New Roomies listings",
                html=(
                    '<a href="https://www.roomies.com/rooms/san-francisco-ca">Search results</a>'
                    '<a href="https://www.roomies.com/rooms/1019570">'
                    'Private room in NOPA</a><p>$1,700 per month · House · Sunny</p>'
                ),
                text="",
            )
        ]
    )

    listing = RoomiesAlertSource(mailbox).search(None, preferences)[0]

    assert listing.platform == "Roomies"
    assert listing.price == 1700
    assert listing.neighborhood == "NOPA"
    assert listing.original_url == "https://www.roomies.com/rooms/1019570"
    assert listing.source_id == "1019570"


def test_zumper_alert_imports_only_direct_home_or_building_links(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="zumper-1",
                subject="New rentals match your Zumper alert",
                html=(
                    '<a href="https://www.zumper.com/apartments-for-rent/san-francisco-ca">Search results</a>'
                    '<a href="https://www.zumper.com/address/421-duboce-ave-san-francisco-ca-94117-usa">'
                    '421 Duboce Ave</a><p>$5,400 / month · 2 bedroom apartment in Duboce Triangle</p>'
                ),
                text="",
            )
        ]
    )

    listing = ZumperAlertSource(mailbox).search(None, preferences)[0]

    assert listing.platform == "Zumper"
    assert listing.price == 5400
    assert listing.original_url.startswith("https://www.zumper.com/address/")


def test_apartments_com_alert_requires_a_stable_individual_listing_link(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="apartments-1",
                subject="New Apartments.com saved search matches",
                html=(
                    '<a href="https://www.apartments.com/san-francisco-ca/">Search results</a>'
                    '<a href="https://www.apartments.com/421-duboce-ave-san-francisco-ca-unit-na/e40l74n/">'
                    '421 Duboce Ave Unit na</a><p>$5,400 / month · 2 bedroom condo</p>'
                ),
                text="",
            )
        ]
    )

    listing = ApartmentsComAlertSource(mailbox).search(None, preferences)[0]

    assert listing.platform == "Apartments.com"
    assert listing.price == 5400
    assert listing.original_url.endswith("/e40l74n/")


def test_apartments_com_opaque_alerts_are_reported_without_failing_the_scan(preferences) -> None:
    mailbox = FakeMailbox(
        [
            AlertEmail(
                message_id="apartments-opaque",
                subject="Your saved search update",
                html='<a href="https://click.email.apartments.com/opaque">Open your matches</a>',
                text="",
            )
        ]
    )
    source = ApartmentsComAlertSource(mailbox)

    assert source.search(None, preferences) == []
    assert source.empty_result_message is not None
    assert "No direct listing links" in source.empty_result_message
