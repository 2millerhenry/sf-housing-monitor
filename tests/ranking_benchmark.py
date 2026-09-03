"""Deterministic, profile-level benchmark for the housing ranker.

The benchmark's expected relevance is intentionally defined as a small
admissibility policy derived from Henry's written housing deal.  It does not
reuse production weights, criterion helpers, or score details.  This lets it
catch extraction mistakes and trade-off mistakes instead of merely asserting
that the implementation agrees with itself.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import mean
from typing import Iterable

from sf_housing.models import ListingCandidate
from sf_housing.preferences import load_preferences
from tests.paths import BENCHMARK_PROFILE
from sf_housing.scoring import score_listing


PROFILE_PATH = BENCHMARK_PROFILE


@dataclass(frozen=True, slots=True)
class Signals:
    """Semantic facts a human should take from a synthetic listing."""

    neighborhood: str = "unknown"  # ideal, preferred, acceptable, unknown, outside
    price: str = "unknown"  # sweet, wider, low, high, unknown
    privacy: str = "unknown"  # private, shared, unknown
    property_type: str = "unknown"  # preferred, apartment, complex, unknown
    lease: str = "unknown"  # flexible, ideal, maximum, long, unknown
    household: str = "unknown"  # ideal, good, over, two_other, unknown
    sunlight: str = "unknown"  # yes, no, unknown
    park: str = "unknown"  # yes, unknown
    outdoor: str = "unknown"  # yes, no, unknown
    lifestyle: str = "unknown"  # yes, unknown


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    cohort: str
    listing: ListingCandidate
    signals: Signals
    expected_score: int
    expected_main_result: bool
    rationale: str
    pair_id: str | None = None
    expected_pair_winner: bool = False


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: BenchmarkCase
    actual_score: int
    actual_main_result: bool
    concern: str


def human_relevance(signals: Signals) -> tuple[int, bool]:
    """Apply a hierarchy rather than a clone of the production weighted sum.

    Neighborhood establishes the starting tier.  Budget, room privacy and a
    non-complex home are admissibility gates.  Remaining preferences move a
    listing within its tier.  Unknown facts have no penalty.
    """

    neighborhood_tier = {
        "ideal": 76,
        "preferred": 68,
        "acceptable": 59,
        "unknown": 54,
        "outside": 24,
    }[signals.neighborhood]
    score = neighborhood_tier

    score += {"sweet": 8, "wider": 2, "low": -22, "high": -22, "unknown": 0}[signals.price]
    score += {"private": 7, "shared": -34, "unknown": 0}[signals.privacy]
    # Henry wants a nice shared flat and rejects a generic apartment complex,
    # not every privately advertised bedroom whose source taxonomy says
    # "apartment." A private-room apartment is therefore compatible; the
    # complex remains a hard mismatch.
    if signals.property_type == "apartment":
        score += 2 if signals.privacy == "private" else -4
    else:
        score += {"preferred": 5, "complex": -20, "unknown": 0}[signals.property_type]
    score += {"flexible": 5, "ideal": 4, "maximum": 0, "long": -13, "unknown": 0}[signals.lease]
    score += {"ideal": 3, "two_other": 3, "good": 1, "over": -10, "unknown": 0}[signals.household]
    score += {"yes": 2, "no": -4, "unknown": 0}[signals.sunlight]
    score += {"yes": 2, "unknown": 0}[signals.park]
    score += {"yes": 1, "no": -2, "unknown": 0}[signals.outdoor]
    score += {"yes": 2, "unknown": 0}[signals.lifestyle]

    # These are explicit user constraints, so attractive secondary details
    # cannot turn a known violation into a recommended listing.
    if signals.neighborhood == "outside":
        score = min(score, 44)
    if signals.privacy == "shared":
        score = min(score, 34)
    if signals.price in {"low", "high"}:
        score = min(score, 54)
    if signals.property_type == "complex":
        score = min(score, 49)
    if signals.household == "over":
        score = min(score, 54)
    if signals.lease == "long":
        score = min(score, 54)

    score = max(0, min(100, round(score)))
    admissible = (
        signals.neighborhood != "outside"
        and signals.privacy != "shared"
        and signals.price not in {"low", "high"}
        and signals.property_type != "complex"
        and signals.household != "over"
        and signals.lease != "long"
    )
    return score, admissible and score >= 60


def _render_listing(
    case_id: str,
    signals: Signals,
    *,
    variant: int = 0,
    neighborhood_name: str | None = None,
    title_override: str | None = None,
    summary_override: str | None = None,
    metadata_override: dict[str, str] | None = None,
) -> ListingCandidate:
    neighborhoods = {
        "ideal": ["Potrero Hill", "Duboce Triangle"],
        "preferred": ["Bernal Heights", "Mission Dolores", "Noe Valley"],
        "acceptable": ["NOPA", "Lower Haight", "Cole Valley", "Glen Park", "Inner Richmond"],
        "unknown": ["San Francisco", "SF", "City of San Francisco"],
        "outside": ["SOMA", "Bayview", "Tenderloin", "Park Merced", "Outer Sunset"],
    }
    prices = {
        "sweet": [1250, 1450, 1500, 1650, 1800],
        "wider": [900, 1100, 1950, 2150, 2280],
        "low": [650, 700, 750],
        "high": [2750, 2900, 3100],
        "unknown": [None],
    }
    privacy_title = {
        "private": ["Private bedroom opening", "Own room available", "Private room for rent"],
        "shared": ["Shared bedroom opening", "Shared room for rent", "Per-bed space available"],
        "unknown": ["Housing opening", "New home opening", "Available space"],
    }[signals.privacy][variant % 3]
    property_text = {
        "preferred": ["in a Victorian shared house", "in a townhouse", "in a shared flat"],
        "apartment": ["in an apartment", "in a modern apartment", "in an apartment unit"],
        "complex": ["in a generic apartment complex", "in a large apartment complex", "in a managed apartment complex"],
        "unknown": ["", "", ""],
    }[signals.property_type][variant % 3]
    title = title_override or " ".join(part for part in (privacy_title, property_text) if part)

    phrases: list[str] = []
    phrases.extend(
        {
            "flexible": [["Month-to-month sublease."], ["Flexible short-term sublet."], ["Month to month arrangement."]],
            "ideal": [["6-month lease."], ["4-month lease."], ["3-8 months available."]],
            "maximum": [["12-month lease."], ["One-year lease required."], ["Lease runs for 12 months."]],
            "long": [["18-month lease."], ["24-month lease."], ["Minimum 18 months."]],
            "unknown": [[], [], []],
        }[signals.lease][variant % 3]
    )
    phrases.extend(
        {
            "ideal": [["3 person household."], ["Three people total in the home."], ["Household of 3."]],
            "two_other": [["You would live with 2 other roommates."], ["Join two current housemates."], ["You plus two roommates."]],
            "good": [["5 person household."], ["Five people total."], ["Household of 5."]],
            "over": [["7 person household."], ["Seven people total."], ["Household of 7."]],
            "unknown": [[], [], []],
        }[signals.household][variant % 3]
    )
    phrases.extend({"yes": [["Bright room with natural light."], ["Sunny bedroom."], ["Sun-filled room."]], "no": [["Windowless room with no natural light."], ["Dark room."], ["No natural light."]], "unknown": [[], [], []]}[signals.sunlight][variant % 3])
    phrases.extend({"yes": [["Near park access and bike routes."], ["A neighborhood park is nearby."], ["Easy access to trails and a park."]], "unknown": [[], [], []]}[signals.park][variant % 3])
    phrases.extend({"yes": [["Shared garden and deck."], ["Backyard access."], ["Sunny patio."]], "no": [["No outdoor space."], ["There is no yard."], ["No patio, deck, or garden."]], "unknown": [[], [], []]}[signals.outdoor][variant % 3])
    phrases.extend({"yes": [["Clean, chill, outdoorsy household."], ["Social young professionals who bike."], ["Respectful, communicative, creative home."]], "unknown": [[], [], []]}[signals.lifestyle][variant % 3])

    metadata: dict[str, str] = {}
    if signals.property_type == "preferred":
        metadata["property_type"] = "victorian"
    elif signals.property_type in {"apartment", "complex"}:
        metadata["property_type"] = "apartment"
    if metadata_override is not None:
        metadata = metadata_override

    return ListingCandidate(
        platform="Synthetic",
        source_id=case_id,
        title=title,
        original_url=f"https://example.test/{case_id}",
        price=prices[signals.price][variant % len(prices[signals.price])],
        neighborhood=neighborhood_name or neighborhoods[signals.neighborhood][variant % len(neighborhoods[signals.neighborhood])],
        listing_type=None,
        summary=summary_override if summary_override is not None else " ".join(phrases),
        metadata=metadata,
    )


def _case(
    case_id: str,
    cohort: str,
    signals: Signals,
    rationale: str,
    *,
    pair_id: str | None = None,
    winner: bool = False,
    **render_overrides: object,
) -> BenchmarkCase:
    expected_score, expected_main = human_relevance(signals)
    listing = _render_listing(case_id, signals, **render_overrides)  # type: ignore[arg-type]
    return BenchmarkCase(
        case_id=case_id,
        cohort=cohort,
        listing=listing,
        signals=signals,
        expected_score=expected_score,
        expected_main_result=expected_main,
        rationale=rationale,
        pair_id=pair_id,
        expected_pair_winner=winner,
    )


def broad_cases(count: int = 100, seed: int = 20260720) -> list[BenchmarkCase]:
    """Round 1: wide, reproducible coverage of the profile's feature space."""

    rng = random.Random(seed)
    choices = {
        "neighborhood": (["ideal", "preferred", "acceptable", "unknown", "outside"], [25, 25, 18, 12, 20]),
        "price": (["sweet", "wider", "low", "high", "unknown"], [38, 26, 7, 15, 14]),
        "privacy": (["private", "shared", "unknown"], [62, 16, 22]),
        "property_type": (["preferred", "apartment", "complex", "unknown"], [42, 18, 10, 30]),
        "lease": (["flexible", "ideal", "maximum", "long", "unknown"], [25, 22, 13, 10, 30]),
        "household": (["ideal", "good", "over", "two_other", "unknown"], [18, 18, 13, 16, 35]),
        "sunlight": (["yes", "no", "unknown"], [35, 12, 53]),
        "park": (["yes", "unknown"], [38, 62]),
        "outdoor": (["yes", "no", "unknown"], [31, 12, 57]),
        "lifestyle": (["yes", "unknown"], [45, 55]),
    }

    def pick(name: str) -> str:
        values, weights = choices[name]
        return rng.choices(values, weights=weights, k=1)[0]

    cases: list[BenchmarkCase] = []
    for index in range(count):
        signals = Signals(**{name: pick(name) for name in choices})
        cases.append(
            _case(
                f"broad-{index:03d}",
                "broad",
                signals,
                "Randomized broad-profile combination, graded by the independent admissibility policy.",
                variant=index,
            )
        )
    return cases


