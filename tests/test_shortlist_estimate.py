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


def store(repository: Repository, index: int, price: int, neighborhood: str,
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


def test_an_empty_pool_is_nought_at_every_cut_off(tmp_path: Path) -> None:
    empty = Repository(tmp_path / "empty.sqlite3")
    empty.initialize()

    estimate = estimate_shortlist_counts(empty, deal_with(), STOPS)

    assert estimate.exact is True
    assert set(estimate.counts.values()) == {0}


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
