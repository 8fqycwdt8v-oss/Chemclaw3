"""The tournament's ranking measured against a constructed ground truth, with a null control.

Every figure here was produced by running `evals.hypothesis_tournament.simulate` and then pinned.
Bounds are loose enough to survive an unrelated change and tight enough that a broken pairing or a
broken fit moves them — a test asserting only `> 0` would pass with the ranking reversed.
"""

import pytest

from chemclaw.evals.hypothesis_tournament import simulate


def test_a_perfect_judge_recovers_the_ordering_exactly() -> None:
    """The ceiling. If this is not 1.0 the machinery itself is broken, not the judge.

    It isolates the question the simulation exists to answer: with the judge held perfect, anything
    less than full recovery is a defect in Swiss pairing or in the Bradley-Terry fit.
    """
    result = simulate(field=10, judge_accuracy=1.0, runs=200, seed=7)
    assert result.top_one == 1.0
    assert result.spearman > 0.95


def test_ranking_beats_not_ranking_even_with_a_barely_better_than_chance_judge() -> None:
    """The null control `D-2026-08-16` says every claim of benefit needs.

    A judge right 55% of the time still triples the chance of naming the best hypothesis, because
    twenty comparisons aggregate a weak signal. The comparison is against a shuffle of the same
    field, which is exactly "what if we had not ranked at all".
    """
    result = simulate(field=10, judge_accuracy=0.55, runs=300, seed=7)
    assert result.beats_null
    assert result.top_one > 2 * result.null_top_one
    assert result.null_spearman == pytest.approx(0.0, abs=0.2)


def test_a_realistic_judge_leaves_the_leader_wrong_more_often_than_right() -> None:
    """The finding that justifies the report refusing to name an undecided leader.

    At 75% judge accuracy over ten hypotheses, the top-rated one is genuinely best under half the
    time — far better than the 7% a shuffle gives, and nowhere near good enough to present as an
    answer. A table that printed a strict order here would be read as far more certain than it is,
    which is why `TournamentOutcome.leader_is_decisive` exists and why `report.summarise` leads with
    "the field does not separate" whenever it is False.
    """
    result = simulate(field=10, judge_accuracy=0.75, runs=300, seed=7)
    assert 0.35 < result.top_one < 0.65
    assert result.top_one > 5 * result.null_top_one


def test_accuracy_rises_monotonically_with_the_judge() -> None:
    """The ordering is driven by the judge, which is what makes the number mean anything.

    A ranking that improved with a *worse* judge would be fitting noise.
    """
    scores = [
        simulate(field=8, judge_accuracy=accuracy, runs=200, seed=3).top_one
        for accuracy in (0.55, 0.70, 0.85, 1.0)
    ]
    assert scores == sorted(scores)


def test_a_bigger_field_is_harder_at_a_fixed_judge_accuracy() -> None:
    """Sanity, and a guard on `rounds_for`.

    Swiss adds a round as the field doubles, which keeps recovery from collapsing but does not make
    a wider field easier.
    """
    small = simulate(field=6, judge_accuracy=0.65, runs=300, seed=11)
    large = simulate(field=12, judge_accuracy=0.65, runs=300, seed=11)
    assert small.top_one > large.top_one
    assert large.comparisons > small.comparisons