def boundary_cases() -> list[BenchmarkCase]:
    """Round 2: 50 explicit trade-off pairs (100 listings)."""

    base = Signals(
        neighborhood="preferred",
        price="sweet",
        privacy="private",
        property_type="preferred",
        lease="ideal",
        household="ideal",
        sunlight="unknown",
        park="unknown",
        outdoor="unknown",
        lifestyle="unknown",
    )
    pair_specs: list[tuple[str, Signals, Signals, str]] = [
        (
            "ideal area beats secondary area's perfect price",
            replace(base, neighborhood="ideal", price="wider"),
            replace(base, neighborhood="acceptable", price="sweet"),
            "Neighborhood is the strongest stated priority.",
        ),
        (
            "private unknown-area room beats shared ideal-area room",
            replace(base, neighborhood="unknown", privacy="private"),
            replace(base, neighborhood="ideal", privacy="shared"),
            "A shared bedroom violates the requested product.",
        ),
        (
            "flexible lease beats maximum one-year term",
            replace(base, lease="flexible"),
            replace(base, lease="maximum"),
            "Month-to-month/sublease is preferred to the absolute maximum term.",
        ),
        (
            "unknown sunlight beats explicit windowless room",
            replace(base, sunlight="unknown"),
            replace(base, sunlight="no"),
            "Missing information is neutral; an explicit negative is not.",
        ),
        (
            "five-person maximum beats over-capacity household",
            replace(base, household="good"),
            replace(base, household="over"),
            "The user wants no more than five people total.",
        ),
        (
            "Victorian house beats generic apartment complex",
            replace(base, property_type="preferred"),
            replace(base, property_type="complex"),
            "The requested home format explicitly excludes a generic complex.",
        ),
        (
            "target area beats outside area with bonus amenities",
            replace(base, neighborhood="preferred", sunlight="unknown", park="unknown", outdoor="unknown"),
            replace(base, neighborhood="outside", sunlight="yes", park="yes", outdoor="yes", lifestyle="yes"),
            "A known outside area should not enter main results via amenities.",
        ),
        (
            "ideal neighborhood beats preferred neighborhood",
            replace(base, neighborhood="ideal"),
            replace(base, neighborhood="preferred"),
            "Duboce/Potrero are the user's two clearest first choices.",
        ),
        (
            "sweet-spot price beats edge-of-budget price",
            replace(base, price="sweet"),
            replace(base, price="wider"),
            "$1,200-$1,800 is explicitly the sweet spot.",
        ),
        (
            "unknown privacy beats an explicit shared room",
            replace(base, privacy="unknown"),
            replace(base, privacy="shared"),
            "Unknown cannot be treated as a definite mismatch.",
        ),
    ]

    cases: list[BenchmarkCase] = []
    for repetition in range(5):
        for spec_index, (label, winner, loser, rationale) in enumerate(pair_specs):
            pair_id = f"boundary-{repetition:02d}-{spec_index:02d}"
            cases.extend(
                [
                    _case(f"{pair_id}-a", "boundary", winner, rationale, pair_id=pair_id, winner=True, variant=repetition),
                    _case(f"{pair_id}-b", "boundary", loser, rationale, pair_id=pair_id, winner=False, variant=repetition),
                ]
            )
    return cases


