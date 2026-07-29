"""Classification metrics (plan F10-F1): precision/recall/F1 over predicted vs expected id sets.

Exercises the shared pure computation (including the degenerate empty-set conventions) and the three
registered metrics end-to-end through an `EvalCase`, so they compute the right number and reject a
case with no ground truth.
"""

import math

import pytest

from evals.metric import EvalCase, MetricError, get_metric
from evals.metrics import precision_recall_f1


def test_prf_typical_case() -> None:
    """Two of three predicted are relevant, one of three relevant is missed → 2/3 each."""
    p, r, f = precision_recall_f1({"a", "b", "x"}, {"a", "b", "c"})
    assert p == pytest.approx(2 / 3)
    assert r == pytest.approx(2 / 3)
    assert f == pytest.approx(2 / 3)


def test_prf_perfect_and_disjoint() -> None:
    """Identical sets score 1.0; wholly disjoint sets score 0.0 across the board."""
    assert precision_recall_f1({"a", "b"}, {"a", "b"}) == (1.0, 1.0, 1.0)
    assert precision_recall_f1({"x"}, {"a"}) == (0.0, 0.0, 0.0)


def test_prf_empty_predictions_conventions() -> None:
    """No predictions: precision 1.0 iff nothing expected, else 0.0; recall reflects the miss."""
    assert precision_recall_f1(set(), set()) == (1.0, 1.0, 1.0)  # correct empty answer
    p, r, f = precision_recall_f1(set(), {"a"})
    assert (p, r) == (0.0, 0.0) and f == 0.0  # missed the one relevant note


def test_prf_nothing_expected_recall_is_one() -> None:
    """With nothing expected, recall is vacuously 1.0 but a spurious prediction tanks precision."""
    p, r, f = precision_recall_f1({"x"}, set())
    assert p == 0.0 and r == 1.0 and f == 0.0


def _case(predicted: list[str], expected: list[str]) -> EvalCase:
    return EvalCase(
        id="c",
        metrics=["precision", "recall", "f1"],
        output={"predicted_note_ids": predicted},
        reference={"expected_note_ids": expected},
    )


def test_registered_metrics_score_a_case() -> None:
    """The registry-resolved metrics compute the expected values off an `EvalCase`."""
    case = _case(["a", "b", "x"], ["a", "b", "c"])
    assert get_metric("precision")(case).value == pytest.approx(2 / 3)
    assert get_metric("recall")(case).value == pytest.approx(2 / 3)
    f1 = get_metric("f1")(case)
    assert f1.value == pytest.approx(2 / 3)
    assert f1.passed is None  # a report/drift metric, not a per-case gate


def test_classification_needs_a_reference() -> None:
    """A case with no reference cannot be scored — the ground truth is the whole point (G4)."""
    case = EvalCase(id="c", metrics=["f1"], output={"predicted_note_ids": ["a"]})
    with pytest.raises(MetricError, match="expected_note_ids"):
        get_metric("f1")(case)


def test_predicted_ids_must_be_a_list() -> None:
    """A bare string for the id list is rejected, not silently split into characters (G4)."""
    case = EvalCase(
        id="c",
        metrics=["precision"],
        output={"predicted_note_ids": "reaction-1"},
        reference={"expected_note_ids": ["reaction-1"]},
    )
    with pytest.raises(MetricError, match="must be a list"):
        get_metric("precision")(case)


def test_f1_is_the_harmonic_mean() -> None:
    """F1 equals the harmonic mean of precision and recall for an asymmetric case."""
    # predicted {a,b,c,d} vs expected {a,b} → precision 0.5, recall 1.0, F1 = 2/3.
    p, r, f = precision_recall_f1({"a", "b", "c", "d"}, {"a", "b"})
    assert (p, r) == (0.5, 1.0)
    assert f == pytest.approx(2 * p * r / (p + r))
    assert f == pytest.approx(2 / 3)
    assert not math.isnan(f)
