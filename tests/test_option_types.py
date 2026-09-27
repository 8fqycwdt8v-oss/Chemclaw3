"""`core/connect.option_type_mismatch` judges a manifest value against the annotation it meets.

Two holes the review of 2026-09-26 drove open: a factory module under
`from __future__ import annotations` stores `"bool"` rather than `bool`, so the check passed
everything there; and `True` is an `int` to `isinstance`, so an `int` parameter took a YAML flag.
"""

from chemclaw.core.connect import option_type_mismatch


def _string_annotated(snapshot: "bool" = False, limit: "int" = 0) -> None:
    """What every annotation in a `from __future__ import annotations` module looks like."""


def _partly_unresolvable(flag: bool = False, other: "NotImportedAtRuntime" = None) -> None:  # type: ignore[name-defined]  # noqa: F821
    """A factory whose one annotation names something only `TYPE_CHECKING` imports."""


def _counted(n: int = 0, ratio: float = 1.0) -> None:
    """Numeric parameters, which a YAML flag must not satisfy."""


def test_a_string_annotation_is_judged_like_the_type_it_names() -> None:
    """`snapshot: "false"` is refused whether the factory's module postpones annotations or not."""
    assert "snapshot" in option_type_mismatch(_string_annotated, {"snapshot": "false"})
    assert option_type_mismatch(_string_annotated, {"snapshot": False, "limit": 3}) == ""


def test_an_annotation_that_cannot_be_evaluated_leaves_the_rest_judged() -> None:
    """Falling back to the unevaluated signature passes over only what it cannot read."""
    assert "flag" in option_type_mismatch(_partly_unresolvable, {"flag": "no"})
    assert option_type_mismatch(_partly_unresolvable, {"other": "anything"}) == ""


def test_a_flag_is_not_a_number() -> None:
    """`True` is an `int` in Python and never an integer anybody wrote down."""
    assert "n=True" in option_type_mismatch(_counted, {"n": True})
    assert "ratio=False" in option_type_mismatch(_counted, {"ratio": False})
    assert option_type_mismatch(_counted, {"n": 3, "ratio": 2}) == ""
