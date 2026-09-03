from __future__ import annotations

from sf_housing.classification import ROOM, UNKNOWN, classify_listing
from sf_housing.models import ListingCandidate
from sf_housing.preferences import Preferences, load_preferences
from sf_housing.scoring import score_listing
from tests.paths import BENCHMARK_PROFILE


def test_strong_known_match_scores_high_and_returns_three_reasons(preferences: Preferences) -> None:
    listing = ListingCandidate(
        platform="Test",
        source_id="perfect",
        title="Sunny private room in quiet Victorian house near Golden Gate Park",
        original_url="https://example.test/perfect",
        price=1550,
        neighborhood="NOPA",
        listing_type="Private room",
        summary="Flexible lease in a communal home with a garden and bright natural light.",
        metadata={"property_type": "house", "rooms_in_property": "4"},
    )

    result = score_listing(listing, preferences)

    assert result.score >= 85
    assert len(result.reasons) == 3
    assert result.concern == "No major concern found in the available details."
    assert result.details["home_facts"]["primary"] == "Private room · House"


def test_whole_unit_studio_is_scored_in_a_separate_search_and_labeled_clearly() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="studio",
        title="Sunny studio apartment in Potrero Hill",
        original_url="https://example.test/studio",
        price=2450,
        neighborhood="Potrero Hill",
        summary="Quiet studio apartment with laundry and a patio.",
    )

    result = score_listing(listing, profile)

    assert result.score >= profile.minimum_score
    assert result.details["housing_kind"] == "whole_unit"
    assert result.details["home_facts"]["primary"] == "Studio"
    # A bathroom count joins these facts, and says n/a when nothing states one.
    assert "Building size not stated" in result.details["home_facts"]["secondary"]
    assert "Baths n/a" in result.details["home_facts"]["secondary"]
    assert "verify it has 50 units or fewer" in result.concern


def test_whole_unit_one_bedroom_accepts_small_known_building_and_rejects_large_one() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    small = ListingCandidate(
        platform="Zillow",
        source_id="small-one-bed",
        title="1 bed 1 bath apartment in Noe Valley",
        original_url="https://example.test/small-one-bed",
        price=2595,
        neighborhood="Noe Valley",
        summary="One-bedroom apartment in a 24-unit building.",
    )
    large = ListingCandidate(
        platform="Zillow",
        source_id="large-one-bed",
        title="1 bedroom apartment in Noe Valley",
        original_url="https://example.test/large-one-bed",
        price=2500,
        neighborhood="Noe Valley",
        summary="One-bedroom apartment in a 120-unit building.",
    )

    small_result = score_listing(small, profile)
    large_result = score_listing(large, profile)

    assert small_result.score >= profile.minimum_score
    assert small_result.details["home_facts"] == {
        "primary": "1 bedroom",
        "secondary": ["1 bath", "24-unit building"],
    }
    assert large_result.score < profile.minimum_score
    assert "above your 50-unit maximum" in large_result.concern


def test_one_bedroom_for_rent_is_not_mistaken_for_a_roommate_listing() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    entire = ListingCandidate(
        platform="HotPads",
        source_id="entire-one-bed",
        title="1 bedroom for rent in Bernal Heights",
        original_url="https://example.test/entire-one-bed",
        price=2400,
        neighborhood="Bernal Heights",
        listing_type="Apartment",
    )
    roommate = ListingCandidate(
        platform="HotPads",
        source_id="room-in-one-bed",
        title="Private room in a 1 bedroom apartment",
        original_url="https://example.test/room-in-one-bed",
        price=1400,
        neighborhood="Bernal Heights",
    )

    entire_result = score_listing(entire, profile)
    roommate_result = score_listing(roommate, profile)

    assert entire_result.details["housing_kind"] == "whole_unit"
    assert "housing_kind" not in roommate_result.details
    assert roommate_result.details["home_facts"]["primary"].startswith("Private room")


