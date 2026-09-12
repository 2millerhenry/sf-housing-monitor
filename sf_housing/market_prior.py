"""What a search like this usually turns up, before anything has been searched.

A blank installation has collected nothing, so every cut-off honestly counts
nought. Showing that to somebody in the middle of writing their deal reads as a
verdict on their answers rather than on the empty database, and the first person
to set this up on a machine other than the author's read it exactly that way.

There is no data to measure, so this estimates from what the San Francisco
market looked like on a real board. Every number below was measured rather than
guessed, and the estimate is deliberately pitched low: promising fifteen and
finding thirty is a good surprise, and the reverse is the kind that makes
somebody close the tab.

Measured on a board of 6,935 homes collected by this app, and against one
fresh installation whose first search collected 4,596 homes and put 417 on the
shortlist at a cut-off of 60 with a broad whole-unit deal.

Checked against that board a deal at a time, scoring every home afresh rather
than reading the scores already stored, which answer for whatever deal was
saved at the time. It comes in under what the board holds every time: a little
over three quarters of it for a search naming neighbourhoods, and about a
quarter for one looking anywhere in San Francisco. The shape holds -- adding
two- and three-bedrooms to a studio-and-one-bedroom search multiplies the count
by 1.40 here against 1.33 to 1.48 on the board.

That leaves one thing known and not yet acted on. Against the same board, a
search of the whole city clears about 0.92 of the homes matching on size and
rent at a cut-off of 60, and this estimates 0.39 -- so anywhere-in-SF searches
are pitched two and a half times lower than naming areas, which land almost
exactly right. Correcting it means moving the cut-off curve, which is anchored
on the one real first search there is a record of, and one board is not enough
to move it on.
"""

from __future__ import annotations

from typing import Sequence

from .deal_profile import DealProfile


# One search collected 4,596 homes. Four thousand is that, rounded down, so the
# estimate starts behind the evidence rather than level with it.
FRESH_SCAN_POOL = 4_000

# Share of collected homes by the size they state, from 5,652 that stated one:
# one-bedroom 2,337, two-bedroom 1,388, studio 1,316, three-bedroom 525,
# four-bedroom 86. Rooms are counted separately: 490 of 6,935 were rooms in a
# shared home, which is the 7% below.
SIZE_SHARE = {
    "private_room": 0.07,
    "studio": 0.23,
    "one_bedroom": 0.41,
    "two_bedroom": 0.25,
    "three_bedroom": 0.09,
    "four_bedroom": 0.02,
}

# What fraction of homes of each size sit at or under a given rent. One curve
# for the whole market was the first attempt and it was badly wrong at both
# ends: the collected median is $4,024, which made a $1,500 room look like the
# bottom 7% of the market when it is really the middle of the room market, and
# made a $6,000 two-bedroom look generous when it is the median.
#
# Measured per size on the same board: 1,253 studios, 2,283 one-bedrooms,
# 1,376 two-bedrooms, 499 three-bedrooms, 76 four-bedrooms, and 475 rooms that
# published a rent.
RENT_CDF = {
    # median $1,450
    "private_room": ((850, 0.10), (1_100, 0.25), (1_200, 0.33), (1_450, 0.50),
                     (1_500, 0.55), (1_800, 0.68), (2_200, 0.76), (2_800, 0.87), (4_000, 0.97)),
    # median $2,708
    "studio": ((1_500, 0.05), (2_000, 0.22), (2_708, 0.50), (3_000, 0.62),
               (4_000, 0.86), (6_000, 0.99), (9_000, 1.00)),
    # median $4,200
    "one_bedroom": ((2_000, 0.09), (3_000, 0.20), (4_000, 0.45), (4_200, 0.50),
                    (6_000, 0.92), (8_000, 0.99), (12_000, 1.00)),
    # median $6,295
    "two_bedroom": ((2_000, 0.04), (3_000, 0.07), (4_000, 0.16), (6_000, 0.45),
                    (6_295, 0.50), (8_000, 0.87), (12_000, 0.99)),
    # median $6,800
    "three_bedroom": ((2_000, 0.06), (3_000, 0.07), (4_000, 0.11), (6_000, 0.38),
                      (6_800, 0.50), (8_000, 0.62), (14_000, 0.97)),
    # median $9,499
    "four_bedroom": ((3_000, 0.09), (4_000, 0.09), (6_000, 0.17), (8_000, 0.38),
                     (9_499, 0.50), (16_000, 0.95)),
}

