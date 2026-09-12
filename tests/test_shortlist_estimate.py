"""The number beside the cut-off slider, while the deal is still being edited.

It used to be read from the scores already in the database, which are the
scores of the deal as *saved*. Dropping a neighbourhood or lowering a budget
left it standing at the answer for a deal that no longer existed, and it only
moved once the whole form had been submitted and every stored home reranked.

These pin the two halves of fixing that: the number follows the deal in hand,
and it is affordable to work out -- exact while the pool fits under the
ceiling, and a bounded, repeatable estimate when it does not. Repeatable
matters as much as quick: a number redrawn on every keystroke that disagreed
with itself between two keystrokes that changed nothing would read as a fault.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.scoring import ScoreResult
from sf_housing.shortlist_estimate import estimate_shortlist_counts


STOPS = tuple(range(30, 100, 5))


def store(repository: Repository, index: int, price: int | None, neighborhood: str,
          kind: str = "whole_unit", score: int = 70) -> None:
    # The words matter: upsert_listing re-classifies from the text, so a home
    # meant to be a room has to read like one.
    if kind == "room":
        title = f"Private room in a shared flat in {neighborhood}"
        summary = "A private room in a shared flat, nine month lease."
    else:
        title = f"An entire studio in {neighborhood}"
        summary = "A whole studio, entire place to yourself, nine month lease."
    repository.upsert_listing(
        ListingCandidate(
            platform="Test",
            source_id=f"home-{index}",
            title=title,
            original_url=f"https://example.test/home-{index}",
            price=price,
            neighborhood=neighborhood,
            listing_type="apartment",
            summary=summary,
            housing_kind=kind,
        ),
        ScoreResult(score, ["Stored"], "", {}),
    )


@pytest.fixture
def stocked(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    for index in range(40):
        store(repository, index, 1200 + index * 100, "Mission", score=30 + index)
    return repository


# The profile format the app actually stores. The legacy mapping is a
# compatibility view and its budget key does not reach scoring, so a test that
# edited it would be editing nothing.
BASE_PROFILE = {
    "state": "active",
    "enabled_paths": ["studio"],
    "budgets": {"studio": {"maximum_monthly": 6000, "ideal_monthly": 1500}},
    "geography": {"anywhere_in_sf": True, "dream": [], "strong": [], "okay": [], "avoid": []},
    "move_in": {"flexible": True},
    "lease": {"minimum_months": 6, "maximum_months": 18},
    "room_household": {"private_room_required": False, "maximum_people": None},
    "preferences": {"furnished": "nice", "laundry": "nice", "natural_light": "nice"},
}


def deal_with(**changes):
    profile = copy.deepcopy(BASE_PROFILE)
    profile.update(changes)
    return parse_preferences(
        yaml.safe_dump({"profile_version": 1, "profile": profile})
    )


def budget_of(maximum: int) -> dict:
    return {"studio": {"maximum_monthly": maximum, "ideal_monthly": 1200}}


def test_the_count_follows_the_deal_in_hand_not_the_one_on_disk(stocked: Repository) -> None:
    """The regression. Stored scores answer for the saved deal; a deal being
    edited has no stored scores at all."""
    generous = estimate_shortlist_counts(
        stocked, deal_with(budgets=budget_of(6000)), STOPS
    )
    stingy = estimate_shortlist_counts(
        stocked, deal_with(budgets=budget_of(1500)), STOPS
    )

    assert generous.counts[50] != stingy.counts[50], "the budget changed nothing"
    assert generous.counts[50] > stingy.counts[50]


def test_a_pool_that_fits_under_the_ceiling_is_counted_exactly(stocked: Repository) -> None:
    estimate = estimate_shortlist_counts(stocked, deal_with(), STOPS, ceiling=900)

    assert estimate.exact is True
    assert estimate.pool == 40


def test_a_pool_over_the_ceiling_is_sampled_and_says_so(stocked: Repository) -> None:
    """A number the page presents as certain has to be one, so the estimate
    carries whether it is."""
    estimate = estimate_shortlist_counts(stocked, deal_with(), STOPS, ceiling=10)

    assert estimate.exact is False
    assert estimate.pool == 40


def test_the_sampled_count_does_not_disagree_with_itself(stocked: Repository) -> None:
    """Drawn evenly rather than randomly. A count that flickered between two
    keystrokes that changed nothing would read as a fault in the app."""
    first = estimate_shortlist_counts(stocked, deal_with(), STOPS, ceiling=10)
    second = estimate_shortlist_counts(stocked, deal_with(), STOPS, ceiling=10)

    assert first.counts == second.counts


def test_an_empty_pool_falls_back_to_what_the_market_usually_holds(tmp_path: Path) -> None:
    """Nought is true of an empty database and useless to somebody writing a
    deal, who reads it as a verdict on their answers. With a deal worth
    reasoning about, the estimate comes from the market instead, and says so.
    """
    empty = Repository(tmp_path / "empty.sqlite3")
    empty.initialize()

    estimate = estimate_shortlist_counts(empty, deal_with(), STOPS)

    assert estimate.from_market is True
    assert estimate.exact is False, "a guess must never be presented as a count"
    assert estimate.pool == 0
    assert estimate.counts[50] > 0
    assert estimate.counts[50] >= estimate.counts[90], "a higher bar cannot find more"


def test_a_half_written_deal_is_left_uncounted() -> None:
    """The estimate runs on every keystroke, so it meets deals that are not
    finished -- a saved one always has a home type, a draft halfway through
    does not. Half a deal is not enough to reason from, and inventing a number
    for it would be worse than saying nothing.
    """
    from sf_housing.deal_profile import deal_profile_from_form
    from sf_housing.market_prior import estimate_counts

    blank = deal_profile_from_form({}, state="draft")
    assert estimate_counts(blank, STOPS) == {}

    # A home type chosen but no budget typed yet is still not enough.
    no_budget = deal_profile_from_form({"housing_paths": "studio"}, state="draft")
    assert estimate_counts(no_budget, STOPS) == {}


def test_only_the_home_shapes_this_deal_shows_are_counted(tmp_path: Path) -> None:
    """A deal for rooms alone counted whole units it would never display, and
    the slider read higher than the page."""
    repository = Repository(tmp_path / "mixed.sqlite3")
    repository.initialize()
    for index in range(10):
        store(repository, index, 1500, "Mission", kind="whole_unit")
    for index in range(10, 15):
        store(repository, index, 1500, "Mission", kind="room")

    both = deal_with(
        enabled_paths=["studio", "private_room"],
        budgets={**budget_of(6000), "private_room": {"maximum_monthly": 4000, "ideal_monthly": 1200}},
    )
    whole = estimate_shortlist_counts(repository, both, STOPS, kinds=["whole_unit"])
    rooms = estimate_shortlist_counts(repository, both, STOPS, kinds=["room"])

    assert whole.pool == 10
    assert rooms.pool == 5


def test_a_higher_cut_off_never_shortlists_more_than_a_lower_one(stocked: Repository) -> None:
    """The counts are read off one pass, so they have to be monotone."""
    counts = estimate_shortlist_counts(stocked, deal_with(), STOPS).counts

    values = [counts[stop] for stop in STOPS]
    assert values == sorted(values, reverse=True)


def test_the_count_is_silent_before_the_first_search(tmp_path: Path) -> None:
    """A blank install has no pool, so every cut-off counts nought.

    Shown as "0 homes" while somebody is still writing their deal, that reads
    as a verdict on the deal -- the first person to set this up on another Mac
    watched it say "70 and up, 0 homes" and reasonably thought their answers
    had ruled everything out. Nothing had been collected yet.
    """
    from sf_housing.app import create_app
    from fastapi.testclient import TestClient
    from sf_housing.settings import Settings

    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert 'data-cutoff-pool="0"' in page, "the page does not say the pool is empty"
    assert "0 homes" not in page, "a blank install is telling somebody it found nothing"


def test_a_blank_install_with_a_deal_offers_an_estimate_not_a_nought(tmp_path: Path) -> None:
    """The moment this is all about: the deal is written, nothing has been
    searched, and the slider has to say something useful.

    It says roughly what a search like this usually finds, marked as "about" so
    nobody mistakes it for a count of homes that exist somewhere.
    """
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.preferences import save_deal_profile
    from sf_housing.settings import Settings

    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )
    data.mkdir(parents=True, exist_ok=True)
    (data / "config").mkdir(parents=True, exist_ok=True)
    save_deal_profile(settings.preferences_path, deal_with().deal_profile)

    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert 'data-cutoff-pool="0"' in page, "something was collected; this is not a blank install"
    assert "about" in page, "the estimate is not offered as an estimate"
    assert "0 homes" not in page, "still telling somebody their deal finds nothing"


def stocked_as_a_real_board(repository: Repository, saved_budget: int) -> None:
    """Homes the saved deal already ruled out, stored the way a scan stores them.

    ``store`` hands ``upsert_listing`` an empty ``details`` dict, so every home
    it writes lands as ``eligible``. A board that has actually been scanned does
    not look like that: eligibility is worked out against the deal that was
    saved at the time, and on a real install most rows fail it.
    """
    for index in range(40):
        price = 1200 + index * 100
        ruled_out = price > saved_budget
        repository.upsert_listing(
            ListingCandidate(
                platform="Test",
                source_id=f"home-{index}",
                title=f"An entire studio in Mission",
                original_url=f"https://example.test/home-{index}",
                price=price,
                neighborhood="Mission",
                listing_type="apartment",
                summary="A whole studio, entire place to yourself, nine month lease.",
                housing_kind="whole_unit",
            ),
            ScoreResult(
                0 if ruled_out else 70,
                ["Stored"],
                "",
                {},
                eligibility="ineligible" if ruled_out else "eligible",
            ),
        )


def test_raising_the_budget_finds_the_homes_the_old_budget_ruled_out(tmp_path: Path) -> None:
    """The regression. Eligibility is a verdict on the *saved* deal, stored on
    the row, so filtering the pool by it hid every home the old budget had
    priced out. The count stopped moving the moment the draft went above the
    saved budget: on a real board it sat at 423 while the true answer climbed
    past 1,300, telling somebody that raising their budget found nothing.
    """
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    stocked_as_a_real_board(repository, saved_budget=3000)

    within = estimate_shortlist_counts(repository, deal_with(budgets=budget_of(3000)), STOPS)
    beyond = estimate_shortlist_counts(repository, deal_with(budgets=budget_of(5500)), STOPS)

    assert beyond.counts[50] > within.counts[50], (
        "the draft budget cleared homes the saved deal had ruled out, "
        "and the count did not move"
    )


def test_a_home_the_draft_rules_out_is_not_counted(tmp_path: Path) -> None:
    """The other half of dropping the stored verdict. The pool now carries
    homes the deal may well refuse, so the refusing has to happen here -- on
    the deal in hand -- or a home priced far above the draft budget would be
    counted simply for scoring well under an older, richer one.
    """
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    # Stored as a fine home, which is what a generous saved deal would have
    # written down. The draft below cannot afford it.
    store(repository, 0, 12000, "Mission", score=95)
    store(repository, 1, 1400, "Mission", score=95)

    counts = estimate_shortlist_counts(
        repository, deal_with(budgets=budget_of(1500)), STOPS
    ).counts

    assert counts[30] == 1, "the home priced at $12,000 was counted against a $1,500 deal"




AREAS = ["Mission", "Castro", "Bernal Heights", "Noe Valley",
         "SoMa", "Sunset", "Richmond", "Excelsior"]


def lopsided_board(repository: Repository) -> None:
    """A board shaped like a real one rather than a tidy grid.

    Rents bunch at the cheap end and thin out at the dear end, and areas are
    dealt round so homes do not all score alike.

    Stored scores are skewed on purpose. Rent bands are quantiles, so they hold
    equal numbers whatever the rents are; the stored score is the only axis
    that can make one band far bigger than another, and a fixture that spreads
    scores evenly cannot tell a proportional share of the samples from an equal
    one. Most homes here suited the saved deal poorly, as on a real board,
    where 6,442 of 6,935 were ruled out.
    """
    index = 0
    for rent, count in ((1200, 700), (1800, 300), (2600, 140),
                        (3600, 70), (5200, 60), (9000, 30)):
        for step in range(count):
            # Four homes in five sit low; the rest fan out over the range.
            score = (index * 7) % 25 if index % 5 else min(99, 25 + (index * 11) % 75)
            store(repository, index, rent + step * 5, AREAS[index % len(AREAS)],
                  score=score)
            index += 1
    # Homes that never said what they cost. They answer a budget differently
    # from any home that states one, so they are their own band.
    for step in range(60):
        store(repository, index, None, AREAS[index % len(AREAS)],
              score=(index * 13) % 100)
        index += 1


def tiered_deal(maximum: int):
    # Areas in three tiers, so the homes spread across several scores instead
    # of landing on one.
    return deal_with(
        budgets=budget_of(maximum),
        geography={"anywhere_in_sf": False, "dream": AREAS[:2],
                   "strong": AREAS[2:4], "okay": AREAS[4:6], "avoid": []},
    )


def test_the_sampled_count_agrees_with_counting_the_whole_pool(tmp_path: Path) -> None:
    """The sample has to answer what the whole pool would have answered.

    A broad guard rather than a proof of any one choice in the sampler: a
    fixture of this size cannot separate the banding decisions, which were
    settled against a real board of 6,384 homes and are recorded where they are
    made. What this does catch is the sample drifting away from the pool it
    stands for, whatever the cause.
    """
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    lopsided_board(repository)
    deal = tiered_deal(4000)

    whole = estimate_shortlist_counts(repository, deal, STOPS, ceiling=10**9)
    sample = estimate_shortlist_counts(repository, deal, STOPS, ceiling=200)

    assert whole.exact and not sample.exact, "the two readings must differ in method"
    for stop in STOPS:
        truth = whole.counts[stop]
        if truth < 20:
            continue
        drift = abs(sample.counts[stop] - truth) / truth
        assert drift <= 0.15, (
            f"at a cut-off of {stop} the sample read {sample.counts[stop]} "
            f"where the whole pool holds {truth}"
        )


def test_a_crowded_band_is_sampled_more_than_a_sparse_one(tmp_path: Path) -> None:
    """Shares used to be handed out equally, which is only fair if the bands
    are the same size. They are not: a band of 700 homes got the same few as a
    band of 30 and spoke for all 700 on that evidence.
    """
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    lopsided_board(repository)

    sampled, sizes, exact = repository.shortlist_pool(kinds=["whole_unit"], ceiling=400)

    assert not exact
    taken: dict[int, int] = {}
    for band, _ in sampled:
        taken[band] = taken.get(band, 0) + 1
    assert set(taken) == set(sizes), f"bands {sorted(set(sizes) - set(taken))} went unsampled"
    speaks_for = [sizes[band] / taken[band] for band in sizes]
    assert max(speaks_for) <= 2 * min(speaks_for), (
        f"one sampled home stands for {max(speaks_for):.1f} others while "
        f"another stands for {min(speaks_for):.1f}"
    )


def test_the_sample_is_not_taken_from_the_bottom_of_a_band(tmp_path: Path) -> None:
    """Each band is walked along its stored score. The walk used to start on
    the band's very first home and stop a full step short of its last, so every
    band was read from its weaker end -- and a strict cut-off, which is asking
    about the strong end, came back low: 195 homes at 90 against a true 238.
    """
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    # One rent and one stored score, so the pool is a single band and the
    # arithmetic of which homes get taken is all that is on trial.
    for index in range(1000):
        store(repository, index, 2000, "Mission", score=50)

    sampled, sizes, exact = repository.shortlist_pool(kinds=["whole_unit"], ceiling=100)

    assert not exact and len(sizes) == 1, "this fixture is meant to make one band"
    positions = sorted(int(listing.source_id.removeprefix("home-")) for _, listing in sampled)
    untouched_below = positions[0]
    untouched_above = 999 - positions[-1]
    assert abs(untouched_below - untouched_above) <= 1, (
        f"the sample leaves {untouched_below} homes untouched at the bottom of "
        f"the band and {untouched_above} at the top"
    )
