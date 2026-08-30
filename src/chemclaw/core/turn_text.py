"""The chemist's own words for the turn in flight, as an ambient nobody can pass in.

**This exists because a quote is evidence about a person, and evidence cannot be supplied by the
thing being checked.** `protocols.models.RequestField` lets a slot claim `basis="stated"`, which
means "the chemist wrote this", and obliges a verbatim `quote`. That claim is worth something only
if the text it is checked against is the chemist's. It used to be a tool argument called
`source_text`, which a model fills in — so a model that wanted `stated` supplied a `source_text`
containing its own quotes and got it, and the fabricated attribution landed in
`experiment_protocols` indistinguishable from a real one. Measured, before this module existed:
the same request refused against the real user text and accepted against an invented one.

The shape is `session_context`'s, and so is the argument, stated there in as many words: the
session id "is not something the model should pass as a tool argument (it is not chemistry, and
the model must not be able to spoof it)". `dry_run` rides the same way for the same reason. The
chemist's message is a third of that kind — conversation material rather than kernel material,
which is why it is its own module rather than a second variable on the session id's.

**Absent means refused, not waived.** `get_current_user_text()` returns `None` off a turn — a unit
test, a durable activity, any caller that is not a conversation — and a reader must treat that as
"there is no chemist to have said this", the way `require_actor` treats a missing actor. A check
that waived itself when the ambient was missing would be a check the caller can turn off by calling
from somewhere else.

Two writers stamp it, which is every path a chemist's words can arrive on:
`api.runner._turn_ambient` for the front door, and `cli.chat.converse` for the admin CLI, which
invokes the graph directly rather than through the runner.
"""

from contextvars import ContextVar

_current_user_text: ContextVar[str | None] = ContextVar("chemclaw_current_user_text", default=None)


def set_current_user_text(text: str | None) -> object:
    """Bind the turn's user message; returns a token for `reset_current_user_text`."""
    return _current_user_text.set(text)


def reset_current_user_text(token: object) -> None:
    """Unbind the turn's user message, restoring whatever was bound before."""
    _current_user_text.reset(token)  # type: ignore[arg-type]


def get_current_user_text() -> str | None:
    """The chemist's message for the turn in flight, or `None` when there is no turn.

    `None` is a fact about the caller, not a default to fall back from: it says no chemist spoke,
    so nothing can be attributed to one.
    """
    return _current_user_text.get()