def test_shared_and_spanish_room_cards_never_enter_the_whole_unit_search() -> None:
    cases = (
        ListingCandidate(
            platform="Craigslist",
            source_id="shared-studio",
            title="Temporary shared Studio",
            original_url="https://example.test/shared-studio",
            price=1400,
            neighborhood="Inner Richmond",
            summary="Another bed can go in the room behind a curtain.",
            metadata={"attributes": "1BR / 1Ba | room not private | apartment"},
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="spanish-private-room",
            title="Habitación privada en alquiler",
            original_url="https://example.test/spanish-private-room",
            price=1700,
            neighborhood="Bernal Heights",
            summary="Baño compartido y cocina compartida con una familia.",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="spanish-room-in-apartment",
            title="1 habitación 1 baño - Departamento",
            original_url="https://example.test/spanish-room-in-apartment",
            price=1500,
            neighborhood="Mission Dolores",
            summary="Se renta cuarto con baño y se comparte con el propietario.",
        ),
        ListingCandidate(
            platform="Listings Project",
            source_id="plural-rooms-category",
            title="Sunny apartment in Potrero Hill",
            original_url="https://example.test/plural-rooms-category",
            price=2200,
            neighborhood="Potrero Hill",
            listing_type="Rooms for Rent",
            summary="This is a two-bedroom apartment looking for one person to occupy the second bedroom.",
        ),
    )

    for listing in cases:
        classified = classify_listing(listing)
        assert classified.housing_kind == ROOM
        assert classified.unit_type is None


def test_office_using_the_word_studio_is_not_treated_as_a_home() -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="office-studio",
        title="Affordable Office in Market Downtown",
        original_url="https://example.test/office-studio",
        price=1999,
        neighborhood="Castro",
        summary="Commercial space ideal for office, studio, or administrative use.",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == UNKNOWN
    assert classified.unit_type is None


def test_no_roommates_is_positive_whole_unit_evidence_not_a_room_signal() -> None:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="private-studio",
        title="Private studio apartment",
        original_url="https://example.test/private-studio",
        price=2200,
        neighborhood="Potrero Hill",
        summary="Private entrance and private bathroom. No roommates.",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == "whole_unit"
    assert classified.unit_type == "studio"


def test_two_bedroom_uses_a_two_person_split_and_hard_total_cap() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    match = ListingCandidate(
        platform="Craigslist",
        source_id="two-bed-match",
        title="2 bedroom apartment near Alamo Square",
        original_url="https://example.test/two-bed-match",
        price=5200,
        neighborhood="NOPA",
        summary="Entire two-bedroom apartment in a 12-unit building. Ideal for roommates.",
    )
    over_budget = ListingCandidate(
        platform="Craigslist",
        source_id="two-bed-over",
        title="2BR apartment near Alamo Square",
        original_url="https://example.test/two-bed-over",
        price=5500,
        neighborhood="NOPA",
        summary="Entire 2BR apartment in a 12-unit building.",
    )

    match_result = score_listing(match, profile)
    over_result = score_listing(over_budget, profile)

    assert match_result.score >= profile.minimum_score
    assert match_result.details["search_mode"] == "split_unit"
    assert match_result.details["per_person_monthly"] == 2600
    assert match_result.details["home_facts"] == {
        "primary": "2 bedrooms",
        "secondary": ["$2,600/person for 2", "Baths n/a", "12-unit building"],
    }
    assert over_result.score < profile.minimum_score
    assert "$2,700 per person" in over_result.concern


def test_three_bedroom_uses_three_people_and_its_own_exact_cap() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    at_cap = ListingCandidate(
        platform="Craigslist",
        source_id="three-bed-at-cap",
        title="3 bedroom apartment in Potrero Hill",
        original_url="https://example.test/three-bed-at-cap",
        price=7500,
        neighborhood="Potrero Hill",
        summary="Entire three-bedroom apartment in a 10-unit building.",
    )
    over_cap = ListingCandidate(
        platform="Craigslist",
        source_id="three-bed-over-cap",
        title="3BR apartment in Potrero Hill",
        original_url="https://example.test/three-bed-over-cap",
        price=7501,
        neighborhood="Potrero Hill",
        summary="Entire 3BR apartment in a 10-unit building.",
    )

    match_result = score_listing(at_cap, profile)
    over_result = score_listing(over_cap, profile)

    assert match_result.score >= profile.minimum_score
    assert match_result.details["search_mode"] == "split_unit"
    assert match_result.details["per_person_monthly"] == 2500
    assert match_result.details["occupants"] == 3
    assert match_result.details["home_facts"] == {
        "primary": "3 bedrooms",
        "secondary": ["$2,500/person for 3", "Baths n/a", "10-unit building"],
    }
    assert over_result.score < profile.minimum_score
    assert "$2,500 per person" in over_result.concern


