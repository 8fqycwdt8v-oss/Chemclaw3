"""The chemist's own words in this thread, as an ambient nobody can pass in.

A request slot may claim `basis="stated"` ("the chemist wrote this") with a verbatim `quote`; that
claim means something only if the text it is checked against is the chemist's and not
model-supplied. So, like the session id and `dry_run`, it is a task-local ambient rather than a tool
argument.

It holds the thread's user turns, not one message, because intake is iterative: a constraint stated
two turns ago is still the chemist's. Only messages a person typed enter: the front door reads the
durable transcript filtered to `HumanMessage` rows (`agent.session_store.chemist_words`), never the
checkpointer thread, where a job-result push-back arrives under the user role.

Bounded by `agent_stated_quote_turns` and `agent_stated_quote_chars` (each alone leaves the other
unbounded); the turn in flight is always kept whole. Absent means refused, not waived:
`get_current_user_texts()` returns `None` off a turn, and an empty sequence is normalised to `None`.
Two writers: `api.runner._turn_ambient` (front door) and `cli.chat.converse` (admin CLI, which
accumulates the prompts typed into its own process).
"""

from collections.abc import Sequence
from contextvars import ContextVar

from chemclaw.core.config import settings

_current_user_texts: ContextVar[tuple[str, ...] | None] = ContextVar(
    "chemclaw_current_user_texts", default=None
)


def _bounded(texts: Sequence[str]) -> tuple[str, ...]:
    """The newest of `texts` that fit both budgets, in the order they were said.

    Walks newest-first and always keeps the turn in flight. Stops at the first earlier message that
    does not fit rather than skipping to an older one, so the window has no holes.
    """
    window = list(texts)[-(settings.agent_stated_quote_turns + 1) :]
    budget = settings.agent_stated_quote_chars
    kept: list[str] = []
    for text in reversed(window):
        if kept and len(text) > budget:
            break
        kept.append(text)
        budget -= len(text)
    kept.reverse()
    return tuple(kept)


def set_current_user_texts(texts: Sequence[str] | None) -> object:
    """Bind the chemist's own words for this thread, oldest first, this turn's message last.

    Returns a token for `reset_current_user_texts`. `None` — or an empty sequence, which says the
    same thing — binds "there is no chemist", which every reader must treat as a refusal.

    Args:
        texts: The chemist's messages in this thread, oldest first, ending with the one that
            started the turn now in flight. Bounded here so no writer can forget the budget.
    """
    if isinstance(texts, str):
        # A `str` is a `Sequence[str]` and passes type checking, but would bind one haystack per
        # character and silently break the check, so it is refused loudly.
        raise TypeError("set_current_user_texts takes the chemist's messages, not one string")
    bounded = _bounded(texts) if texts else ()
    return _current_user_texts.set(bounded or None)


def reset_current_user_texts(token: object) -> None:
    """Unbind the thread's user messages, restoring whatever was bound before."""
    _current_user_texts.reset(token)  # type: ignore[arg-type]


def get_current_user_texts() -> tuple[str, ...] | None:
    """The chemist's own messages for the thread in flight, or `None` when there is no turn.

    `None` means no chemist spoke, so nothing can be attributed to one. Never an empty tuple.
    """
    return _current_user_texts.get()
