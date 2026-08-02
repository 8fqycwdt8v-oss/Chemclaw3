"""The ambient session **id** for the current turn (plan Phase F3-T3).

When the agent launches a durable job (e.g. `compute_dft_energy`), the job must know *which
session* to notify on completion — but the session id is not something the model should pass as a
tool argument (it is not chemistry, and the model must not be able to spoof it). So the front-door
runner stamps the current session into a `contextvar` for the duration of the turn, and
job-launching tools read it here. A `contextvar` is the right carrier: it is task-local, so
concurrent turns for different sessions never see each other's id, and it defaults to `None` off
the request path (tests, the classic non-service caller) where there simply is no session to
notify.

**Kernel material, not conversation material.** The id is a bare `str`, this module imports nothing
but `contextvars`, and it is read from six packages — audit, the PR-gate, connector identity
headers, template steps, and `core.logging`'s own `ContextFilter`. It lived in `chemclaw.agent`
until the R2 layering move, which is exactly why `core/logging.py` had to reach for it through a
lazy import to stay off the agent layer; that is now an ordinary intra-`core` import.

The other half of this ambient — the live `AgentSession` **object** — stayed behind in
`agent/live_session.py`, because it needs `agent_framework` and so cannot be kernel material. They
were always two separate contextvars, for the reason that module's docstring gives; the split runs
along the line that was already there.
"""

from contextvars import ContextVar

_current_session_id: ContextVar[str | None] = ContextVar(
    "chemclaw_current_session_id", default=None
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