def test_three_bedroom_still_enforces_target_area_and_small_building_limit() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    large_building = ListingCandidate(
        platform="Zillow",
        source_id="three-bed-large-building",
        title="3 bedroom apartment in Noe Valley",
        original_url="https://example.test/three-bed-large-building",
        price=7200,
        neighborhood="Noe Valley",
        summary="Entire 3-bedroom apartment in a 51-unit building.",
    )
    outside_area = ListingCandidate(
        platform="Zillow",
        source_id="three-bed-outside-area",
        title="3 bedroom apartment in SoMa",
        original_url="https://example.test/three-bed-outside-area",
        price=6900,
        neighborhood="SoMa",
        summary="Entire 3-bedroom apartment in a 6-unit building.",
    )

    large_result = score_listing(large_building, profile)
    outside_result = score_listing(outside_area, profile)

    assert large_result.score < profile.minimum_score
    assert "above your 50-unit maximum" in large_result.concern
    assert outside_result.score < profile.minimum_score
    assert "outside your target neighborhoods" in outside_result.concern


def test_room_ads_inside_three_bedroom_homes_never_enter_the_split_search() -> None:
    cases = (
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="room-in-three-bed",
            title="Private room in a 3 bedroom apartment",
            original_url="https://example.test/room-in-three-bed",
            price=1750,
            neighborhood="Mission Dolores",
            summary="Looking for one roommate to share our home.",
        ),
        ListingCandidate(
            platform="Facebook Groups",
            source_id="three-rooms-available",
            title="Three private bedrooms available in a shared house",
            original_url="https://example.test/three-rooms-available",
            price=1600,
            neighborhood="Potrero Hill",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="three-bed-marketplace-room-only",
            title="3 Beds 1 Bath Room only",
            original_url="https://example.test/three-bed-marketplace-room-only",
            price=1300,
            neighborhood="Mission Dolores",
            summary="One available room in a 3 bed apartment. Unit includes Room Only.",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="three-bed-marketplace-master-room",
            title="3 Beds 2.5 Baths Apartment",
            original_url="https://example.test/three-bed-marketplace-master-room",
            price=2200,
            neighborhood="Mission Dolores",
            summary="Private master bedroom available. I would be one of the roommates in this 3 bed apartment.",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="three-bed-marketplace-two-rooms",
            title="3 Beds 2 Baths House",
            original_url="https://example.test/three-bed-marketplace-two-rooms",
            price=1700,
            neighborhood="Mission District",
            summary="Looking for two awesome roommates. Two large bright bedrooms available in our Victorian.",
        ),
    )

    for listing in cases:
        classified = classify_listing(listing)
        assert classified.housing_kind == ROOM
        assert classified.unit_type is None


def test_secondary_whole_home_area_stays_visible_but_never_outranks_priority_areas() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="secondary-two-bed",
        title="2 bedroom apartment in Inner Sunset",
        original_url="https://example.test/secondary-two-bed",
        price=4000,
        neighborhood="Inner Sunset",
        summary="Entire 2-bedroom apartment in a 4-unit building.",
    )

    result = score_listing(listing, profile)

    assert result.score == 74
    assert result.details["neighborhood"]["priority"] == "secondary"


def test_private_room_inside_a_two_bedroom_never_enters_the_apartment_split() -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="room-in-two-bed",
        title="Private room in a 2 bedroom apartment",
        original_url="https://example.test/room-in-two-bed",
        price=1800,
        neighborhood="Mission Dolores",
        summary="Looking for a roommate to share the apartment.",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == ROOM
    assert classified.unit_type is None


