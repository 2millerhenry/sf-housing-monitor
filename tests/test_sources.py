from __future__ import annotations

import httpx

from sf_housing.models import ListingCandidate
from sf_housing.preferences import Preferences
from sf_housing.sources import AbacusSource, CraigslistSource, ListingsProjectSource, SpareRoomSource


CRAIGSLIST_HTML = """
<ol class="cl-static-search-results">
  <li class="cl-static-search-result" title="Sunny room">
    <a href="https://www.craigslist.org/view/d/san-francisco-sunny-room/abc123">
      <div class="title">Sunny private room</div>
      <div class="details"><div class="price">$1,650</div><div class="location">Inner Richmond</div></div>
    </a>
  </li>
</ol>
"""

SPAREROOM_HTML = """
<ul class="listing-results">
  <li class="listing-result" data-listing-id="42" data-listing-title="Garden room"
      data-listing-neighbourhood="Mission" data-listing-property-type="house"
      data-listing-rooms-in-property="4" data-listing-ad-rate-normalised="$1,700">
    <article class="listing-card">
      <a class="listing-card__link" href="/rooms-for-rent/san_francisco/mission/42?listing_click=1"></a>
      <h2 class="listing-card__title">Private garden room</h2>
      <p class="listing-card__room-type">1 room - Available now</p>
      <p class="listing-card__short_description">Sunny room in a quiet shared house.</p>
    </article>
  </li>
</ul>
"""

LISTINGS_PROJECT_HTML = """
<main>
  <div class="flex flex-col md:flex-row mb-8">
    <div class="text-grey-dark mb-2 text-smish">Potrero Hill, San Francisco | Rooms for Rent</div>
    <h4><a href="/listings/potrero-room">Sunny Potrero room</a></h4>
    <p>$2,200/month · August 1, 2026 · Sunny two-bedroom home near Potrero Hill.</p>
  </div>
  <div class="flex flex-col md:flex-row mb-8">
    <div class="text-grey-dark mb-2 text-smish">Grand Lake, Oakland | Apartments for Sublet</div>
    <h4><a href="/listings/oakland-sublet">Oakland sublet</a></h4>
    <p>$4,000/month · Two bedrooms.</p>
  </div>
</main>
"""

ABACUS_HTML = """
<div class="listing-item result js-listing-item">
  <div class="sidebar__price">$5,100</div>
  <span class="js-listing-blurb-bed-bath">2 bd / 1 ba</span>
  <h2 class="listing-item__title js-listing-title"><a href="/listings/detail/potrero-flat">Two-bedroom flat</a></h2>
  <span class="js-listing-address">88 Potrero Avenue, San Francisco, CA 94110</span>
  <div class="js-listing-available">NOW</div>
</div>
<div class="listing-item result js-listing-item">
  <div class="sidebar__price">$2,100</div>
  <h2 class="listing-item__title js-listing-title"><a href="/listings/detail/outside-sf">Outside SF</a></h2>
  <span class="js-listing-address">1 Main Street, Daly City, CA 94014</span>
</div>
"""

ABACUS_DETAIL_HTML = """
<main>
  <h1 class="js-show-title">Two-bedroom flat in Potrero Hill</h1>
  <p class="header__summary">2 bd, 1 ba | Available Now</p>
  <p class="description">A sunny two-bedroom flat in Potrero Hill with laundry.</p>
  <a class="js-apply-now" href="/listings/rental_applications/new?listable_uid=potrero-flat">Apply Now</a>
</main>
"""


def client_for(body: str) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))


def test_craigslist_public_result_parser(preferences: Preferences) -> None:
    source = CraigslistSource()
    with client_for(CRAIGSLIST_HTML) as client:
        listings = source.search(client, preferences)
    assert len(listings) == 1
    assert listings[0].price == 1650
    assert listings[0].neighborhood == "Inner Richmond"
    assert listings[0].source_id == "abc123"


def test_spareroom_public_result_parser(preferences: Preferences) -> None:
    source = SpareRoomSource()
    with client_for(SPAREROOM_HTML) as client:
        listings = source.search(client, preferences)
    assert len(listings) == 1
    assert listings[0].price == 1700
    assert listings[0].source_id == "42"
    assert listings[0].metadata["property_type"] == "house"
    assert "quiet shared house" in listings[0].summary


def test_listings_project_keeps_only_explicit_sf_residential_cards(preferences: Preferences) -> None:
    with client_for(LISTINGS_PROJECT_HTML) as client:
        listings = ListingsProjectSource().search(client, preferences)

    assert len(listings) == 1
    listing = listings[0]
    assert listing.platform == "Listings Project"
    assert listing.source_id == "potrero-room"
    assert listing.price == 2200
    assert listing.neighborhood == "Potrero Hill"
    assert listing.listing_type == "Rooms for Rent"
    assert listing.metadata["direct_lister"] is True


