"""The ambient live `AgentSession` object for the current turn (plan Phase F3-T3, D-058).

The turn's session **id** is the ambient almost everything wants, it is a bare `str`, and it is
therefore kernel material (`core/session_context.py`). This is the other half: the live
`AgentSession` object itself, carried for the consumers that need more than the id —
`chemclaw.agent.plan_gate` gates on the session the harness is running, and
`chemclaw.connectors.jobs` hands it to `chemclaw.agent.harness_todo.mark_awaiting_job`, which
mutates the session's own `TodoProvider` state. That state lives on the object and is not reachable
from the id alone.

Two things keep this out of the kernel beside the id it accompanies. It imports `agent_framework`,
which `chemclaw.core` may not; and it was already a separate contextvar, so every id-only consumer
(job attribution, audit, the log record's `session_id`) is untouched by its existence — the reason
the two were never folded together in the first place.

The front-door runner sets both around a turn (`api/runner.py`), which is the one place that has
the session object to bind.
"""

from contextvars import ContextVar

from agent_framework import AgentSession

_current_session: ContextVar[AgentSession | None] = ContextVar(
    "chemclaw_current_session", default=None
)


def set_current_session(session: AgentSession | None) -> object:
    """Bind the current turn's live session object; returns a token for `reset_current_session`."""
    return _current_session.set(session)


def get_current_session() -> AgentSession | None:
    """The live session object of the turn in flight, or None when there is no session."""
    return _current_session.get()


def reset_current_session(token: object) -> None:
    """Restore the previous session object, undoing a `set_current_session` (turn teardown)."""
    _current_session.reset(token)  # type: ignore[arg-type]
