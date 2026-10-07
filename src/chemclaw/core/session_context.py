"""The ambient session **id** for the current turn.

A durable job must know which session to notify, but the session id must not be a model argument
(not chemistry, and spoofable). The front-door runner stamps it into a task-local `contextvar` for
the turn, and job-launching tools read it here; off the request path it is `None`. Imports only
`contextvars`, since audit, the plan gate, connector headers, template steps and `core.logging`'s
`ContextFilter` read it. The live session object is a separate contextvar in the agent layer.
"""

from contextvars import ContextVar

_current_session_id: ContextVar[str | None] = ContextVar(
    "chemclaw_current_session_id", default=None
)


def set_current_session_id(session_id: str | None) -> object:
    """Bind the current turn's session id; returns a token for `reset_current_session_id`."""
    return _current_session_id.set(session_id)


def get_current_session_id() -> str | None:
    """The session id of the turn in flight, or None when there is no session (non-service).

    Blank or whitespace is treated as absent, and the value is stripped, as in `get_current_actor`:
    otherwise a whitespace id would pass `if not session_id` checks such as the plan gate's and key
    a plan approval no chemist made. Written inline rather than via a shared helper, to keep this
    module importing nothing but `contextvars` (it is on the logging hot path).
    """
    return (_current_session_id.get() or "").strip() or None


def reset_current_session_id(token: object) -> None:
    """Restore the previous session id, undoing a `set_current_session_id` (turn teardown)."""
    _current_session_id.reset(token)  # type: ignore[arg-type]