def test_listings_project_reports_a_real_zero_when_only_outside_or_seeking_cards_exist(
    preferences: Preferences,
) -> None:
    html = """
    <div class="flex flex-col md:flex-row mb-8">
      <div class="md:ml-8 flex-auto"><p>Oakland | Apartments for Sublet</p>
      <h4><a href="/listings/oakland-flat">Oakland flat</a></h4></div>
    </div>
    <div class="flex flex-col md:flex-row mb-8">
      <div class="md:ml-8 flex-auto"><p>SF | Seeking Living Space</p>
      <h4><a href="/listings/seeking">Seeking a home</a></h4></div>
    </div>
    """
    source = ListingsProjectSource()
    with client_for(html) as client:
        listings = source.search(client, preferences)

    assert listings == []
    assert "no explicit San Francisco rentals" in source.empty_result_message


def test_abacus_keeps_sf_cards_and_exposes_the_direct_application_link(preferences: Preferences) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/potrero-flat"):
            return httpx.Response(200, text=ABACUS_DETAIL_HTML)
        return httpx.Response(200, text=ABACUS_HTML)

    source = AbacusSource()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        listings = source.search(client, preferences)
        enriched = source.enrich(client, listings[0])

    assert len(listings) == 1
    assert listings[0].price == 5100
    assert listings[0].listing_type == "2 bd / 1 ba"
    assert listings[0].neighborhood is None
    assert enriched.neighborhood == "Potrero Hill"
    assert enriched.metadata["application_url"].endswith("listable_uid=potrero-flat")


def test_abacus_distinguishes_zero_inventory_from_parser_breakage(preferences: Preferences) -> None:
    html = """
    <div class="listings js-listings-container">
      <div class="no_vacancies listings__no-vacancies">We currently have no available properties for rent.</div>
    </div>
    """
    source = AbacusSource()
    with client_for(html) as client:
        listings = source.search(client, preferences)

    assert listings == []
    assert source.empty_result_message == "Abacus currently reports no available rental properties."


def test_craigslist_searches_rooms_small_units_and_two_to_three_bedrooms_with_separate_budgets(preferences: Preferences) -> None:
    apartment_html = """
    <ol>
    <li class="cl-static-search-result">
      <a href="https://sfbay.craigslist.org/sfc/apa/d/oakland-outside-studio/111111.html">
        <div class="title">Outside studio apartment</div>
        <div class="price">$2,000</div><div class="location">Oakland</div>
      </a>
    </li>
    <li class="cl-static-search-result">
      <a href="https://sfbay.craigslist.org/sfc/apa/d/san-francisco-sunny-studio/987654.html">
        <div class="title">Sunny studio apartment</div>
        <div class="price">$2,550</div><div class="location">NOPA</div>
      </a>
    </li></ol>
    """
    two_bedroom_html = """
    <ol><li class="cl-static-search-result">
      <a href="https://sfbay.craigslist.org/sfc/apa/d/san-francisco-two-bedroom-nopa/222222.html">
        <div class="title">2 bedroom apartment near Alamo Square</div>
        <div class="price">$5,200</div><div class="location">NOPA</div>
      </a>
    </li></ol>
    """
    three_bedroom_html = """
    <ol><li class="cl-static-search-result">
      <a href="https://sfbay.craigslist.org/sfc/apa/d/san-francisco-three-bedroom-hayes/333333.html">
        <div class="title">3 bedroom flat in Hayes Valley</div>
        <div class="price">$7,200</div><div class="location">Hayes Valley</div>
      </a>
    </li></ol>
    """
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.path.endswith("/apa") and request.url.params.get("min_bedrooms") == "3":
            return httpx.Response(200, text=three_bedroom_html)
        if request.url.path.endswith("/apa") and request.url.params.get("min_bedrooms") == "2":
            return httpx.Response(200, text=two_bedroom_html)
        return httpx.Response(200, text=apartment_html if request.url.path.endswith("/apa") else CRAIGSLIST_HTML)

    source = CraigslistSource()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        listings = source.search(client, preferences)

    studio = next(item for item in listings if item.source_id == "987654.html")
    two_bedroom = next(item for item in listings if item.source_id == "222222.html")
    three_bedroom = next(item for item in listings if item.source_id == "333333.html")
    small_unit_listings = [
        item for item in listings if item.metadata.get("craigslist_search_kind") == "whole_unit"
    ]
    assert studio.housing_kind == "whole_unit"
    assert studio.title == "Sunny studio apartment"
    assert two_bedroom.metadata["craigslist_search_kind"] == "two_bedroom"
    assert three_bedroom.metadata["craigslist_search_kind"] == "three_bedroom"
    assert small_unit_listings[0].source_id == "987654.html"
    assert any("/search/sfc/apa" in url and "max_price=3000" in url for url in seen_urls)
    assert any(
        "/search/sfc/apa" in url
        and "max_price=5400" in url
        and "min_bedrooms=2" in url
        and "max_bedrooms=2" in url
        for url in seen_urls
    )
    assert any(
        "/search/sfc/apa" in url
        and "max_price=7500" in url
        and "min_bedrooms=3" in url
        and "max_bedrooms=3" in url
        for url in seen_urls
    )
    assert any("/search/sfc/roo" in url and "max_price=2000" in url for url in seen_urls)
    # The test profile disables room details but leaves both apartment-mode
    # defaults active, so both broad apartment workflows receive ten detail
    # slots and exact 3-bedroom inventory receives its own 25-slot budget.
    # The dedicated sublet category receives 15 detail slots so its six-month
    # term and actual home shape can be verified.
    assert source.detail_budget == 60
    assert any("/search/sfc/sub" in url and "max_price=7500" in url for url in seen_urls)


