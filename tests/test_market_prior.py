"""What the slider says on an install that has never searched.

A blank database counts nought at every cut-off, and the first person to set
this up on a Mac that was not the author's read "70 and up, 0 homes" as a
verdict on the deal they had just written rather than on the empty board. So
the count falls back to what a search like this usually turns up.

That estimate is the first number a new person sees, and they have no way to
check it. These pin the properties it has to hold whatever combination of
answers somebody gives -- every one of which was verified against a real board
of 6,935 homes, and one fresh install whose first search collected 4,596 and
shortlisted 417.

The numbers themselves are deliberately low: promising fifteen and finding
thirty is a good surprise, and the reverse makes somebody close the tab. So
these check direction, ordering and plausibility, not exact values, which are
free to move as the market is re-measured.
"""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest
import yaml

from sf_housing.market_prior import FRESH_SCAN_POOL, SIZE_SHARE, estimate_counts
from sf_housing.preferences import parse_preferences


STOPS = tuple(range(30, 100, 5))
PATHS = tuple(SIZE_SHARE)
AREAS = ["Mission District", "Bernal Heights", "Castro", "Noe Valley",
         "Alamo Square", "Duboce Triangle", "Hayes Valley", "SoMa"]


def profile(paths, budget, *, areas=0, anywhere=False):
    document = {
        "profile_version": 1,
        "profile": {
            "state": "active",
            "enabled_paths": list(paths),
            "budgets": {
                path: {"maximum_monthly": budget, "ideal_monthly": int(budget * 0.75)}
                for path in paths
            },
            "geography": {"anywhere_in_sf": anywhere, "dream": AREAS[:areas],
                          "strong": [], "okay": [], "avoid": []},
            "move_in": {"flexible": True},
            "lease": {"minimum_months": 6, "maximum_months": 18},
            "room_household": {"private_room_required": "private_room" in paths,
                               "maximum_people": None},
            "preferences": {"furnished": "nice", "laundry": "nice",
                            "natural_light": "nice"},
        },
    }
    return parse_preferences(yaml.safe_dump(document)).deal_profile


def at(paths, budget, stop=60, *, areas=0, anywhere=False):
    return estimate_counts(profile(paths, budget, areas=areas, anywhere=anywhere), STOPS)[stop]


def test_more_money_never_finds_fewer_homes() -> None:
    """The estimate has to move the way the answer moves. Raising a budget and
    watching the number fall would read as a fault in the app, and rightly."""
    for paths in (("private_room",), ("studio", "one_bedroom"), PATHS):
        counts = [at(paths, budget, anywhere=True)
                  for budget in range(1000, 15_001, 500)]
        assert counts == sorted(counts), f"{paths}: raising the budget lost homes"


def test_naming_more_neighborhoods_finds_more_of_them() -> None:
    """Naming an area does not rule the rest out -- they keep their place in
    Near matches -- so each extra area can only add.

    It has to actually add. ``areas`` is a mapping of tier to the places in it,
    and reading its length counted the tiers: four, always, however many places
    somebody picked. Every estimate came back as though exactly four areas had
    been named, so the number never moved as areas were chosen, and a test that
    only asked for "never fewer" was satisfied by a number that never moved.
    """
    counts = [at(("one_bedroom",), 4000, areas=n) for n in range(1, len(AREAS) + 1)]
    assert counts == sorted(counts), "naming another neighbourhood lost homes"
    assert counts[-1] > counts[0], (
        f"naming {len(AREAS)} areas estimates the same {counts[0]} homes as naming one"
    )


def test_anywhere_in_sf_is_never_worse_than_naming_places() -> None:
    for n in range(1, len(AREAS) + 1):
        named = at(("studio", "one_bedroom"), 3500, areas=n)
        anywhere = at(("studio", "one_bedroom"), 3500, anywhere=True)
        assert anywhere >= named, f"{n} named areas beat the whole city"


def test_looking_at_another_kind_of_home_never_finds_fewer() -> None:
    """Every size is its own slice of the market, so adding one adds its slice."""
    for extra in PATHS:
        for base in (("studio",), ("one_bedroom",), ("private_room", "studio")):
            if extra in base:
                continue
            assert at((*base, extra), 4000, anywhere=True) >= at(base, 4000, anywhere=True), (
                f"adding {extra} to {base} lost homes"
            )


