"""How large a building you are willing to live in.

This was a hidden field posting 50 on every save. The preference underneath was
real -- stored per path, read by scoring -- but nobody could answer it, and
homes were being refused on a limit no one had chosen: a 449-home building
scored 38 on building size alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.deal_profile import deal_profile_from_form, profile_form_values
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.scoring import score_listing
from tests.test_deal_profile import settings_for


WHOLE_PATHS = ("studio", "one_bedroom", "two_bedroom", "three_bedroom", "four_bedroom")


def form(**extra) -> dict:
    base = {
        "housing_paths": ["studio", "three_bedroom", "private_room"],
        "studio_maximum": "3000",
        "three_bedroom_maximum": "2000",
        "three_bedroom_occupants": "3",
        "private_room_maximum": "1800",
        "anywhere_in_sf": "1",
    }
    base.update(extra)
    return base


def profile(**extra):
    return deal_profile_from_form(form(**extra))


# --------------------------------------------------------------------------
# the answer itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("posted", "stored"), [("25", 25), ("50", 50), ("100", 100), ("", None)])
def test_the_answer_is_kept_as_given(posted, stored) -> None:
    """"Any size" is a real answer, not a missing one, so it is kept as no
    limit rather than as a number too large to ever match."""
    saved = profile(building_units=posted)

    for path in ("studio", "three_bedroom"):
        assert saved.budgets[path].maximum_building_units == stored


def test_one_answer_covers_every_whole_home() -> None:
    """Nobody wants a different building-size answer for a studio than for a
    four-bedroom, and asking six times is six chances to disagree."""
    saved = profile(building_units="25")

    limits = {saved.budgets[path].maximum_building_units for path in ("studio", "three_bedroom")}
    assert limits == {25}


def test_a_private_room_is_not_asked_at_all() -> None:
    """A room in somebody's flat is not a building you are choosing."""
    saved = profile(building_units="25")
    assert saved.budgets["private_room"].maximum_building_units is None


def test_a_form_that_never_had_the_field_still_saves() -> None:
    """The hidden field shipped for long enough that a saved draft or an older
    client may still post the per-path shape."""
    assert profile().budgets["studio"].maximum_building_units == 50
    assert profile(studio_building_units="100").budgets["studio"].maximum_building_units == 100


@pytest.mark.parametrize("posted", ["25", "100", ""])
def test_the_answer_survives_a_round_trip(posted) -> None:
    saved = profile(building_units=posted)
    shown = profile_form_values(saved)["building_units"]

    assert shown == saved.budgets["studio"].maximum_building_units
    assert deal_profile_from_form(form(building_units="" if shown is None else str(shown))) == saved


# --------------------------------------------------------------------------
# what it changes about a home
# --------------------------------------------------------------------------


def big_building(units: int = 449) -> ListingCandidate:
    return ListingCandidate(
        platform="Rent.com",
        source_id="lc1",
        title="855 Brannan",
        original_url="https://www.rent.com/apartment/x-lc1",
        price=2500,
        neighborhood="SoMa",
        listing_type="Studio apartment",
        summary="855 Brannan listed on Rent.com. Its studio homes start at $2,500 a month.",
        metadata={"bedrooms": 0},
        housing_kind="whole_unit",
        unit_type="studio",
        building_units=units,
    )


def preferences_for(units: str):
    import yaml

    from sf_housing.deal_profile import canonical_document

    return parse_preferences(yaml.safe_dump(canonical_document(profile(building_units=units))))


def test_a_tower_is_refused_when_you_asked_for_a_small_building() -> None:
    result = score_listing(big_building(), preferences_for("50"))

    assert result.score <= 49
    assert any(item["check"] == "building size" and item["status"] == "fail" for item in result.details["hard_constraints"])


def test_a_tower_is_not_refused_once_you_say_any_size() -> None:
    """The whole point: 449 homes is a fact, not a fault, unless you said so."""
    small = score_listing(big_building(), preferences_for("50"))
    anysize = score_listing(big_building(), preferences_for(""))

    assert anysize.score > small.score
    assert not any(item["check"] == "building size" for item in anysize.details["hard_constraints"])
    assert "unit maximum" not in anysize.concern


def test_an_unstated_size_stops_being_a_question_you_did_not_ask() -> None:
    """Asked for a small building, an unknown size is worth verifying. Having
    said any size, it is not a gap -- it is a question you declined."""
    unknown = big_building()
    unknown.building_units = None

    asked = score_listing(unknown, preferences_for("25"))
    unasked = score_listing(unknown, preferences_for(""))

    assert any(item["check"] == "building size" for item in asked.details["hard_constraints"])
    assert not any(item["check"] == "building size" for item in unasked.details["hard_constraints"])
    assert unasked.score > asked.score
    # And it is never mentioned in words either. Asked with no ceiling set, the
    # sentence reads "verify it has None units or fewer", which is the kind of
    # thing a reader rightly stops trusting a page over.
    assert "building size" not in unasked.concern.casefold()
    assert "None" not in unasked.concern
    assert "building size is not stated" in asked.concern