def test_craigslist_sublet_cards_stay_unclassified_until_the_detail_page() -> None:
    sublet_html = """
    <ol><li class="cl-static-search-result">
      <a href="https://sfbay.craigslist.org/sfc/sub/d/san-francisco-six-month-sublet/444444.html">
        <div class="title">6-month 1BR sublet in Noe Valley</div>
        <div class="price">$2,300</div><div class="location">Noe Valley</div>
      </a>
    </li></ol>
    """
    response = httpx.Response(200, text=sublet_html, request=httpx.Request("GET", "https://sfbay.craigslist.org/search/sfc/sub"))

    listings = CraigslistSource._parse_search(response, max_results=10, search_kind="sublet")

    assert len(listings) == 1
    assert listings[0].listing_type == "Sublet / temporary rental"
    assert listings[0].housing_kind == "unknown"
    assert listings[0].metadata["craigslist_search_kind"] == "sublet"


def test_craigslist_detail_body_overrides_a_vague_meta_description_and_conflicting_card_area() -> None:
    detail_html = """
    <html><head><meta name="description" content="Beautiful studio for rent"></head>
    <body>
      <section id="postingbody">
        Large studio available for rent in San Francisco Excelsior/Portola District.
        Private entrance and private bathroom. No roommates.
      </section>
      <span class="postingtitle"><span class="housing">1BR / 1Ba</span></span>
    </body></html>
    """
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="conflicting-area",
        title="Beautiful studio for rent",
        original_url="https://example.test/conflicting-area",
        price=975,
        neighborhood="Potrero Hill",
        housing_kind="whole_unit",
    )

    with client_for(detail_html) as client:
        enriched = CraigslistSource().enrich(client, listing)

    assert enriched.summary.startswith("Large studio available for rent")
    assert enriched.neighborhood == "Excelsior"
    assert enriched.metadata["detail_declared_neighborhood"] == "Excelsior"


def test_craigslist_detail_body_rejects_an_explicit_outside_sf_address() -> None:
    detail_html = """
    <html><body>
      <section id="postingbody">
        396 Pine Hill Rd, Mill Valley, CA 94941. A spacious two-bedroom apartment.
      </section>
      <span class="postingtitle"><span class="housing">2BR / 1Ba</span></span>
    </body></html>
    """
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="outside-sf",
        title="2 bedroom apartment",
        original_url="https://example.test/outside-sf",
        price=3645,
        neighborhood="NOPA",
        housing_kind="whole_unit",
    )

    with client_for(detail_html) as client:
        enriched = CraigslistSource().enrich(client, listing)

    assert enriched.neighborhood == "Mill Valley (outside SF)"
    assert enriched.metadata["detail_declared_neighborhood"] == "Mill Valley (outside SF)"


def test_craigslist_detail_marks_a_removed_post_inactive() -> None:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="removed-post",
        title="Studio apartment",
        original_url="https://example.test/removed-post",
        price=1900,
        neighborhood="Hayes Valley",
        housing_kind="whole_unit",
    )

    with client_for("<html><h2>This posting has been flagged for removal.</h2></html>") as client:
        enriched = CraigslistSource().enrich(client, listing)

    assert enriched.metadata["craigslist_detail_checked"] is True
    assert enriched.metadata["verified_inactive"] is True


