"""The ambient session **id** for the current turn (plan Phase F3-T3).

When the agent launches a durable job (e.g. `sample_conformers`), the job must know *which
session* to notify on completion — but the session id is not something the model should pass as a
tool argument (it is not chemistry, and the model must not be able to spoof it). So the front-door
runner stamps the current session into a `contextvar` for the duration of the turn, and
job-launching tools read it here. A `contextvar` is the right carrier: it is task-local, so
concurrent turns for different sessions never see each other's id, and it defaults to `None` off
the request path (tests, the classic non-service caller) where there simply is no session to
notify.

**Kernel material, not conversation material.** The id is a bare `str`, this module imports nothing
but `contextvars`, and it is read from six packages — audit, the plan gate, connector identity
headers, template steps, and `core.logging`'s own `ContextFilter`. It lived in `chemclaw.agent`
until the R2 layering move, which is exactly why `core/logging.py` had to reach for it through a
lazy import to stay off the agent layer; that is now an ordinary intra-`core` import.

The other half of this ambient — the live `AgentSession` **object** — stayed behind in
`agent/session.py`, because it needs `agent_framework` and so cannot be kernel material. They
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
    """The session id of the turn in flight, or None when there is no session (non-service).

    Blank is absent, for the reason `core/identity_context.get_current_actor` states once and this
    module shares: a reader that accepts `""` and not `"   "` fails closed on one spelling of
    nothing and open on the other. Measured, the open one reaches further here than anywhere else —
    `agent/plan_gate.enforce_plan_approval` asks `if not session_id`, so a whitespace id proceeds
    *past* the early return and is then used as the `plan_approvals` lookup key, which is a session
    whose plan can never be the one a chemist approved. Stripped rather than merely rejected for
    that function's second reason: two spellings of one session must not be two rows.

    Kept as an expression rather than a shared helper on purpose. This module's docstring promises
    it "imports nothing but `contextvars`" — it is read from `core.logging`'s own filter, on the
    logging hot path — and importing a predicate from a sibling to save six characters would spend
    that guarantee.
    """
    return (_current_session_id.get() or "").strip() or None


def reset_current_session_id(token: object) -> None:
    """Restore the previous session id, undoing a `set_current_session_id` (turn teardown)."""
    _current_session_id.reset(token)  # type: ignore[arg-type]
