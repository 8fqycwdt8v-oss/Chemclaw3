"""The ambient session for the current turn (plan Phase F3-T3).

When the agent launches a durable job (e.g. `submit_qm_job`), the job must know *which session* to
notify on completion — but the session id is not something the model should pass as a tool argument
(it is not chemistry, and the model must not be able to spoof it). So the front-door runner stamps
the current session into a `contextvar` for the duration of the turn, and job-launching tools read
it here. A `contextvar` is the right carrier: it is task-local, so concurrent turns for different
sessions never see each other's id, and it defaults to `None` off the request path (tests, the
classic non-service caller) where there simply is no session to notify.

`get_current_session` carries the live `AgentSession` object alongside the id, for the one
consumer that needs more than the id: `agents.harness_todo.mark_awaiting_job` mutates the
session's own `TodoProvider` state, which lives on the object, not reachable from the id alone.
Kept as a second ambient rather than folding into the id one so every existing id-only consumer
(job attribution) is unaffected.
"""

from contextvars import ContextVar

from agent_framework import AgentSession

_current_session_id: ContextVar[str | None] = ContextVar(
    "chemclaw_current_session_id", default=None
)
_current_session: ContextVar[AgentSession | None] = ContextVar(
    "chemclaw_current_session", default=None
)


def set_current_session_id(session_id: str | None) -> object:
    """Bind the current turn's session id; returns a token for `reset_current_session_id`."""
    return _current_session_id.set(session_id)


def get_current_session_id() -> str | None:
    """The session id of the turn in flight, or None when there is no session (non-service)."""
    return _current_session_id.get()


def reset_current_session_id(token: object) -> None:
    """Restore the previous session id, undoing a `set_current_session_id` (turn teardown)."""
    _current_session_id.reset(token)  # type: ignore[arg-type]


def set_current_session(session: AgentSession | None) -> object:
    """Bind the current turn's live session object; returns a token for `reset_current_session`."""
    return _current_session.set(session)


def get_current_session() -> AgentSession | None:
    """The live session object of the turn in flight, or None when there is no session."""
    return _current_session.get()


def reset_current_session(token: object) -> None:
    """Restore the previous session object, undoing a `set_current_session` (turn teardown)."""
    _current_session.reset(token)  # type: ignore[arg-type]
