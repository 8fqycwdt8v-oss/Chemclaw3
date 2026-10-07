"""Who core says is calling this connector, readable inside a tool: advisory, never a gate.

`CallerLogMiddleware` binds the `X-Chemclaw-Actor`/`-Session`/`-Correlation-Id` headers into
task-local contextvars (a tool has no request object, and one process serves every user), so a
connector's durable records can be joined to core's audit trail (D-141).

These values arrive on an unauthenticated header. Authorization already happened in core
(`chemclaw.agent.authz`); a connector must use them for attribution in records and logs only,
never to gate anything.
"""

from contextvars import ContextVar

from chemclaw.core.logging import bind_claimed_caller, reset_claimed_caller

_caller_actor: ContextVar[str] = ContextVar("chemclaw_connector_caller_actor", default="")
_caller_session: ContextVar[str] = ContextVar("chemclaw_connector_caller_session", default="")
_caller_correlation: ContextVar[str] = ContextVar(
    "chemclaw_connector_caller_correlation", default=""
)


class CallerTokens:
    """The reset tokens for one bound request, so binding is symmetric with unbinding."""

    __slots__ = ("actor", "correlation", "log", "session")

    def __init__(self, actor: object, session: object, correlation: object, log: object) -> None:
        """Hold the four `ContextVar.set` tokens for `reset_caller`."""
        self.actor = actor
        self.session = session
        self.correlation = correlation
        self.log = log


def bind_caller(actor: str, session_id: str, correlation_id: str) -> CallerTokens:
    """Bind the calling identity for this request; returns tokens for `reset_caller`.

    Called by request middleware, never by a tool (a tool that set its own caller would make the
    attribution meaningless). Also binds `core.logging`'s claimed-caller variable, never the core
    identity ones, so every log line a connector writes carries the caller.
    """
    return CallerTokens(
        actor=_caller_actor.set(actor),
        session=_caller_session.set(session_id),
        correlation=_caller_correlation.set(correlation_id),
        log=bind_claimed_caller(actor, session_id, correlation_id),
    )


def reset_caller(tokens: CallerTokens) -> None:
    """Unbind the caller bound by the matching `bind_caller`.

    Per-call isolation comes from `connectors/server.py::_bind_caller_per_tool_call`, which binds
    and resets around each tool call; this is the unbinding half of both call sites.
    """
    _caller_actor.reset(tokens.actor)  # type: ignore[arg-type]
    _caller_session.reset(tokens.session)  # type: ignore[arg-type]
    _caller_correlation.reset(tokens.correlation)  # type: ignore[arg-type]
    reset_claimed_caller(tokens.log)


def caller_provenance() -> tuple[str, str, str]:
    """The serving call's `(actor, session_id, correlation_id)`, empty strings off that path.

    Empty rather than `None` because consumers write them into columns defaulting to `''`; a tool
    exercised directly (a test, a CLI) has no caller, which is "not recorded", not an error.
    """
    return _caller_actor.get(), _caller_session.get(), _caller_correlation.get()