def test_craigslist_detail_rejects_conflicting_or_out_of_market_copy() -> None:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="conflicted-post",
        title="Clean One-Bedroom Apartment",
        original_url="https://example.test/conflicted-post",
        price=1820,
        neighborhood="Potrero Hill",
        housing_kind="whole_unit",
    )
    detail_html = """
    <html><body>
      <section id="postingbody">
        One bedroom available. Habitación privada cerca de la línea Roja y Metra en Rogers Park.
      </section>
      <span class="postingtitle"><span class="housing">2BR / 1Ba</span></span>
    </body></html>
    """

    with client_for(detail_html) as client:
        enriched = CraigslistSource().enrich(client, listing)

    assert enriched.metadata["craigslist_detail_checked"] is True
    assert enriched.metadata["craigslist_content_rejected"] is True
    assert "conflicts" in enriched.metadata["verification_concern"]


def test_craigslist_detail_page_records_the_posting_time() -> None:
    """The search cards carry no date, so a listing's real age comes from here."""
    from sf_housing.sources import CraigslistSource
    from sf_housing.models import ListingCandidate

    class Response:
        # A real response always carries a status; a double without one hid the
        # fact that a deleted post answers 410 rather than raising.
        status_code = 200
        url = "https://www.craigslist.org/view/d/room/1.html"
        text = (
            '<html><body><section id="postingbody">A sunny room near the park.</section>'
            '<time class="date timeago" datetime="2026-08-28T14:11:54-0700">Aug 28</time>'
            "</body></html>"
        )

        def raise_for_status(self):
            return None

    class Client:
        def get(self, url, **kwargs):
            return Response()

    listing = ListingCandidate(
        platform="Craigslist",
        source_id="1",
        title="Sunny room",
        original_url=Response.url,
    )

    enriched = CraigslistSource().enrich(Client(), listing)

    assert enriched.metadata["listing_timestamp"] == "2026-08-28T14:11:54-0700"
    # It must be readable by the first-run window and by the database writer.
    from datetime import datetime

    datetime.fromisoformat(enriched.metadata["listing_timestamp"])


def test_the_app_has_one_idea_of_what_it_calls_itself() -> None:
    """The scanner kept its own copy of the request headers, and a checker
    written to verify the sources kept a third. Zumper answers this app
    honestly and serves a browser string a bot challenge, so the copy that
    drifted reported a working source as broken -- a checker asking a different
    question than the app is worse than no checker at all."""
    from pathlib import Path

    from sf_housing.sources import MONITOR_HEADERS

    root = Path(__file__).resolve().parents[1]
    assert "SFHousingMonitor" in MONITOR_HEADERS["User-Agent"]

    # Imported is not the same as used: the checker passed its client no
    # headers at all for a while and still mentioned the name.
    for owner, usage in (
        ("sf_housing/scanner.py", "headers = dict(MONITOR_HEADERS)"),
        ("scripts/check_sources.py", "headers=dict(MONITOR_HEADERS)"),
    ):
        body = (root / owner).read_text(encoding="utf-8")
        assert usage in body, f"{owner} does not read with the shared headers"
        assert "SFHousingMonitor/" not in body, f"{owner} still spells out its own user agent"


def test_the_source_check_asks_about_a_fresh_download() -> None:
    """It exists to answer "would somebody downloading this today get
    listings". Handed a mailbox it would check sources a new person does not
    have, and a narrow deal would report a working source as empty."""
    from pathlib import Path

    body = (Path(__file__).resolve().parents[1] / "scripts/check_sources.py").read_text(
        encoding="utf-8"
    )

    assert "default_sources()" in body, "the check must run the no-account source set"
    assert 'mode == "automatic"' in body
    assert "max_results_per_source" in body, "a per-source cap would truncate the answer"


def test_a_site_declining_today_is_not_reported_as_a_broken_reader() -> None:
    """ApartmentGuide answered with 488 homes and then, twenty minutes later,
    with HTTP 202. Both times the reader was fine; the second time the site had
    simply had enough of being asked. A check that calls that broken teaches
    you to distrust the check, and it is the thing this app is built to
    survive rather than a thing to fix."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from check_sources import THROTTLE_MARKERS, check

    from sf_housing.sources import SourceError

    class Declining:
        platform = "ApartmentGuide"

        def search(self, client, preferences):
            raise SourceError(
                "ApartmentGuide answered HTTP 202 rather than a page of results, "
                "which is how it turns away an unattended request."
            )

    class Broken:
        platform = "Somewhere"

        def search(self, client, preferences):
            raise KeyError("listResults")

    assert any("202" in marker for marker in THROTTLE_MARKERS)
    assert check(Declining(), None)[0] == "THROTTLED"
    assert check(Broken(), None)[0] == "FAILING"


def test_only_a_broken_reader_fails_the_check() -> None:
    """It is meant to gate a release. Failing it every time a site throttles
    would make it noise, and the throttling is usually the check's own doing."""
    import sys
    from pathlib import Path

    body = (Path(__file__).resolve().parents[1] / "scripts/check_sources.py").read_text(
        encoding="utf-8"
    )

    assert "return 1 if failing else 0" in body
    assert "THROTTLED" in body
