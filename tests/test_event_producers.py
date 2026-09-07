"""Every member of the turn-event union has something in `src/` that can emit it.

This is the `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` shape, applied to
the wire contract rather than to the audit column: *a declared capability nothing can write is not a
capability*. That ADR deleted `record_handoff` and the specialist contextvar because their only
callers were their own tests, and it left one thing standing on the argument that dropping a union
member is a coordinated change across `Chemclaw3_ui` — `HandoffEvent`, kept declared with **nothing
in the tree able to produce it**.

The cost of keeping it was not zero and was not paid here. `Chemclaw3_ui` mirrors this union by
hand, so the member propagated into `shared/events.ts`, `state/types.ts`, `state/turnActivity.ts`,
`state/chatStore.ts` and a `TracePanel` renderer printing "Handed to X" — a whole consumer chain for
an event that has never been sent and cannot be. That repository deleted its half first and pinned
the absence; `D-2026-09-07-an-event-nobody-emits-is-a-renderer-somebody-else-maintains` deleted this
one. This is what stops the next.

**Why a producer sweep rather than a name in a list.** Naming `handoff` here would assert the
deletion and nothing more. What the union needs asserted is the *rule*: a member arrives with its
emitter, in the same commit, or this fails by name. That is exactly what did not happen last time —
`HandoffEvent` outlived its producer by two ADRs while every test stayed green.

Constructor calls, resolved against the union at runtime rather than against a hand-listed set of
class names, so a member added tomorrow is swept the day it is declared.
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

    Fails with the member's own class name, because the two honest responses to it are opposite and
    the reader has to pick one: ship the producer, or drop the member here **and** in the UI's
    mirror. Leaving it declared is the third option this test exists to remove.
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
