"""A listing was labelled instead of the unknown being named.

"Needs verification" told a renter that something about a home was unresolved
but never what, so the badge could not be acted on. Worse, the one sentence the
row did show came from a different selection than the constraints that actually
held the listing back: a $1,845-per-week sublet said only "needs verification",
its Check column talked about the sublet term, and the fact that the amount
might not be a monthly rent at all appeared nowhere on the page.

Every constraint now carries the short name of what is unresolved as well as
the sentence. The tests here are mostly about the two ways naming can go wrong:
naming the wrong one, and naming none because a new constraint was added
without a name.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.classification import classify_listing
from sf_housing.database import Repository
from sf_housing.models import (
    CHECK_ORDER,
    CRITERION_CHECKS,
    ListingCandidate,
    ScoreResult,
    ordered_checks,
    unmeasured_criteria,
)
from sf_housing.preferences import parse_preferences
from sf_housing.scoring import score_listing
from sf_housing.settings import Settings


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

PREFERENCES = """
minimum_score: 55
budget: {min_monthly: 800, max_monthly: 3000}
ideal_neighborhoods: [Mission District]
preferred_neighborhoods: [SoMa]
private_room: true
lease: {min_months: 6, max_months: 12, flexible: true}
household: {min_people: 1, max_people: 4}
"""


def preferences():
    return parse_preferences(PREFERENCES)


def listing(**overrides) -> ListingCandidate:
    fields = dict(
        platform="Craigslist",
        source_id="x1",
        title="Sunny 1 bedroom in the Mission District",
        original_url="https://sfbay.craigslist.org/x/1.html",
        price=2400,
        neighborhood="Mission District",
        listing_type="Apartment",
        summary="A one bedroom apartment available now.",
    )
    fields.update(overrides)
    return classify_listing(ListingCandidate(**fields))


def scored(**overrides) -> ScoreResult:
    return score_listing(listing(**overrides), preferences())


def checks_of(result: ScoreResult, status: str = "unknown") -> list[str]:
    return [item["check"] for item in ordered_checks(result.details.get("hard_constraints"), status)]


# --------------------------------------------------------------------------
# every constraint is named, and every name is ordered
# --------------------------------------------------------------------------


def test_no_hard_constraint_can_be_added_without_naming_what_to_check() -> None:
    """The regression this whole change exists to prevent.

    A constraint added without a name degrades silently: eligibility still
    changes, the row still shows a caution, and the caution goes back to saying
    "Needs verification" with no indication that anything was missed.
    """
    source = (REPO_ROOT / "sf_housing" / "scoring.py").read_text(encoding="utf-8")
    unnamed = re.findall(r'append\(\{"status": "[a-z]+", "reason".*', source)
    assert unnamed == [], f"these constraints carry no check name: {unnamed}"


def test_every_name_the_scorer_uses_has_a_defined_place_in_the_order() -> None:
    """A name missing from CHECK_ORDER still sorts, but always last, which is
    silently wrong for something urgent."""
    source = (REPO_ROOT / "sf_housing" / "scoring.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"check": "([a-z ]+)"', source))
    assert used, "the scorer must name its constraints"
    assert used <= set(CHECK_ORDER), f"unordered check names: {sorted(used - set(CHECK_ORDER))}"


def test_no_name_appears_twice_in_the_order() -> None:
    """A repeated name silently takes the later of its two places, so an urgent
    check listed first and again near the end would rank as unimportant with
    nothing to show for it."""
    assert len(CHECK_ORDER) == len(set(CHECK_ORDER))


def test_the_criterion_vocabulary_agrees_with_the_constraint_vocabulary() -> None:
    """The two exist to recognise one fact stated in two voices; a name in one
    and not the other means the fact is asked about twice."""
    assert set(CRITERION_CHECKS.values()) <= set(CHECK_ORDER)


def test_a_name_that_is_not_in_the_order_is_kept_rather_than_dropped() -> None:
    unordered = [{"status": "unknown", "check": "sunlight", "reason": "Confirm the light."}]
    assert ordered_checks(unordered, "unknown") == [
        {"check": "sunlight", "reason": "Confirm the light."}
    ]


# --------------------------------------------------------------------------
# the right unknown leads
# --------------------------------------------------------------------------


def test_the_badge_names_the_unknown_rather_than_labelling_the_listing() -> None:
    result = scored()
    assert result.eligibility == "needs_verification"
    assert checks_of(result)[0] in CHECK_ORDER


def test_a_rent_that_looks_wrong_outranks_a_building_size_nobody_publishes() -> None:
    """The case that started this. Most listings cannot state a building size,
    so ranking by the order constraints happen to be appended in buried the one
    thing worth a minute of the renter's time behind the one thing that is
    almost always unknown."""
    result = scored(price=300)

    assert "rent" in checks_of(result)
    assert checks_of(result)[0] == "rent"
    assert checks_of(result).index("rent") < checks_of(result).index("building size")


def test_a_missing_price_outranks_a_missing_building_size() -> None:
    assert checks_of(scored(price=None)).index("price") < checks_of(scored(price=None)).index(
        "building size"
    )


def test_the_same_listing_names_its_checks_in_the_same_order_every_time() -> None:
    """A home must not swap the reason it is flagged between page loads."""
    orders = {tuple(checks_of(scored(price=300))) for _ in range(50)}
    assert len(orders) == 1


def test_one_fact_is_asked_about_once_even_when_two_constraints_raise_it() -> None:
    """An unstated building size is both an unmeasured criterion and an unmet
    limit, and the reader should be asked once."""
    names = checks_of(scored())
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------
# a failure always outranks an open question
# --------------------------------------------------------------------------


def test_an_excluded_listing_says_why_it_was_excluded_not_what_is_unknown() -> None:
    """312 of 315 excluded listings in a real scan also carried open questions.
    Showing one of those instead of the exclusion answers a question the reader
    did not ask and hides the one they did."""
    result = scored(price=9000)

    assert result.eligibility == "ineligible"
    assert checks_of(result, "fail") == ["price"]
    assert checks_of(result, "unknown"), "this listing genuinely has open questions too"


def test_the_row_of_an_excluded_listing_shows_the_exclusion(tmp_path: pathlib.Path) -> None:
    page, ids = render(
        tmp_path, [("over", scored(price=9000), listing(price=9000))], view="near_matches"
    )
    row = row_for(page, ids["over"])

    assert "exceeds this path" in row
    assert "Confirm the building has" not in row, "the exclusion is what matters here"


# --------------------------------------------------------------------------
# what a renter actually sees
# --------------------------------------------------------------------------


def settings_for(tmp_path: pathlib.Path) -> Settings:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(PREFERENCES, encoding="utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )


def render(tmp_path: pathlib.Path, rows, *, view: str = "shortlist", housing: str = "one_bedroom"):
    """Store the given listings and return the page plus each one's row id."""
    from dataclasses import replace

    settings = settings_for(tmp_path)
    repository = Repository(settings.database_path)
    repository.initialize()
    identifiers = {}
    for source_id, result, candidate in rows:
        listing_id, _ = repository.upsert_listing(replace(candidate, source_id=source_id), result)
        identifiers[source_id] = listing_id
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        response = client.get(f"/?view={view}&housing={housing}")
    assert response.status_code == 200
    return response.text, identifiers