def test_two_separate_bedrooms_for_rent_are_not_an_entire_two_bedroom() -> None:
    listing = ListingCandidate(
        platform="Facebook Groups",
        source_id="two-rooms",
        title="Two private bedrooms available in a shared house",
        original_url="https://example.test/two-rooms",
        price=1600,
        neighborhood="Potrero Hill",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == ROOM
    assert classified.unit_type is None


def test_marketplace_two_bedroom_shell_does_not_hide_roommate_or_room_copy() -> None:
    roommate = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="marketplace-roommate",
        title="2 Beds 1 Bath Apartment",
        original_url="https://example.test/marketplace-roommate",
        price=1400,
        neighborhood="Bernal Heights",
        summary="I have lived here for years and am looking for a chill roommate.",
    )
    advertised_room = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="marketplace-large-room",
        title="2 Beds 1 Bath Apartment",
        original_url="https://example.test/marketplace-large-room",
        price=1800,
        neighborhood="NOPA",
        summary="$1800 for large room with bay windows in a 2 bedroom Victorian apartment.",
    )

    for listing in (roommate, advertised_room):
        classified = classify_listing(listing)
        assert classified.housing_kind == ROOM
        assert classified.unit_type is None


def test_warning_post_is_not_classified_as_a_two_bedroom_offer() -> None:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="scam-warning",
        title="Scammer listings in SF",
        original_url="https://example.test/scam-warning",
        price=4500,
        neighborhood="Cole Valley",
        listing_type="2BR / 1Ba condo",
        summary="Be careful: there are many fake ads for apartments.",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == UNKNOWN
    assert classified.unit_type is None


def test_written_building_count_is_extracted() -> None:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="five-unit",
        title="2BR apartment",
        original_url="https://example.test/five-unit",
        summary="A spacious apartment in a classic five-unit walk-up.",
    )

    classified = classify_listing(listing)

    assert classified.building_units == 5


def test_explicit_outside_sf_address_overrides_a_bad_target_area_label() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="mill-valley-labelled-nopa",
        title="2 bedroom apartment",
        original_url="https://example.test/mill-valley",
        price=3645,
        neighborhood="NOPA",
        summary="396 Pine Hill Rd, Mill Valley, CA 94941.",
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert "Mill Valley (outside SF)" in result.concern


def test_unusually_low_two_bedroom_stays_visible_but_is_flagged_for_verification() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="low-two-bedroom",
        title="2 Beds 1 Bath Apartment",
        original_url="https://example.test/low-two-bedroom",
        price=1780,
        neighborhood="Potrero Hill",
        summary="Entire apartment with two bedrooms and one bathroom.",
    )

    result = score_listing(listing, profile)

    assert profile.minimum_score <= result.score <= 79
    assert "unusually low" in result.concern
    assert "room price or deposit" in result.concern


def test_craigslist_whole_unit_requires_a_clean_detail_check_and_price_floor() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    unverified = ListingCandidate(
        platform="Craigslist",
        source_id="unchecked",
        title="Studio apartment in Potrero Hill",
        original_url="https://www.craigslist.org/view/d/san-francisco-unchecked/unchecked",
        price=2200,
        neighborhood="Potrero Hill",
        summary="Studio apartment with a private entrance.",
    )
    under_floor = ListingCandidate(
        platform="Craigslist",
        source_id="under-floor",
        title="Studio apartment in Potrero Hill",
        original_url="https://www.craigslist.org/view/d/san-francisco-under-floor/under-floor",
        price=950,
        neighborhood="Potrero Hill",
        summary="Studio apartment with a private entrance.",
        metadata={"craigslist_detail_checked": True},
    )
    rejected = ListingCandidate(
        platform="Craigslist",
        source_id="rejected",
        title="Studio apartment in Potrero Hill",
        original_url="https://www.craigslist.org/view/d/san-francisco-rejected/rejected",
        price=2200,
        neighborhood="Potrero Hill",
        summary="Studio apartment with a private entrance.",
        metadata={
            "craigslist_detail_checked": True,
            "craigslist_content_rejected": True,
            "verification_concern": "Rejected: detail text conflicts with the advertised home.",
        },
    )

    unchecked_result = score_listing(unverified, profile)
    under_floor_result = score_listing(under_floor, profile)
    rejected_result = score_listing(rejected, profile)

    assert unchecked_result.score < profile.minimum_score
    assert "detail-page check" in unchecked_result.concern
    assert under_floor_result.score < profile.minimum_score
    assert "safety floor" in under_floor_result.concern
    assert rejected_result.score < profile.minimum_score
    assert rejected_result.concern.startswith("Rejected:")


