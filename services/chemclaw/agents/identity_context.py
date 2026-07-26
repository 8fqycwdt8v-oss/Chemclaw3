"""The ambient authenticated identity for the current turn (plan Phase F4-T5).

Like the session id (`agents.session_context`), the authenticated user's Entra `oid` and app roles
are ambient to a turn, not tool arguments: the front-door runner stamps them from the request's
validated `Principal`, and audit, the authorization gate, and job-attribution read them here. A
`contextvar` is the right carrier — task-local, so concurrent turns never cross identities — and it
defaults to "no identity" off the request path (tests, the classic non-service caller), where the
static audit actor and the dev-mode allowances apply.

Kept in `agents/` (not `service/`) as plain `str`/`frozenset` values so `agents.audit` and
`agents.authz` can read it without importing the front door (which would invert the layering).
"""

from contextvars import ContextVar

_current_actor: ContextVar[str | None] = ContextVar("chemclaw_current_actor", default=None)
_current_roles: ContextVar[frozenset[str]] = ContextVar(
    "chemclaw_current_roles", default=frozenset()
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
