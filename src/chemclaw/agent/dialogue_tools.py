"""Letting the agent ask instead of guess, and letting a chemist ask what *would* happen.

Two interaction capabilities that had no expression in the tool surface:

- **AGT-5.** `_INSTRUCTIONS` tells the agent to "say plainly when the data is silent", but there was
  no contract for it to *ask*. An ambiguous question ("what did we get on the Suzuki?") therefore
  produced a best-guess sweep across every matching campaign — worse *and* more expensive than
  asking which one was meant. `ApprovalRequestEvent` was structurally close but semantically wrong:
  an approval is a yes/no on something already decided.

- **IDEA-4.** Every expensive path is idempotent and cached, but there was no way to ask "what
  would this cost, what would you do" without doing it. For a system whose production-default
  autonomy is `plan_only`, an explicit dry run is the natural product primitive — and a cheap
  safety valve in front of the durable job launchers.
"""

from contextvars import ContextVar

from chemclaw.agent.tool_registry import tool
from chemclaw.agent.turn_signals import record_question

# Whether the turn in flight is a dry run. A contextvar for the same reason the ambient session and
# identity are: it must be per-turn, it must not be a model-supplied tool argument (the model must
# not be able to turn a real run into a dry one or vice versa), and it must default to "off" for
# every non-request caller.
_dry_run: ContextVar[bool] = ContextVar("chemclaw_dry_run", default=False)


def set_dry_run(enabled: bool) -> object:
    """Mark the current turn as a dry run; returns a token for `reset_dry_run`."""
    return _dry_run.set(enabled)


def reset_dry_run(token: object) -> None:
    """Clear the dry-run flag at turn teardown."""
    _dry_run.reset(token)  # type: ignore[arg-type]


def is_dry_run() -> bool:
    """Whether the turn in flight is a dry run (False off the request path)."""
    return _dry_run.get()


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
