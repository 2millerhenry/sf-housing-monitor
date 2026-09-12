"""How many homes a deal would shortlist, answered while it is still being typed.

The number beside the cut-off slider came from the scores already in the
database, which are the scores of the deal as *saved*. Editing the deal --
dropping a neighbourhood, lowering a budget -- left the number standing at the
answer for a deal that no longer existed, and it only moved again once the
whole form had been submitted and every stored home reranked.

Answering it means scoring the pool against the deal in hand. The pool is much
smaller than the board it comes from: on a real install, 351 homes were live
and eligible out of 5,597 stored, and scoring 351 takes about a third of a
second. So the usual answer is exact. Only a pool bigger than the ceiling is
sampled, which keeps the cost bounded on an install where the pool has grown
past what a keystroke can wait for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from .classification import classify_listing
from .database import Repository
from .market_prior import estimate_counts as market_counts
from .preferences import Preferences
from .scoring import score_listing
from .sources import facebook_coordinate_neighborhood, visible_sf_area_hint


# What one keystroke may cost. 900 homes is about eight tenths of a second of
# scoring, which is already more than this should ever spend; past it the pool
# is sampled instead of scored whole.
POOL_CEILING = 900


@dataclass(frozen=True, slots=True)
class ShortlistEstimate:
    counts: dict[int, int]
    exact: bool
    pool: int
    # True when there was no pool to count and the numbers come from what this
    # kind of search usually turns up rather than from anything collected.
    from_market: bool = False


def _prepared(listing):
    """The same tidying a real rescore does before scoring, and no other.

    A pool scored differently from the board it stands for would be an accurate
    answer to the wrong question.
    """
    if listing.platform == "Facebook Marketplace":
        mapped = facebook_coordinate_neighborhood(listing.neighborhood)
        if mapped:
            listing = replace(listing, neighborhood=mapped)
    elif listing.platform == "Furnished Finder" and not listing.neighborhood:
        area = visible_sf_area_hint(
            " ".join(filter(None, [listing.title, listing.summary, listing.listing_type]))
        )
        if area:
            listing = replace(listing, neighborhood=area)
    return classify_listing(listing)


def estimate_shortlist_counts(
    repository: Repository,
    preferences: Preferences,
    thresholds: Sequence[int],
    *,
    kinds: Sequence[str] = (),
    ceiling: int = POOL_CEILING,
) -> ShortlistEstimate:
    """How many homes each cut-off would shortlist under this deal.

    Exact whenever the pool fits under the ceiling, which is the ordinary case;
    a weighted estimate from a stratified sample when it does not. ``exact``
    says which happened, because a number the page presents as certain has to
    be one.
    """
    stops = [int(threshold) for threshold in thresholds]
    sampled, sizes, exact = repository.shortlist_pool(kinds=kinds, ceiling=ceiling)
    if not sampled:
        # Nothing has been collected yet, so there is nothing to count. Saying
        # nought here reads as a verdict on the deal rather than on the empty
        # database, so this says roughly what a search like this usually finds.
        prior = market_counts(preferences.deal_profile, stops)
        if prior:
            return ShortlistEstimate(prior, False, 0, from_market=True)
        return ShortlistEstimate({stop: 0 for stop in stops}, True, 0)

    # Scored once and then read at every stop: nineteen cut-offs are nineteen
    # questions about one pass, not nineteen passes.
    #
    # A home the deal rules out outright never reaches the shortlist whatever
    # the cut-off, so it is carried as None rather than dropped. It still has to
    # be counted in its band: the band's share is what gets multiplied back up
    # by the band's true size, and dropping the rejects would inflate it.
    by_band: dict[int, list[int | None]] = {}
    for band, listing in sampled:
        result = score_listing(_prepared(listing), preferences)
        by_band.setdefault(band, []).append(
            None if result.eligibility == "ineligible" else int(result.score)
        )

    counts: dict[int, int] = {}
    for stop in stops:
        if exact:
            counts[stop] = sum(
                1
                for scores in by_band.values()
                for value in scores
                if value is not None and value >= stop
            )
            continue
        total = 0.0
        for band, scores in by_band.items():
            if scores:
                clearing = sum(
                    1 for value in scores if value is not None and value >= stop
                ) / len(scores)
                total += clearing * sizes.get(band, 0)
        counts[stop] = int(round(total))
    return ShortlistEstimate(counts, exact, sum(sizes.values()))