def test_facebook_and_craigslist_sublets_need_an_explicit_six_month_term() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    eligible = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="six-month-sublet",
        title="Noe Valley 1BR sublet",
        original_url="https://www.facebook.com/marketplace/item/6month/",
        price=2300,
        neighborhood="Noe Valley",
        summary="Entire one-bedroom apartment. Six-month sublease available from August.",
    )
    too_short = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="three-month-sublet",
        title="Noe Valley 1BR sublet",
        original_url="https://www.facebook.com/marketplace/item/3month/",
        price=2300,
        neighborhood="Noe Valley",
        summary="Entire one-bedroom apartment. Sublet available for 3 months.",
    )
    unclear = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="unclear-sublet",
        title="Noe Valley 1BR sublet",
        original_url="https://www.facebook.com/marketplace/item/unclear/",
        price=2300,
        neighborhood="Noe Valley",
        summary="Entire one-bedroom apartment. Flexible sublet, dates to discuss.",
    )
    room_too_short = ListingCandidate(
        platform="Craigslist",
        source_id="three-month-room-sublet",
        title="Private room in Noe Valley",
        original_url="https://sfbay.craigslist.org/sfc/sub/room-3month.html",
        price=1500,
        neighborhood="Noe Valley",
        summary="Private room in a small flat. Sublet for 3 months.",
    )
    room_unclear = ListingCandidate(
        platform="Facebook Groups",
        source_id="unclear-room-sublet",
        title="Private room in Noe Valley",
        original_url="https://www.facebook.com/groups/example/posts/room-unclear/",
        price=1500,
        neighborhood="Noe Valley",
        summary="Private room in a small flat. Flexible sublet; dates to discuss.",
    )

    eligible_result = score_listing(eligible, profile)
    too_short_result = score_listing(too_short, profile)
    unclear_result = score_listing(unclear, profile)
    room_too_short_result = score_listing(room_too_short, profile)
    room_unclear_result = score_listing(room_unclear, profile)

    assert eligible_result.score >= profile.minimum_score
    assert eligible_result.details["sublease"] == {
        "is_sublease": True,
        "minimum_months": 6,
        "main_results_eligible": True,
    }
    assert too_short_result.score < profile.minimum_score
    assert "below the 6-month minimum" in too_short_result.concern
    assert unclear_result.score < profile.minimum_score
    assert "explicit 6-month term" in unclear_result.concern
    assert room_too_short_result.score < profile.minimum_score
    assert room_unclear_result.score < profile.minimum_score


def test_shared_bathroom_and_one_bedroom_available_copy_are_room_signals() -> None:
    cases = (
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="shared-bathroom",
            title="1 Bed 1 Bath Apartment",
            original_url="https://example.test/shared-bathroom",
            price=1400,
            neighborhood="Bernal Heights",
            summary="This bedroom is furnished. The bathroom will be shared with three roommates.",
        ),
        ListingCandidate(
            platform="Craigslist",
            source_id="one-bedroom-available",
            title="Beautiful 1 bedroom 1 bath",
            original_url="https://example.test/one-bedroom-available",
            price=1950,
            neighborhood="Marina",
            summary="One bedroom available in a historic four bedroom home.",
        ),
    )

    for listing in cases:
        classified = classify_listing(listing)
        assert classified.housing_kind == ROOM
        assert classified.unit_type is None


def test_fourplex_is_a_known_small_building() -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="fourplex",
        title="1 Bed 1 Bath House",
        original_url="https://example.test/fourplex",
        summary="Junior one-bedroom garden unit in a fourplex.",
    )

    assert classify_listing(listing).building_units == 4


def test_detail_area_and_outside_city_url_override_bad_card_locations() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    detail_area = ListingCandidate(
        platform="Craigslist",
        source_id="detail-area",
        title="Studio apartment",
        original_url="https://www.craigslist.org/view/d/san-francisco-studio/detail-area",
        price=2200,
        neighborhood="Potrero Hill",
        summary="Private studio apartment.",
        metadata={"detail_declared_neighborhood": "Excelsior"},
    )
    outside_url = ListingCandidate(
        platform="Craigslist",
        source_id="outside-url",
        title="1BR apartment",
        original_url="https://www.craigslist.org/view/d/oakland-open-house/outside-url",
        price=2195,
        neighborhood="Marina",
        summary="One-bedroom apartment with monthly rent.",
    )

    detail_result = score_listing(detail_area, profile)
    outside_result = score_listing(outside_url, profile)

    assert detail_result.score < profile.minimum_score
    assert "Excelsior" in detail_result.concern
    assert outside_result.score < profile.minimum_score
    assert "Oakland (outside SF)" in outside_result.concern


