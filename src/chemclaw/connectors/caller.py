"""Who core says is calling this connector, readable inside a tool — advisory, never a gate.

`chemclaw.connectors.identity` sends `X-Chemclaw-Actor`/`-Session`/`-Correlation-Id` on every
request, and `chemclaw.connectors.server.CallerLogMiddleware` logs them — with a docstring stating
exactly why they exist: "so a connector's own records can be joined to the core audit trail by
actor and session". That was the whole point of D-141, and until now a connector could only put
them in a *log line*. A connector that writes a durable record — a persisted BO suggestion, say —
had no way to stamp it with the conversation that asked for it, which is the gap that made the
record worth little: rows nobody can trace back to a chemist or a turn.

So the middleware now binds them into task-local contextvars a tool body can read, exactly as
`chemclaw.agent.identity_context` does on the core side and for the same reason: a tool has no
request object, and a connector process serves every user, so anything bound at import time would
be shared across them.

**The trust rule is unchanged and is the important part.** These values arrive on an
unauthenticated header from outside this process's trust boundary. Authorization already happened
in core (`chemclaw.agent.authz`) before the call was made, and a connector that gated on one of
these would be trusting a string anyone who can reach the Service could set. They are for
attribution in records and logs, and for nothing else. Every reader here is named so that stays
checkable.
"""

from contextvars import ContextVar

_caller_actor: ContextVar[str] = ContextVar("chemclaw_connector_caller_actor", default="")
_caller_session: ContextVar[str] = ContextVar("chemclaw_connector_caller_session", default="")
_caller_correlation: ContextVar[str] = ContextVar(
    "chemclaw_connector_caller_correlation", default=""
)


class CallerTokens:
    """The reset tokens for one bound request, so binding is symmetric with unbinding."""

    __slots__ = ("actor", "correlation", "session")

    def __init__(self, actor: object, session: object, correlation: object) -> None:
        """Hold the three `ContextVar.set` tokens for `reset_caller`."""
        self.actor = actor
        self.session = session
        self.correlation = correlation


def bind_caller(actor: str, session_id: str, correlation_id: str) -> CallerTokens:
    """Bind the calling identity for this request; returns tokens for `reset_caller`.

    Called by the connector's request middleware, never by a tool — a tool that could set its own
    caller would make the attribution it is stamping meaningless.
    """
    return CallerTokens(
        actor=_caller_actor.set(actor),
        session=_caller_session.set(session_id),
        correlation=_caller_correlation.set(correlation_id),
    )


def reset_caller(tokens: CallerTokens) -> None:
    """Unbind the request's caller, so one request's identity cannot leak into the next."""
    _caller_actor.reset(tokens.actor)  # type: ignore[arg-type]
    _caller_session.reset(tokens.session)  # type: ignore[arg-type]
    _caller_correlation.reset(tokens.correlation)  # type: ignore[arg-type]


def caller_provenance() -> tuple[str, str, str]:
    """The request's `(actor, session_id, correlation_id)`, empty strings off the request path.

    Empty rather than `None` because every consumer writes them into a record whose columns default
    to `''`: a connector tool exercised directly (a test, a CLI) genuinely has no caller, and that
    is "not recorded", not an error.
    """
    return _caller_actor.get(), _caller_session.get(), _caller_correlation.get()