def row_for(page: str, listing_id: int) -> str:
    """The one row for this listing.

    Matched on the identifier the row links to rather than on any text, because
    a substring search finds "low" inside "below" and quietly asserts nothing.
    """
    rows = re.findall(r'<tr class="listing-row.*?</tr>', page, re.S)
    matching = [row for row in rows if f"/listings/{listing_id}/" in row]
    assert matching, f"no row rendered for listing {listing_id}"
    assert len(matching) == 1
    return matching[0]


def test_the_shortlist_badge_says_what_to_check(tmp_path: pathlib.Path) -> None:
    page, ids = render(
        tmp_path, [("low", scored(price=300), listing(price=300))], view="near_matches"
    )
    row = row_for(page, ids["low"])

    assert "Check rent" in row
    assert "Needs verification" not in row


def test_the_check_column_shows_every_open_question_not_one_of_them(
    tmp_path: pathlib.Path,
) -> None:
    """The column used to show a sentence chosen by different logic than the
    constraints, so a listing could be held back for a reason the row never
    mentioned."""
    result = scored(price=300)
    page, ids = render(
        tmp_path, [("low", result, listing(price=300))], view="near_matches"
    )
    row = row_for(page, ids["low"])

    reasons = [item["reason"] for item in ordered_checks(result.details["hard_constraints"], "unknown")]
    assert len(reasons) >= 2
    for reason in reasons:
        assert reason.split(".")[0] in row.replace("&#39;", "'"), f"missing from the row: {reason}"