def test_a_stricter_cut_off_never_shortlists_more() -> None:
    for paths in (("private_room",), ("two_bedroom", "three_bedroom"), PATHS):
        counts = [at(paths, 5000, stop, anywhere=True) for stop in STOPS]
        assert counts == sorted(counts, reverse=True), f"{paths}: a stricter cut-off found more"


def test_no_combination_of_answers_promises_more_than_one_search_collects() -> None:
    """The estimate stands for one search. Promising more homes than a search
    brings home would be promising something the app cannot deliver.

    Every path subset, against a spread of budgets and geographies.
    """
    worst = 0
    for size in range(1, len(PATHS) + 1):
        for paths in itertools.combinations(PATHS, size):
            for budget in (1000, 2500, 5000, 10_000, 25_000, 100_000):
                for areas, anywhere in ((0, True), (1, False), (8, False)):
                    count = at(paths, budget, 30, areas=areas, anywhere=anywhere)
                    assert count >= 0, f"{paths} at ${budget} estimated {count}"
                    worst = max(worst, count)
    assert worst <= FRESH_SCAN_POOL, (
        f"the broadest deal promises {worst} homes from a search of {FRESH_SCAN_POOL}"
    )


def test_a_deal_nobody_could_estimate_says_nothing_rather_than_nought() -> None:
    """Silence and nought read very differently under a slider. A deal with no
    budget yet is not a deal that finds nothing; it is a deal still being
    written, and the readout stays blank until there is something to say."""
    # Built with replace rather than through parse_preferences, which refuses a
    # deal this incomplete -- the live preview meets exactly these drafts.
    written = profile(("studio",), 3000, anywhere=True)
    assert estimate_counts(replace(written, enabled_paths=()), STOPS) == {}
    assert estimate_counts(replace(written, budgets={}), STOPS) == {}


def test_a_room_at_the_going_rate_is_not_treated_as_the_bottom_of_the_market() -> None:
    """The regression behind measuring rents separately for each size. One
    curve for the whole market put a $1,500 room in the cheapest 7% -- the
    collected median is $4,024, which is a flat, not a room -- and a room
    hunter on a perfectly ordinary budget was told three homes.
    """
    ordinary = at(("private_room",), 1500, anywhere=True)
    assert ordinary >= 10, f"a $1,500 room budget was told about {ordinary} homes"
    # Still the middle of the room market, not the top of it.
    assert ordinary < at(("private_room",), 3000, anywhere=True)


def test_people_splitting_a_home_are_counted_on_what_they_can_pay_together() -> None:
    """The regression. On a home to split, the form asks for each person's
    share -- "$2,700 each for 2 people" -- and the estimate read that share
    against whole-unit rents, pricing a couple as though they were renting a
    two-bedroom alone on $2,700. Two people at $2,700 can pay $5,400, which
    reaches a third of the two-bedrooms on a real board where $2,700 reaches a
    sixteenth. They were told about five homes where a search holds hundreds.
    """
    shared = profile(("two_bedroom",), 2700, anywhere=True)
    shared.budgets["two_bedroom"] = replace(shared.budgets["two_bedroom"], occupants=2)
    alone = profile(("two_bedroom",), 2700, anywhere=True)
    alone.budgets["two_bedroom"] = replace(alone.budgets["two_bedroom"], occupants=1)

    together = estimate_counts(shared, STOPS)[60]
    by_oneself = estimate_counts(alone, STOPS)[60]

    assert together > by_oneself * 3, (
        f"two people at $2,700 each are offered {together} homes and one person "
        f"at $2,700 is offered {by_oneself}; between them they can pay $5,400"
    )


@pytest.mark.parametrize("paths,budget,areas,anywhere", [
    (("studio", "one_bedroom"), 3800, 0, True),
    (("private_room",), 1500, 0, True),
    (("one_bedroom",), 3000, 1, False),
    (PATHS, 6000, 3, False),
])
def test_an_ordinary_deal_lands_somewhere_a_person_would_believe(
    paths, budget, areas, anywhere
) -> None:
    """Not a measurement -- a sanity rail. A number in single figures reads as
    "this app found nothing" and a number in the thousands reads as a machine
    that has not understood the question. Ordinary deals belong between."""
    count = at(paths, budget, 60, areas=areas, anywhere=anywhere)
    assert 5 <= count <= 1000, f"{paths} at ${budget} estimated {count} homes"
