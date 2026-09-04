"""The chemist's own words in this thread, as an ambient nobody can pass in.

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

**It is the thread's user turns, not one message, and that widening is the whole point of the
module's current shape.** It carried exactly the message that started *this* turn, while
`structure_experiment_request` tells the model to call it "first … while correcting it is still
cheap" — iteratively, across turns. Measured: a chemist who wrote "24 wells, no DMF, by Friday
please." on turn 1 and "ok go ahead" on turn 3 had the intake refused, because `'24 wells'` is not
in "ok go ahead". So an honest `stated` was unrepresentable on the ordinary path, and the remedy
the refusal prescribed — mark it `inferred` — recorded a real chemist constraint as a model
inference, which is the mislabelling this check exists to prevent, running the other way.

**Widening it costs the anti-spoofing property nothing, and that is a claim about the producers.**
An earlier turn's user message is still the chemist's own words. What must never enter is the
model's: the two writers below stamp only messages a person typed, and the durable read behind the
front door's (`agent.session_store.chemist_words`) filters the transcript to `HumanMessage` rows it
decoded, so an assistant turn, a tool result and a mid-turn job-result push-back are all excluded.
The push-back is the one worth naming, because it enters the *graph* as a user message
(`api.runner._job_results_message` → `state.turn_input`) and so makes the checkpointer thread an
unsafe source for this: it carries framed tool output under the chemist's role.

**Bounded, because a conversation is not.** `agent_stated_quote_turns` bounds how many of the
chemist's earlier messages stay quotable and `agent_stated_quote_chars` bounds what they may weigh
— both, because either alone is unbounded in the other: the front door accepts
`service_max_message_chars` (100,000) in one message, so a turn count bounds no memory at all. The
turn in flight is always kept whole, even when it alone exceeds the character budget, so widening
can never refuse a quote the narrow version would have accepted.

**Absent means refused, not waived.** `get_current_user_texts()` returns `None` off a turn — a unit
test, a durable activity, any caller that is not a conversation — and a reader must treat that as
"there is no chemist to have said this", the way `require_actor` treats a missing actor. A check
that waived itself when the ambient was missing would be a check the caller can turn off by calling
from somewhere else. An empty sequence is normalised to `None` for the same reason: the two say the
same thing, and one absent value is one branch in every reader.

Two writers stamp it, which is every path a chemist's words can arrive on:
`api.runner._turn_ambient` for the front door, and `cli.chat.converse` for the admin CLI, which
invokes the graph directly rather than through the runner. Their windows differ in what each can
see, not in what counts: the front door reads the session's durable transcript, and the CLI — which
writes no transcript — accumulates the prompts typed into the process it is running in.
"""

from collections.abc import Sequence
from contextvars import ContextVar

from chemclaw.core.config import settings

_current_user_texts: ContextVar[tuple[str, ...] | None] = ContextVar(
    "chemclaw_current_user_texts", default=None
)


def _bounded(texts: Sequence[str]) -> tuple[str, ...]:
    """The newest of `texts` that fit both budgets, in the order they were said.

    Walks newest-first so the turn in flight is what survives a tight budget, and **keeps it
    unconditionally**: the check's floor is that this turn's message is quotable, and a bound that
    could take it away would make a configuration silently stricter than the one this widening
    replaced. Stops at the first earlier message that does not fit rather than skipping it for an
    older, smaller one — a window with a hole in it is not a window a refusal message can describe.
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
            started the turn now in flight. Bounded here rather than at the two call sites, so a
            writer cannot forget the budget and no reader has to re-state it.
    """
    if isinstance(texts, str):
        # A `str` *is* a `Sequence[str]`, so this passes `mypy --strict` and then binds one
        # haystack per character — under which a one-character quote matches anything and every
        # longer one is refused. Silent in both directions, and the failure this whole module
        # exists to prevent is a check that quietly stops checking, so it is refused loudly here
        # rather than typed around. `require_quotes_are_verbatim` takes the ambient's own
        # `tuple[str, ...]` for the same reason, where the type alone is enough.
        raise TypeError("set_current_user_texts takes the chemist's messages, not one string")
    bounded = _bounded(texts) if texts else ()
    return _current_user_texts.set(bounded or None)


def reset_current_user_texts(token: object) -> None:
    """Unbind the thread's user messages, restoring whatever was bound before."""
    _current_user_texts.reset(token)  # type: ignore[arg-type]


def get_current_user_texts() -> tuple[str, ...] | None:
    """The chemist's own messages for the thread in flight, or `None` when there is no turn.

    `None` is a fact about the caller, not a default to fall back from: it says no chemist spoke,
    so nothing can be attributed to one. Never an empty tuple — `set_current_user_texts`
    normalises that to `None`, so one absent value is one branch in every reader.
    """
    return _current_user_texts.get()
