"""Letting the agent ask instead of guess.

**AGT-5.** `_INSTRUCTIONS` tells the agent to "say plainly when the data is silent", but there was
no contract for it to *ask*. An ambiguous question ("what did we get on the Suzuki?") therefore
produced a best-guess sweep across every matching campaign — worse *and* more expensive than
asking which one was meant. The approval event of the day was structurally close and semantically
wrong — an approval is a yes/no on something already decided — so `QuestionEvent` is its own member
of the stream. That event has since gone with the hold behind it
(`D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold`); this one stayed, because a clarifying
question has a producer.

The dry-run turn flag (IDEA-4) used to live here too, since it started as a sibling interaction
primitive; it moved to `chemclaw.agent.turn_flags` because this module's import has a side effect
— registering `ask_clarifying_question` into the model-facing tool registry — that a plain flag
reader (`chemclaw.agent.tool_authz`, `chemclaw.connectors.identity`) has no business triggering.
This module now keeps only its tool.
"""

from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_question


@tool
async def ask_clarifying_question(question: str, options: list[str] | None = None) -> str:
    """Ask the chemist to disambiguate, instead of guessing across several possible readings.

    Use this when the question genuinely has more than one reasonable reading and the readings lead
    to different work — several campaigns match "the Suzuki", a compound name is ambiguous, a
    requested scale is missing. Do **not** use it for something the evidence can settle: sweep
    first, ask only when the sweep itself is what is ambiguous.

    The chemist's answer arrives as the next message, so anything you write after this call is
    written without it. Say only what you already know to be true — a partial answer with the
    missing input named is good, and is what the instructions ask for. What you must not do is
    describe work as under way, started, scheduled or arriving later: nothing is running, and a
    chemist told a campaign will "deliver by Monday" will wait for it. That happened in the
    2026-08-02 live run, in the same turn as the question.

    Args:
        question: The single question to ask, phrased so a one-line answer resolves it.
        options: Concrete choices when you can enumerate them, so the surface can offer buttons.

    Returns:
        Confirmation that the question was put to the chemist.
    """
    record_question(question, list(options or []))
    return "Question put to the chemist; awaiting their answer."
