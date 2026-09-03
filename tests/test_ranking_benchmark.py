from __future__ import annotations

from tests.ranking_benchmark import adversarial_cases, all_cases, boundary_cases, broad_cases, evaluate


def test_benchmark_has_three_reproducible_hundred_listing_rounds() -> None:
    first = broad_cases()
    second = broad_cases()

    assert len(first) == len(boundary_cases()) == len(adversarial_cases()) == 100
    assert [case.signals for case in first] == [case.signals for case in second]
    assert [case.case_id for case in first] == [case.case_id for case in second]


def test_broad_profile_ranking_tracks_independent_human_policy() -> None:
    metrics, _ = evaluate(broad_cases())
    broad = metrics["broad"]

    # These are quality floors, not a demand for exact numeric agreement with
    # the independent rubric.  They catch large priority inversions.
    assert broad["spearman"] >= 0.75
    assert broad["main_precision"] >= 0.75
    assert broad["main_recall"] >= 0.75


def test_explicit_profile_tradeoffs_rank_in_the_expected_direction() -> None:
    metrics, _ = evaluate(boundary_cases())

    assert metrics["boundary"]["pairs"] == 50
    assert metrics["boundary"]["pair_accuracy"] >= 0.90


def test_missing_and_adversarial_language_is_handled_robustly() -> None:
    metrics, _ = evaluate(adversarial_cases())

    assert metrics["adversarial"]["pairs"] == 50
    assert metrics["adversarial"]["pair_accuracy"] >= 0.80


def test_explicit_hard_constraints_never_enter_main_results() -> None:
    _, results = evaluate(all_cases())
    violations = [
        result.case.case_id
        for result in results
        if result.actual_main_result
        and (
            result.case.signals.neighborhood == "outside"
            or result.case.signals.privacy == "shared"
            or (
                result.case.signals.price in {"low", "high"}
                and (
                    result.case.listing.price is None
                    or result.case.listing.price < 760
                    or result.case.listing.price > 2700
                )
            )
            or result.case.signals.property_type == "complex"
            or result.case.signals.household == "over"
            or result.case.signals.lease == "long"
        )
    ]

    assert violations == []