def adversarial_cases() -> list[BenchmarkCase]:
    """Round 3: missing-data and wording traps, arranged as 50 pairs."""

    cases: list[BenchmarkCase] = []
    neutral_base = Signals(neighborhood="preferred", price="sweet", privacy="private", property_type="preferred")

    # Unknown-vs-explicit-negative pairs ensure the neutral-missing policy holds.
    missing_specs = [
        ("privacy", "unknown", "shared"),
        ("sunlight", "unknown", "no"),
        ("outdoor", "unknown", "no"),
        ("lease", "unknown", "long"),
        ("household", "unknown", "over"),
    ]
    for repetition in range(5):
        for spec_index, (field, neutral_value, negative_value) in enumerate(missing_specs):
            pair_id = f"missing-{repetition:02d}-{spec_index:02d}"
            neutral = replace(neutral_base, **{field: neutral_value})
            negative = replace(neutral_base, **{field: negative_value})
            cases.extend(
                [
                    _case(
                        f"{pair_id}-neutral",
                        "adversarial",
                        neutral,
                        f"Missing {field} information must rank above an explicit mismatch.",
                        pair_id=pair_id,
                        winner=True,
                        variant=repetition,
                    ),
                    _case(
                        f"{pair_id}-negative",
                        "adversarial",
                        negative,
                        f"Explicitly negative {field} fact.",
                        pair_id=pair_id,
                        winner=False,
                        variant=repetition,
                    ),
                ]
            )

    # A commute reference is not evidence that the home is in that neighborhood.
    preferred_locations = ["Bernal Heights", "Mission Dolores", "Noe Valley", "Bernal Heights", "Mission Dolores"]
    outside_locations = ["SOMA", "Bayview", "Tenderloin", "Park Merced", "Outer Sunset"]
    commute_copy = [
        "Private room. 15 minute bike ride to Duboce Triangle.",
        "Private room. Easy commute to Potrero Hill.",
        "Private room. Close to restaurants in Mission Dolores.",
        "Private room. Direct transit to Bernal Heights.",
        "Private room. Weekend bike route goes through Noe Valley.",
    ]
    for repetition in range(5):
        pair_id = f"commute-mention-{repetition:02d}"
        real = replace(neutral_base, neighborhood="preferred")
        outside = replace(neutral_base, neighborhood="outside")
        cases.extend(
            [
                _case(
                    f"{pair_id}-real",
                    "adversarial",
                    real,
                    "An actual preferred-neighborhood address beats a distant location mention.",
                    pair_id=pair_id,
                    winner=True,
                    variant=repetition,
                    neighborhood_name=preferred_locations[repetition],
                ),
                _case(
                    f"{pair_id}-mention",
                    "adversarial",
                    outside,
                    "The listing is in SOMA; Duboce is only described as a bike destination.",
                    pair_id=pair_id,
                    winner=False,
                    variant=repetition,
                    neighborhood_name=outside_locations[repetition],
                    summary_override=commute_copy[repetition],
                ),
            ]
        )

    # Substring aliases must not turn an explicitly outside address into Noe Valley.
    substring_locations = [
        "Noel Street, SOMA",
        "Noelle Way, Bayview",
        "Castro Valley",
        "Potrero Avenue, Mission",
        "Bernal Avenue, Bayview",
    ]
    for repetition in range(5):
        pair_id = f"alias-boundary-{repetition:02d}"
        acceptable = replace(neutral_base, neighborhood="acceptable")
        outside = replace(neutral_base, neighborhood="outside")
        cases.extend(
            [
                _case(
                    f"{pair_id}-real",
                    "adversarial",
                    acceptable,
                    "A real secondary target is better than a coincidental text substring.",
                    pair_id=pair_id,
                    winner=True,
                    variant=repetition,
                    neighborhood_name="Cole Valley",
                ),
                _case(
                    f"{pair_id}-substring",
                    "adversarial",
                    outside,
                    "Noel Street in SOMA is not Noe Valley.",
                    pair_id=pair_id,
                    winner=False,
                    variant=repetition,
                    neighborhood_name=substring_locations[repetition],
                ),
            ]
        )

    # Natural wording of the ideal three-person household should not be worse
    # than a machine-friendly numeric phrase.
    for repetition in range(5):
        pair_id = f"household-language-{repetition:02d}"
        natural = replace(neutral_base, household="two_other")
        good = replace(neutral_base, household="good")
        cases.extend(
            [
                _case(
                    f"{pair_id}-natural",
                    "adversarial",
                    natural,
                    "Two other roommates means the user's ideal total household of three.",
                    pair_id=pair_id,
                    winner=True,
                    variant=repetition,
                ),
                _case(
                    f"{pair_id}-five",
                    "adversarial",
                    good,
                    "Five total is acceptable but not the ideal household size.",
                    pair_id=pair_id,
                    winner=False,
                    variant=repetition,
                ),
            ]
        )

    # Explicit negation should override attractive vocabulary in the same copy.
    for repetition in range(5):
        pair_id = f"negation-{repetition:02d}"
        positive = replace(neutral_base, sunlight="yes", outdoor="yes")
        negative = replace(neutral_base, sunlight="no", outdoor="no")
        cases.extend(
            [
                _case(
                    f"{pair_id}-positive",
                    "adversarial",
                    positive,
                    "Actual natural light and a deck beat explicit absence of both.",
                    pair_id=pair_id,
                    winner=True,
                    variant=repetition,
                ),
                _case(
                    f"{pair_id}-negative",
                    "adversarial",
                    negative,
                    "Positive nouns inside negated phrases are not positive amenities.",
                    pair_id=pair_id,
                    winner=False,
                    variant=repetition,
                    summary_override=[
                        "No natural light and no outdoor space; old photos show a sunny deck.",
                        "Not sunny; no yard, despite garden photos from a prior unit.",
                        "Dark room with no patio; building marketing says bright balcony homes.",
                        "Windowless room and no deck; sunny garden shown is next door.",
                        "No natural light or outdoor space; roof deck is not tenant-accessible.",
                    ][repetition],
                ),
            ]
        )

    # Property words embedded in a different housing type should not satisfy
    # the user's preference for an actual house/Victorian/shared flat.
    embedded_property_titles = [
        "Private bedroom in a houseboat apartment",
        "Own room in apartment near Victorian Row",
        "Private room in townhouse-style apartment unit",
        "Private bedroom in apartment beside a shared flat",
        "Own room in apartment with a Victorian lobby",
    ]
    for repetition in range(5):
        pair_id = f"property-substring-{repetition:02d}"
        preferred = replace(neutral_base, property_type="preferred")
        apartment = replace(neutral_base, property_type="apartment")
        cases.extend(
            [
                _case(
                    f"{pair_id}-real",
                    "adversarial",
                    preferred,
                    "An actual Victorian house beats an apartment described with an embedded word.",
                    pair_id=pair_id,
                    winner=True,
                    variant=repetition,
                ),
                _case(
                    f"{pair_id}-houseboat",
                    "adversarial",
                    apartment,
                    "A houseboat apartment is not the requested house format.",
                    pair_id=pair_id,
                    winner=False,
                    variant=repetition,
                    title_override=embedded_property_titles[repetition],
                    metadata_override={},
                ),
            ]
        )

    assert len(cases) == 100
    return cases