def test_sub_month_date_range_does_not_count_as_monthly_rent() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    cases = (
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="short-stay-words",
            title="One bedroom sublet - Aug 6-17",
            original_url="https://example.test/short-stay-words",
            price=2000,
            neighborhood="Hayes Valley",
            summary="Only available from Aug 6 to Aug 17.",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="short-stay-numeric",
            title="1 Bed 1 Bath Apartment",
            original_url="https://example.test/short-stay-numeric",
            price=2400,
            neighborhood="Lower Haight",
            summary="Available 8/7-30 for the listed dates.",
        ),
    )

    results = [score_listing(listing, profile) for listing in cases]

    assert all(result.score < profile.minimum_score for result in results)
    assert "12 days" in results[0].concern
    assert "24 days" in results[1].concern
    assert all("not a full monthly rent" in result.concern for result in results)


def test_whole_unit_preserves_an_external_verification_concern() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="HotPads",
        source_id="verified-concern",
        title="281 14th St studio",
        original_url="https://example.test/verified-concern",
        price=2000,
        neighborhood="Mission District",
        summary="Studio apartment available now in a 22-unit building.",
        metadata={
            "bedrooms": 0,
            "building_units": 22,
            "verification_concern": "Verify an open fire-safety item before applying.",
        },
    )

    result = score_listing(listing, profile)

    assert result.score >= profile.minimum_score
    assert result.concern == "Verify an open fire-safety item before applying."


def test_verified_inactive_whole_unit_stays_out_of_the_shortlist() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="SpareRoom",
        source_id="lease-pending",
        title="One bedroom condo",
        original_url="https://example.test/lease-pending",
        price=2400,
        neighborhood="Inner Parkside",
        summary="One bedroom condo in a four-unit building.",
        metadata={
            "bedrooms": 1,
            "building_units": 4,
            "verified_inactive": True,
            "verification_concern": "Verified inactive: lease pending and applications are closed.",
        },
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert result.concern == "Verified inactive: lease pending and applications are closed."


def test_missing_information_is_neutral_not_a_definite_negative(preferences: Preferences) -> None:
    unknown = ListingCandidate(
        platform="Test",
        source_id="unknown",
        title="Room available",
        original_url="https://example.test/unknown",
        price=1500,
        neighborhood="NOPA",
    )
    mismatch = ListingCandidate(
        platform="Test",
        source_id="mismatch",
        title="Shared bedroom in apartment",
        original_url="https://example.test/mismatch",
        price=3000,
        neighborhood="SOMA",
        listing_type="shared bedroom",
        summary="A 24-month lease with 12 roommates. No yard and no natural light.",
        metadata={"property_type": "apartment", "rooms_in_property": "12"},
    )

    unknown_result = score_listing(unknown, preferences)
    mismatch_result = score_listing(mismatch, preferences)

    assert unknown_result.score > mismatch_result.score
    assert unknown_result.score >= 60
    assert unknown_result.concern.startswith("Unknown:")


