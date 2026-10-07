"""Every member of the turn-event union has something in `src/` that can emit it.

A declared member nothing can produce is not a capability, and `Chemclaw3_ui` mirrors the union
by hand, so an unemittable member costs a whole consumer chain there. The rule is a producer
sweep: a member arrives with its emitter in the same commit. Constructor calls are resolved
against the union at runtime, so a new member is swept the day it is declared.
"""

import re
import typing
from pathlib import Path

from chemclaw.api.events import Event

_SRC = Path(__file__).resolve().parent.parent / "src" / "chemclaw"

# The declaring module itself: every member is defined here, so a `class X(BaseModel)` line would
# count as its own producer and the sweep would pass on any member at all.
_DECLARATION = _SRC / "api" / "events.py"


def _producers(class_name: str) -> list[str]:
    """Modules under `src/` (outside the declaration) that call `class_name(...)`."""
    pattern = re.compile(rf"(?<![\w.]){class_name}\(")
    return sorted(
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if path != _DECLARATION and pattern.search(path.read_text(encoding="utf-8"))
    )


def test_every_declared_turn_event_has_a_producer() -> None:
    """A union member with no emitter is a promise the shipped code cannot keep.

    Fails with the member's class name: ship the producer, or drop the member here and in the UI's
    mirror.
    """
    unproduced = sorted(
        model.__name__ for model in typing.get_args(Event) if not _producers(model.__name__)
    )
    assert not unproduced, (
        "turn-event union member(s) nothing under src/ can emit: "
        f"{unproduced} — ship the producer in the same commit, or delete the member here and in "
        "Chemclaw3_ui's hand-written mirror (shared/events.ts, state/types.ts, "
        "state/turnActivity.ts, state/chatStore.ts, TracePanel.tsx)"
    )