def all_cases() -> list[BenchmarkCase]:
    return broad_cases() + boundary_cases() + adversarial_cases()


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average_rank = (cursor + end - 1) / 2 + 1
        for position in range(cursor, end):
            ranks[order[position]] = average_rank
        cursor = end
    return ranks


def _correlation(left: list[float], right: list[float]) -> float:
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else 1.0


def _pair_accuracy(results: Iterable[CaseResult]) -> tuple[float, int]:
    grouped: dict[str, list[CaseResult]] = {}
    for result in results:
        if result.case.pair_id:
            grouped.setdefault(result.case.pair_id, []).append(result)
    correct = 0.0
    evaluated = 0
    for pair in grouped.values():
        if len(pair) != 2:
            continue
        winner = next(item for item in pair if item.case.expected_pair_winner)
        loser = next(item for item in pair if not item.case.expected_pair_winner)
        evaluated += 1
        if winner.actual_score > loser.actual_score:
            correct += 1
        elif winner.actual_score == loser.actual_score:
            correct += 0.5
    return (correct / evaluated if evaluated else 1.0), evaluated


def evaluate(cases: list[BenchmarkCase] | None = None) -> tuple[dict[str, dict[str, float]], list[CaseResult]]:
    preferences = load_preferences(PROFILE_PATH)
    cases = cases or all_cases()
    results: list[CaseResult] = []
    for case in cases:
        scored = score_listing(case.listing, preferences)
        results.append(
            CaseResult(
                case=case,
                actual_score=scored.score,
                actual_main_result=scored.score >= preferences.minimum_score,
                concern=scored.concern,
            )
        )

    metrics: dict[str, dict[str, float]] = {}
    for cohort in ("broad", "boundary", "adversarial"):
        cohort_results = [result for result in results if result.case.cohort == cohort]
        if not cohort_results:
            continue
        expected = [float(result.case.expected_score) for result in cohort_results]
        actual = [float(result.actual_score) for result in cohort_results]
        true_positive = sum(result.case.expected_main_result and result.actual_main_result for result in cohort_results)
        false_positive = sum(not result.case.expected_main_result and result.actual_main_result for result in cohort_results)
        false_negative = sum(result.case.expected_main_result and not result.actual_main_result for result in cohort_results)
        pair_accuracy, pair_count = _pair_accuracy(cohort_results)
        metrics[cohort] = {
            "cases": float(len(cohort_results)),
            "spearman": _correlation(_rank(expected), _rank(actual)),
            "mean_absolute_error": mean(abs(a - b) for a, b in zip(expected, actual, strict=True)),
            "main_precision": true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0,
            "main_recall": true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0,
            "pair_accuracy": pair_accuracy,
            "pairs": float(pair_count),
        }
    return metrics, results


def largest_failures(results: list[CaseResult], limit: int = 12) -> list[CaseResult]:
    return sorted(
        results,
        key=lambda result: (
            result.case.expected_main_result != result.actual_main_result,
            abs(result.actual_score - result.case.expected_score),
        ),
        reverse=True,
    )[:limit]


def report() -> str:
    metrics, results = evaluate()
    lines = ["Synthetic ranking benchmark (3 deterministic rounds, 100 cases each)"]
    for cohort, values in metrics.items():
        lines.append(
            f"{cohort:11} Spearman={values['spearman']:.3f}  "
            f"MAE={values['mean_absolute_error']:.1f}  "
            f"precision@60={values['main_precision']:.3f}  "
            f"recall@60={values['main_recall']:.3f}  "
            f"pair={values['pair_accuracy']:.3f}/{int(values['pairs'])}"
        )
    lines.append("Largest disagreements:")
    for result in largest_failures(results):
        lines.append(
            f"- {result.case.case_id}: expected={result.case.expected_score} "
            f"actual={result.actual_score}; {result.case.rationale} "
            f"Concern: {result.concern}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