def test_real_profile_prioritizes_ideal_price_area_and_flexible_lease() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    ideal = ListingCandidate(
        platform="Test",
        source_id="ideal",
        title="Sunny private room in a Victorian house near Dolores Park",
        original_url="https://example.test/ideal",
        price=1500,
        neighborhood="Potrero Hill",
        listing_type="Private room",
        summary="Month-to-month sublease with two clean, chill, outdoorsy roommates, a garden and laundry.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )
    acceptable = ListingCandidate(
        platform="Test",
        source_id="acceptable",
        title="Private room in a nice shared flat",
        original_url="https://example.test/acceptable",
        price=2200,
        neighborhood="NOPA",
        listing_type="Private room",
        summary="12 month lease with four quiet roommates.",
        metadata={"property_type": "apartment", "rooms_in_property": "5"},
    )

    ideal_result = score_listing(ideal, profile)
    acceptable_result = score_listing(acceptable, profile)

    assert ideal_result.score >= 90
    assert acceptable_result.score >= profile.minimum_score
    assert ideal_result.score > acceptable_result.score
    assert any("ideal price" in reason for reason in ideal_result.reasons)


def test_real_profile_accepts_the_current_private_room_budget() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="room-budget-cap",
        title="Sunny private room near Dolores Park",
        original_url="https://example.test/near-miss",
        price=2700,
        neighborhood="Mission Dolores",
        summary="Flexible sublease with two clean roommates and a garden.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.score >= profile.minimum_score
    assert result.details["price"]["value"] == 0.72
    assert "outside your" not in result.concern


def test_real_profile_keeps_private_rooms_above_the_cap_out_of_the_shortlist() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="room-over-budget",
        title="Sunny private room near Dolores Park",
        original_url="https://example.test/over-budget",
        price=2710,
        neighborhood="Mission Dolores",
        summary="Flexible sublease with two clean roommates and a garden.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert "slightly outside your $800–$2,700 budget" in result.concern


def test_real_profile_prioritizes_august_availability_and_keeps_late_openings_out_of_shortlist() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    shared_fields = {
        "platform": "Furnished Finder",
        "title": "Sunny private room in a Victorian house near Dolores Park",
        "price": 1500,
        "neighborhood": "Mission Dolores",
        "listing_type": "Private room · House",
        "metadata": {"property_type": "house", "rooms_in_property": "3"},
    }
    august = ListingCandidate(
        source_id="august-ready",
        original_url="https://example.test/august-ready",
        summary="Available: Aug. 15, 2026. Flexible month-to-month sublease with garden and laundry.",
        **shared_fields,
    )
    next_year = ListingCandidate(
        source_id="next-year",
        original_url="https://example.test/next-year",
        summary="Available: Jan. 19, 2027. Flexible month-to-month sublease with garden and laundry.",
        **shared_fields,
    )

    august_result = score_listing(august, profile)
    next_year_result = score_listing(next_year, profile)

    assert august_result.score > next_year_result.score
    assert august_result.details["availability"]["value"] == 1.0
    assert next_year_result.score < profile.minimum_score
    assert next_year_result.details["availability"]["value"] == 0.0
    assert "near-term cutoff" in next_year_result.concern


def test_missing_availability_stays_neutral() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Furnished Finder",
        source_id="date-unknown",
        title="Private room in a shared house",
        original_url="https://example.test/date-unknown",
        price=1500,
        neighborhood="Potrero Hill",
        summary="Flexible sublease with two clean roommates and a garden.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.details["availability"]["known"] is False
    assert result.details["availability"]["value"] == 0.5


def test_landmark_location_is_useful_display_context_but_not_an_out_of_area_penalty() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Furnished Finder",
        source_id="near-golden-gate-park",
        title="Sunny private room",
        original_url="https://example.test/near-golden-gate-park",
        price=1500,
        neighborhood="Near Golden Gate Park",
        summary="Available: Aug. 1, 2026. Flexible sublease with a garden.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.details["neighborhood"]["known"] is False
    assert result.details["neighborhood"]["value"] == 0.5


def test_facebook_like_mission_listing_scores_as_a_real_match() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="mission-example",
        title="4 Beds 1.5 Baths - Apartment",
        original_url="https://www.facebook.com/marketplace/item/1412015050215985/",
        price=1360,
        neighborhood="San Francisco, CA",
        listing_type="Apartment",
        summary=(
            "Large sunny bay window room with rear garden view on a quiet street in the heart "
            "of the Mission District, six minutes from Dolores Park. Four roommates, sunny back "
            "porch, roof access, 1930s Edwardian, clean developer, hiking, skateboarding and cooking."
        ),
    )

    result = score_listing(listing, profile)

    assert result.score >= 70
    assert result.details["neighborhood"]["match_label"] == "Mission Dolores"


def test_real_profile_neighborhood_priority_beats_a_better_price_outside_target_areas() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    ideal_area = ListingCandidate(
        platform="Test",
        source_id="potrero",
        title="Private room in a shared flat",
        original_url="https://example.test/potrero",
        price=1975,
        neighborhood="Potrero Hill",
        summary="Clean home with a flexible sublease.",
    )
    wrong_area = ListingCandidate(
        platform="Test",
        source_id="park-merced",
        title="Sunny private room in a house",
        original_url="https://example.test/park-merced",
        price=1500,
        neighborhood="Park Merced",
        summary="Flexible lease with garden and laundry.",
        metadata={"property_type": "house"},
    )

    ideal_result = score_listing(ideal_area, profile)
    wrong_area_result = score_listing(wrong_area, profile)

    assert ideal_result.score > wrong_area_result.score
    assert wrong_area_result.score < profile.minimum_score
    assert wrong_area_result.concern == "Park Merced is outside your target neighborhoods."


