"""The tournament's ranking measured against a constructed ground truth, with a null control.

Figures come from running `evals.hypothesis_tournament.simulate` and are pinned with bounds loose
enough to survive unrelated changes and tight enough that a broken pairing or fit moves them.
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


def test_a_judge_with_no_information_scores_exactly_at_the_null() -> None:
    """A judge with no information scores exactly at the null.

    A coin-flip judge must be indistinguishable from chance. The truth labels are shuffled relative
    to the ids, so a bracket favouring early ids would show up here.
    """
    result = simulate(field=10, judge_accuracy=0.5, runs=600, seed=7)
    assert result.spearman == pytest.approx(0.0, abs=0.06)
    assert result.top_one == pytest.approx(0.1, abs=0.05)


def test_ranking_beats_not_ranking_even_with_a_barely_better_than_chance_judge() -> None:
    """The null control `D-2026-08-16` says every claim of benefit needs.

    A judge right 55% of the time still lifts the chance of naming the best hypothesis well above
    the 1-in-10 a shuffle gives, because twenty comparisons aggregate a weak signal.
    """
    result = simulate(field=10, judge_accuracy=0.55, runs=600, seed=7)
    assert result.beats_null
    assert result.top_one > result.null_top_one
    assert result.null_spearman == pytest.approx(0.0, abs=0.2)


def test_a_realistic_judge_leaves_the_leader_wrong_more_often_than_right() -> None:
    """A realistic judge leaves the leader wrong more often than right.

    At 75% judge accuracy over ten hypotheses the top-rated one is best only about 39% of the time,
    so a strict order would overstate certainty. This is why `TournamentOutcome.leader_is_decisive`
    exists and `report.summarise` leads with "the field does not separate" when it is False.
    """
    result = simulate(field=10, judge_accuracy=0.75, runs=600, seed=7)
    assert 0.30 < result.top_one < 0.50
    assert result.top_one > 3 * result.null_top_one


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
