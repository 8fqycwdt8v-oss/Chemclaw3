"""The ambient authenticated identity for the current turn (plan Phase F4-T5).

Like the session id (`chemclaw.agent.session_context`), the authenticated user's Entra `oid` and
app roles
are ambient to a turn, not tool arguments: the front-door runner stamps them from the request's
validated `Principal`, and audit, the authorization gate, and job-attribution read them here. A
`contextvar` is the right carrier — task-local, so concurrent turns never cross identities — and it
defaults to "no identity" off the request path (tests, the classic non-service caller), where the
static audit actor and the dev-mode allowances apply.

The turn's **correlation id** rides here for the same reason and with the same consumer. It used
to be bound once inside `build_agent`, and agents are cached per profile for the process's whole
life — so every turn from every user on a pod shared one id, which is precisely the opposite of
what a correlation id is for: the audit trail could not separate two chemists' tool calls, and
"show me everything that happened in this conversation" returned the pod's entire history. It is
per-turn state, so it belongs in a task-local like the actor, not on a cached object.

Kept in `agent/` (not `api/`) as plain `str`/`frozenset` values so `chemclaw.agent.audit` and
`chemclaw.agent.authz` can read it without importing the front door (which would invert the
layering).
"""

from contextvars import ContextVar

_current_actor: ContextVar[str | None] = ContextVar("chemclaw_current_actor", default=None)
_current_roles: ContextVar[frozenset[str]] = ContextVar(
    "chemclaw_current_roles", default=frozenset()
)
_current_correlation_id: ContextVar[str | None] = ContextVar(
    "chemclaw_current_correlation_id", default=None
)


def set_current_identity(actor: str, roles: frozenset[str]) -> tuple[object, object]:
    """Bind the turn's actor (Entra oid) and roles; returns tokens for `reset_current_identity`."""
    return _current_actor.set(actor), _current_roles.set(roles)


def reset_current_identity(tokens: tuple[object, object]) -> None:
    """Restore the previous identity, undoing a `set_current_identity` (turn teardown)."""
    actor_token, roles_token = tokens
    _current_actor.reset(actor_token)  # type: ignore[arg-type]
    _current_roles.reset(roles_token)  # type: ignore[arg-type]


def get_current_actor() -> str | None:
    """The Entra oid of the turn in flight, or None when there is no authenticated user."""
    return _current_actor.get()


def get_current_roles() -> frozenset[str]:
    """The app roles of the turn's user (empty when there is no authenticated user)."""
    return _current_roles.get()


def set_current_correlation_id(correlation_id: str) -> object:
    """Bind the turn's correlation id; returns a token for `reset_current_correlation_id`."""
    return _current_correlation_id.set(correlation_id)


def reset_current_correlation_id(token: object) -> None:
    """Restore the previous correlation id, undoing a `set_current_correlation_id` (teardown)."""
    _current_correlation_id.reset(token)  # type: ignore[arg-type]


def get_current_correlation_id() -> str | None:
    """The correlation id of the turn in flight, or None off the request path.

    None means "no turn stamped one", and the caller falls back to whatever id it was built with —
    which is what the Temporal template activities and the CLI rely on, since they bind a
    meaningful id (the workflow id) at build time and have no per-turn stamp.
    """
    return _current_correlation_id.get()
