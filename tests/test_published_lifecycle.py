"""The design lifecycle the API contract publishes is the one `require_movable` enforces.

A client draws its sign-off buttons from `StatusIn`'s `x-` extensions instead of transcribing
`protocols/store.py`, so every (status, target, head kind) the published rules allow must pass the
store's check and every one they refuse must raise — derived from the document, not restated.
"""

import functools
import itertools
from typing import get_args

import pytest

from chemclaw.api.contract import build_document
from chemclaw.protocols.models import DesignStatus, RevisionKind
from chemclaw.protocols.store import UnstorableDocument, require_movable


@functools.cache
def _published() -> dict[str, object]:
    """`StatusIn`'s schema as the served document carries it."""
    schema: dict[str, object] = build_document()["components"]["schemas"]["StatusIn"]
    return schema


def _allowed(rules: dict[str, object], current: str, target: str, head: str) -> bool:
    """What the published rules say about one move — the reading a client implements."""
    moves = rules["x-legal-moves"]
    needing = rules["x-statuses-needing-a-protocol"]
    refusing = rules["x-statuses-refusing-a-protocol"]
    assert isinstance(moves, dict) and isinstance(needing, list) and isinstance(refusing, list)
    if target in needing and head != "protocol":
        return False
    if target in refusing and head == "protocol":
        return False
    return target == current or target in moves[current]


def test_every_status_has_a_published_row() -> None:
    moves = _published()["x-legal-moves"]
    assert isinstance(moves, dict)
    assert sorted(moves) == sorted(get_args(DesignStatus))


@pytest.mark.parametrize(
    ("current", "target", "head"),
    list(itertools.product(get_args(DesignStatus), get_args(DesignStatus), get_args(RevisionKind))),
)
def test_the_published_rules_are_the_enforced_rules(current: str, target: str, head: str) -> None:
    allowed = _allowed(_published(), current, target, head)
    if allowed:
        require_movable(current, target, head)  # type: ignore[arg-type]
    else:
        with pytest.raises(UnstorableDocument):
            require_movable(current, target, head)  # type: ignore[arg-type]