def test_a_fact_named_as_a_check_is_not_repeated_as_missing_evidence(
    tmp_path: pathlib.Path,
) -> None:
    """"Confirm the building has 50 units or fewer" and "building size is not
    stated" are one fact in two voices."""
    page, ids = render(tmp_path, [("plain", scored(), listing())], view="shortlist")
    row = row_for(page, ids["plain"])

    assert "Confirm the building has" in row
    assert "building size is not stated" not in row


def test_the_saved_page_names_the_questions_before_someone_makes_contact(
    tmp_path: pathlib.Path,
) -> None:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    repository = Repository(settings.database_path)
    repository.initialize()
    listing_id, _ = repository.upsert_listing(listing(price=300), scored(price=300))
    assert repository.set_listing_status(listing_id, "saved")

    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/?view=saved&housing=one_bedroom").text

    assert "check rent" in page
    assert "needs verification" not in page
    assert "Confirm that this unusually low amount" in page


# --------------------------------------------------------------------------
# a listing scored before checks were named
# --------------------------------------------------------------------------


def rewrite_stored_constraints(database: pathlib.Path, transform) -> None:
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("SELECT id, score_details_json FROM listings").fetchall()
        for listing_id, raw in rows:
            details = json.loads(raw)
            details["hard_constraints"] = transform(details.get("hard_constraints"))
            connection.execute(
                "UPDATE listings SET score_details_json = ? WHERE id = ?",
                (json.dumps(details), listing_id),
            )
        connection.commit()
    finally:
        connection.close()


def page_after_rewrite(tmp_path: pathlib.Path, transform, *, view: str):
    """Render a row the running app has not rescored.

    Start-up rescores stored listings, which would put the names straight back,
    so the rewrite happens after the app is up -- exactly the state an upgraded
    install is in until its next scan.
    """
    settings = settings_for(tmp_path)
    repository = Repository(settings.database_path)
    repository.initialize()
    listing_id, _ = repository.upsert_listing(listing(price=300), scored(price=300))
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        client.get("/")  # start-up rescoring happens here, before the rewrite
        rewrite_stored_constraints(settings.database_path, transform)
        response = client.get(f"/?view={view}&housing=one_bedroom")
    assert response.status_code == 200
    return response.text, listing_id


def test_a_listing_scored_before_this_change_still_shows_its_sentences(
    tmp_path: pathlib.Path,
) -> None:
    """An upgrade does not rescore a stored listing, so an older row carries no
    names. It has to go back to what it showed before rather than go blank."""
    page, listing_id = page_after_rewrite(
        tmp_path,
        lambda constraints: [
            {key: value for key, value in constraint.items() if key != "check"}
            for constraint in constraints
        ],
        view="near_matches",
    )
    row = row_for(page, listing_id)

    assert "Needs verification" in row, "no name is available, so say what was said before"
    assert "Confirm that this unusually low amount is the full monthly rent." in row


@pytest.mark.parametrize("stored", ["not a list", None, [], [{"status": "unknown"}]])
def test_an_unreadable_constraint_list_never_breaks_the_page(
    tmp_path: pathlib.Path, stored
) -> None:
    """Whatever is in the column, the listing itself still has to reach the
    reader; losing the row loses the home."""
    page, listing_id = page_after_rewrite(tmp_path, lambda _: stored, view="near_matches")

    assert row_for(page, listing_id)


@pytest.mark.parametrize(
    "constraints",
    [None, [], "text", [None], [{}], [{"status": "unknown"}], [{"status": "unknown", "reason": ""}]],
)
def test_ordering_survives_anything_stored_in_the_constraint_list(constraints) -> None:
    assert ordered_checks(constraints, "unknown") == []


@pytest.mark.parametrize("details", [None, "text", {}, {"price": None}, {"price": {"known": False}}])
def test_unmeasured_criteria_survives_anything_stored_in_the_details(details) -> None:
    assert unmeasured_criteria(details, set()) == []