def test_real_profile_uses_neighborhood_aliases_without_penalizing_a_generic_city_label() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    alias = ListingCandidate(
        platform="Test",
        source_id="nopa",
        title="Room in an Alamo Square house",
        original_url="https://example.test/nopa",
        price=1500,
        neighborhood="San Francisco",
    )
    generic = ListingCandidate(
        platform="Test",
        source_id="generic",
        title="Room in a house",
        original_url="https://example.test/generic",
        price=1500,
        neighborhood="San Francisco",
    )

    alias_result = score_listing(alias, profile)
    generic_result = score_listing(generic, profile)

    assert alias_result.details["neighborhood"]["value"] == 0.55
    assert generic_result.details["neighborhood"]["value"] == 0.5


def test_room_for_rent_is_a_private_room_signal_unless_explicitly_shared(preferences: Preferences) -> None:
    private = ListingCandidate(
        platform="Test",
        source_id="private",
        title="Room for rent in a shared house",
        original_url="https://example.test/private",
        price=1500,
        neighborhood="NOPA",
    )
    shared = ListingCandidate(
        platform="Test",
        source_id="shared",
        title="Shared room for rent in a house",
        original_url="https://example.test/shared",
        price=1500,
        neighborhood="NOPA",
    )

    private_result = score_listing(private, preferences)
    shared_result = score_listing(shared, preferences)

    assert private_result.details["private_room"]["value"] == 1.0
    assert shared_result.details["private_room"]["value"] == 0.0


def test_real_profile_rejects_explicit_shared_room_complex() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="bad",
        title="Shared room per bed in apartment complex",
        original_url="https://example.test/bad",
        price=2500,
        neighborhood="SOMA",
        listing_type="Shared room",
        summary="24 month lease, ten roommates, no yard and no natural light.",
        metadata={"property_type": "apartment", "rooms_in_property": "10"},
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert result.concern.startswith("Possible dealbreaker")


def test_private_room_in_shared_apartment_counts_as_shared_flat() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="shared-flat-label",
        title="Sunny private room with two roommates",
        original_url="https://example.test/shared-flat-label",
        price=1500,
        neighborhood="Potrero Hill",
        listing_type="Private room",
        summary="A clean shared apartment with a garden and laundry.",
        metadata={"property_type": "apartment", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.details["property_type"]["value"] == 1.0
    assert "outside your preferred home types" not in result.concern


def test_score_records_normalized_target_neighborhood_for_display() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="dolores-normalized",
        title="Quiet private room beside Dolores Park",
        original_url="https://example.test/dolores-normalized",
        price=1500,
        neighborhood="city of san francisco",
    )

    result = score_listing(listing, profile)

    assert result.details["neighborhood"]["match_label"] == "Mission Dolores"


def test_whole_unit_card_in_an_ideal_area_needs_room_evidence_for_shortlist(preferences) -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="whole-unit-in-potrero",
        original_url="https://example.test/whole-unit-in-potrero",
        title="Entire apartment for rent",
        price=1700,
        neighborhood="Potrero Hill",
        summary="A bright apartment near the waterfront.",
    )

    result = score_listing(listing, preferences)

    assert result.score <= 59
    assert result.details["private_room"]["main_results_eligible"] is False


def test_scoring_flags_explicit_women_only_household_and_declared_cross_street(preferences) -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="women-only-cross-street",
        original_url="https://example.test/women-only-cross-street",
        title="Sunny private room",
        price=1500,
        neighborhood="Potrero Hill",
        summary="Women only. Private room on the corner of 23rd St and Bryant St.",
    )

    result = score_listing(listing, preferences)

    assert result.concern == "Women-only or women-preferred household; confirm eligibility."
    assert result.details["household_restriction"] == result.concern
    assert result.details["location_hint"] == "23rd St & Bryant St"