# Matching on size and rent is not the same as reaching the shortlist, and how
# far apart those two are depends on what you are looking for.
#
# Measured on a board of 6,935 homes, one deal at a time: each size offered its
# own median rent, with the number of people the form fills in by default, and
# counting what share of the homes matching on size and rent then reached a
# cut-off of 60. Against the one-bedroom case, which anchors this at 1.0:
#
#   studio 0.95, two-bedroom 1.03, three-bedroom 0.94, four-bedroom 1.03,
#   private room 0.70, from 629, 695, 252, 39 and 247 homes respectively.
#
# Homes to split were held at 0.3 here before, on a reading of one board where
# 300 whole small homes reached a cut-off of 60 against 29 homes to split. That
# gap was rent, not friction: at the $3,000 that deal allowed, a three-bedroom
# is in the cheapest 7% of three-bedrooms, and size and rent alone account for
# the whole of it -- they predict a thirteenth, and a tenth was observed. The
# penalty was charging a second time for something the rent curves had already
# charged for, and it left a couple looking to share told a twentieth of what
# was there.
#
# Every figure is set at or under what was measured, because under-promising is
# the point. The room number is the one to keep an eye on: 247 homes is the
# thinnest evidence here, and rooms are advertised more loosely than flats.
PATH_CLEARANCE = {
    "private_room": 0.65,
    "studio": 0.90,
    "one_bedroom": 1.0,
    "two_bedroom": 0.90,
    "three_bedroom": 0.90,
    "four_bedroom": 0.90,
}

# Naming neighborhoods does not rule anything else out -- homes elsewhere keep
# their place in Near matches -- but it does hold their score down. Measured on
# the same board at a cut-off of 60: 300 homes with anywhere-in-SF against 139
# with one named area, and 156 with five. So a named search is a little under
# half as productive, rising slowly as more areas are named.
ANYWHERE_FACTOR = 1.0
ONE_AREA_FACTOR = 0.45
PER_EXTRA_AREA = 0.02
MAX_AREA_FACTOR = 0.60

# Of the homes that match on size and budget, the share that reach each
# cut-off. Anchored at 60, where a fresh install put 417 homes on the shortlist
# out of roughly 1,075 that matched its sizes and budget. The shape above and
# below comes from the same board measured at every stop, taking the steeper of
# the two geographies seen, because under-promising is the point.
CLEARANCE = {30: 0.55, 35: 0.52, 40: 0.50, 45: 0.47, 50: 0.45, 55: 0.42,
             60: 0.39, 65: 0.36, 70: 0.33, 75: 0.25, 80: 0.17, 85: 0.11,
             90: 0.07, 95: 0.03}

# Everything above is a central estimate. This is the margin that turns it into
# a conservative one.
CAUTION = 0.75


def _rent_share(path: str, budget: int) -> float:
    """The share of homes of this size at or under this rent, interpolated."""
    curve = RENT_CDF[path]
    if budget <= curve[0][0]:
        # Below the cheapest point measured, fall away rather than flatten: a
        # budget under the bottom of a market finds very little, not a tenth.
        return curve[0][1] * max(0.0, budget / curve[0][0]) ** 2
    for (low_rent, low), (high_rent, high) in zip(curve, curve[1:]):
        if budget <= high_rent:
            span = high_rent - low_rent
            return low + (high - low) * ((budget - low_rent) / span if span else 0)
    return curve[-1][1]


def _area_factor(profile: DealProfile) -> float:
    if profile.anywhere_in_sf:
        return ANYWHERE_FACTOR
    # areas is a mapping of tier to the places in it, not a list of places.
    # len() of it counted the tiers -- four, always, whoever was searching --
    # so every estimate came back as though exactly four areas had been named
    # and naming more made no difference to the number on screen.
    chosen = {
        place.casefold()
        for tier in ("dream", "strong", "okay")
        for place in (profile.areas or {}).get(tier, ())
        if str(place).strip()
    }
    named = len(chosen)
    if named <= 0:
        # No areas named and not anywhere: the form is half-written. Treat it
        # as the narrowest case rather than the broadest.
        return ONE_AREA_FACTOR
    return min(MAX_AREA_FACTOR, ONE_AREA_FACTOR + PER_EXTRA_AREA * (named - 1))


def estimate_counts(profile: DealProfile, thresholds: Sequence[int]) -> dict[int, int]:
    """Roughly what each cut-off would shortlist, from the market rather than a board.

    Empty when there is not enough of a deal to reason about, which is the
    honest answer while somebody is still choosing what they are looking for.
    """
    paths = [p for p in (profile.enabled_paths or ()) if p in SIZE_SHARE]
    if not paths:
        return {}

    matched = 0.0
    for path in paths:
        budget = profile.budgets.get(path)
        # What the household can pay for the whole home, which is what the rent
        # curves above are curves of. For a home to split, ``maximum_monthly``
        # is each person's share -- the form asks "$X each for N people" -- and
        # reading that share against whole-unit rents priced a couple at half a
        # flat. Two people at $2,700 each can pay $5,400, which reaches 36% of
        # two-bedrooms; $2,700 reaches 6% of them. They were told about five
        # homes where the board held enough for three hundred.
        maximum = getattr(budget, "total_maximum", None) if budget else None
        if not maximum:
            continue
        # Each home type is its own slice of the market, with its own rents and
        # its own odds of ever reaching a shortlist.
        matched += (
            FRESH_SCAN_POOL
            * SIZE_SHARE[path]
            * _rent_share(path, int(maximum))
            * PATH_CLEARANCE[path]
        )
    if matched <= 0:
        return {}

    matched *= _area_factor(profile) * CAUTION
    counts: dict[int, int] = {}
    for threshold in thresholds:
        stop = int(threshold)
        nearest = min(CLEARANCE, key=lambda known: abs(known - stop))
        counts[stop] = int(matched * CLEARANCE[nearest])
    return counts
