"""The ambient authenticated identity for the current turn.

The user's Entra `oid`, app roles and the turn's correlation id are ambient to a turn, not tool
arguments: the front-door runner stamps them from the validated `Principal`, and audit, the
authorization gate, job attribution, logging and `kg/record.record_note` read them here. A
`contextvar` keeps concurrent turns from crossing identities and defaults to "no identity" off the
request path. The correlation id is per-turn state and must never be bound on a cached agent.

Values are plain `str`/`frozenset`, which is what lets this sit in the kernel below every reader.
A subagent is an attenuation of its caller's authority, never a new actor; any carrier for the
running agent belongs beside `_current_actor`, not over it.
"""

from contextvars import ContextVar

# Prefix for a group-derived entitlement, so it can never collide with an app role.
#
# App roles are defined by the API's app registration, group claims by the directory; merged without
# a prefix, a group named like an app role would widen every write-tool and skill gate. It lives in
# the role vocabulary because manifests, refusal messages and docs that teach a group gate must name
# the same string; `tests/test_document_share.py` keeps them agreeing.
GROUP_ROLE_PREFIX = "group:"

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
    """The Entra oid of the turn in flight, or None when there is no authenticated user.

    A blank or whitespace-only value is treated as no actor, so it cannot pass the reject-if-absent
    gates or mint a per-actor memory namespace no erasure request can name. The value is returned
    stripped so two spellings of one person share one namespace. Normalised here, in the one reader
    every gate shares, rather than in each producer.
    """
    return (_current_actor.get() or "").strip() or None


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

    None means no turn stamped one, and the caller falls back to the id it was built with (Temporal
    template activities and the CLI bind the workflow id). Blank is treated as absent, as for the
    actor, since a whitespace join key matches nothing.
    """
    return (_current_correlation_id.get() or "").strip() or None