def test_a_tighter_answer_refuses_more() -> None:
    thirty = big_building(30)

    assert score_listing(thirty, preferences_for("50")).score > 49
    assert score_listing(thirty, preferences_for("25")).score <= 49


# --------------------------------------------------------------------------
# what the reader sees
# --------------------------------------------------------------------------


def page_for(tmp_path: Path, units: str | None = None) -> str:
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    if units is not None:
        import yaml

        from sf_housing.deal_profile import canonical_document

        settings.preferences_path.write_text(
            yaml.safe_dump(canonical_document(profile(building_units=units))), encoding="utf-8"
        )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        return client.get("/preferences").text


def test_the_question_is_asked_out_loud(tmp_path: Path) -> None:
    page = page_for(tmp_path)

    assert "Largest building you would live in" in page
    # Four choices a reader can actually see and click. Counting the fields
    # alone would pass just as happily on four hidden ones, which is the exact
    # shape this replaced.
    assert page.count('type="radio" name="building_units"') == 4
    assert 'name="building_units"' in page and 'type="hidden" name="building_units"' not in page
    assert 'type="hidden" name="studio_building_units"' not in page
    for label in ("Up to 25", "Up to 50", "Up to 100", "Any size"):
        assert label in page


def enclosing_div(page: str, opener: str) -> str:
    """The markup of one <div>, from its opening tag to the tag that closes it.

    Slicing to the next landmark on the page instead is how the first version of
    the test below passed with the question moved back out of the table: the
    landmark was further down than the closing tag, so outside still read as
    inside.
    """
    start = page.index(opener)
    at = page.index(">", start) + 1
    depth = 1
    while depth:
        opened = page.find("<div", at)
        closed = page.find("</div>", at)
        assert closed != -1, f"{opener!r} is never closed"
        if opened != -1 and opened < closed:
            depth, at = depth + 1, opened + 4
        else:
            depth, at = depth - 1, closed + 6
    return page[start:at]


def test_the_question_is_the_last_row_of_the_table_it_applies_to(tmp_path: Path) -> None:
    """It reads "applies to every whole home above", and it used to sit outside
    the box those homes are in, joined by a hairline that doubled the border
    under the last row. Inside the ledger, on the same fill as the column
    headings, it bookends the table it is asking about."""
    from tests.test_deal_profile import block_body, stylesheet

    page = page_for(tmp_path)
    ledger = enclosing_div(page, '<div class="path-ledger')

    assert 'class="size-choice"' in ledger, "the question sits outside the table again"
    assert ledger.rindex('class="path-row"') < ledger.index('class="size-choice"'), "it comes last"

    band = block_body(stylesheet(), ".size-choice { ")
    head = block_body(stylesheet(), ".path-ledger-head { ")
    assert "background: var(--surface-muted)" in band, band
    assert "var(--surface-muted)" in head, "the two ends of the table have to match"


def test_a_link_shaped_button_lays_out_like_the_text_around_it() -> None:
    """Every button here is a 42px inline-flex box centred on its own label, so
    one sitting inside a sentence pushed the whole line box down and opened a
    blank line through the middle of the help text. A button that reads as a
    link has to lay out as one."""
    from tests.test_deal_profile import block_body, stylesheet

    style = stylesheet()
    shared = block_body(style, "button, .button {")
    link = block_body(style, ".link-button { ")

    assert "min-height: 42px" in shared and "display: inline-flex" in shared, shared
    assert "min-height: 0" in link and "display: inline" in link, link


@pytest.mark.parametrize(("units", "expected"), [("25", 'value="25" checked'), ("", 'value="" checked')])
def test_your_answer_is_the_one_shown_back(tmp_path: Path, units, expected) -> None:
    page = page_for(tmp_path, units)
    assert expected in page.replace("  ", " ")


def test_saving_the_page_keeps_the_answer(tmp_path: Path) -> None:
    """The whole reason this was worth doing: the old hidden field posted 50
    on every save, so any other answer lasted exactly until the next one."""
    from tests.test_deal_profile import valid_form

    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    payload = {key: value for key, value in valid_form().items() if not key.endswith("_building_units")}
    payload["building_units"] = ""

    with TestClient(application) as client:
        posted = client.post("/preferences/deal", data=payload, follow_redirects=False)
        assert posted.status_code in (200, 303), posted.text[:300]
        page = client.get("/preferences").text

    assert 'value="" checked' in page.replace("  ", " ")
    assert 'value="50" checked' not in page.replace("  ", " ")
