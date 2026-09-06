"""The bundled dev page renders every event a turn can emit (W1.5).

The page is a dev surface, not the product, which is exactly why it drifted: three events were
added to `chemclaw.api.events.Event` over three separate changes and none of them reached
`static/app.js`, whose `switch` silently falls through to `default` for a type it does not know.
A missing case looks identical to an event that was never sent, so the drift is invisible from the
page and only surfaces as "I ran `make chat`, launched a job, and nothing ever happened".

This is the same shape as every other validator in the repo: a declaration checked against the
live surface, so the page cannot fall behind the union without a red test naming the type.
"""

import re
from pathlib import Path
from typing import get_args

from chemclaw.api.events import Event

_APP_JS = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "api" / "static" / "app.js"

# `case "tool_call":` — the switch labels in `applyEvent`. A regex rather than a JS parser: the
# page is 180 lines of plain script, and the only thing being asserted is which literals appear.
_CASE = re.compile(r'case\s+"([a-z_]+)"\s*:')


def _event_types() -> set[str]:
    """Every `type` literal in the turn event union, read from the models themselves."""
    return {member.model_fields["type"].default for member in get_args(Event)}


def test_dev_page_handles_every_event_type() -> None:
    """Adding an event to the union without a case here is the drift this test exists to catch."""
    handled = set(_CASE.findall(_APP_JS.read_text(encoding="utf-8")))
    assert _event_types() <= handled, "the bundled dev page does not render: " + ", ".join(
        sorted(_event_types() - handled)
    )


# `events.addEventListener("job_completed", …)` — the push-back stream's subscriptions.
_LISTENER = re.compile(r'events\.addEventListener\(\s*"([a-z_]+)"')

#: Kinds `api/routes/streams.py` maps onto the push-back stream, as the SSE `event:` names it sends.
#:
#: Written out rather than imported because this is the *wire* name a browser dispatches on, and
#: the point of the test below is to compare two independent spellings of it.
_PUSHED_TO_THE_BROWSER = {"job_completed", "job_failed", "awaiting_answer"}


def test_dev_page_subscribes_to_every_pushed_kind() -> None:
    """A `case` without a matching `addEventListener` is dead code, and looks exactly like a fix.

    `EventSource` dispatches by the SSE `event:` name, so `applyEvent`'s branch for a pushed kind
    is only ever reached if `openEventStream` subscribed to that name. It did not for
    `awaiting_answer`: the case was added, the listener was not, and the one surface in this
    repository that renders the event never received a single one.

    `test_dev_page_handles_every_event_type` cannot see this — it matches `case "…":` as text and
    executes no JavaScript — which is exactly why the gap survived a green suite.
    """
    source = _APP_JS.read_text(encoding="utf-8")
    subscribed = set(_LISTENER.findall(source))
    missing = _PUSHED_TO_THE_BROWSER - subscribed
    assert not missing, (
        "the dev page renders these kinds but never subscribes to them, so the branch is "
        f"unreachable: {sorted(missing)}"
    )


def test_dev_page_has_no_case_for_a_type_that_does_not_exist() -> None:
    """The other direction: a case left behind by a renamed or removed event is dead code."""
    handled = set(_CASE.findall(_APP_JS.read_text(encoding="utf-8")))
    assert handled <= _event_types(), (
        "the dev page handles events that no turn can emit: "
        + ", ".join(sorted(handled - _event_types()))
    )
