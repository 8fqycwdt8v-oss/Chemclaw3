"""The ambient authenticated identity for the current turn (plan Phase F4-T5).

Like the session id (`chemclaw.core.session_context`), the authenticated user's Entra `oid` and
app roles are ambient to a turn, not tool arguments: the front-door runner stamps them from the
request's validated `Principal`, and audit, the authorization gate, and job-attribution read them
here. A `contextvar` is the right carrier — task-local, so concurrent turns never cross identities
— and it defaults to "no identity" off the request path (tests, the classic non-service caller),
where the static audit actor and the dev-mode allowances apply.

The turn's **correlation id** rides here for the same reason and with the same consumer. It used
to be bound once inside `build_langgraph_agent`, and agents are cached per profile for the process's
whole life — so every turn from every user on a pod shared one id, which is precisely the opposite
of what a correlation id is for: the audit trail could not separate two chemists' tool calls, and
"show me everything that happened in this conversation" returned the pod's entire history. It is
per-turn state, so it belongs in a task-local like the actor, not on a cached object.

The **running specialist** used to ride here as a fourth carrier, and does not any more
(`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`): its setter had no caller in
`src/` for as long as it existed, so the audit trail's `agent` column could only ever be empty while
the module said the trail named the agent beside the human. The rule it carried is not lost — a
subagent is an attenuation of its caller's authority and never a new actor
(`docs/decisions/D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md`), which binds whoever
re-adds one — and re-adding the carrier beside `_current_actor` rather than over it is what that
rule asks for. An invariant is not a function.

**Plain `str`/`frozenset` values and nothing but `contextvars`**, which is what makes this kernel
material: seven packages read the turn's actor — audit, the authz gate, the PR-gate, connector
identity headers, template activities, the CLI, and `core.logging`'s own `ContextFilter`. It sat
in `chemclaw.agent` until the R2 layering move, where it was the single import that put `kg` and
`connectors` above the conversation layer and forced `core/logging.py` to reach for it lazily.
"""

from contextvars import ContextVar

# What a group-derived entitlement is named, so it can never collide with an app role.
#
# Entra app roles are values the API's own app registration defines; group claims are values the
# *directory* defines, and a tenant may emit them as object-ids or as names
# (`groupMembershipClaims` accepts `sam_account_name`, `cloud_displayname`, …). Merged into one flat
# set, a directory group called `process-chemist` is indistinguishable from the app role of that
# name — so enabling `entra_group_claims_as_roles` to give one file share its read entitlement would
# also widen every write-tool and skill gate. The prefix keeps the two namespaces apart.
#
# **It lives here because it is part of the role vocabulary, not of the HTTP layer.** `api.auth`
# stamps it onto the roles this module carries, and the places that *tell an operator how to write
# a group-gated entitlement* have to name the same string — the shipped `sharedrive` manifest, the
# binding's own refusal message, `docs/guides/sharedrive-concept.md`. While it sat in `api.auth`
# those were four hand-typed copies of one security-relevant string, and three of them were wrong:
# they told an operator to write the bare object-id, which matches nothing, so a correctly
# configured tenant got an empty corpus and no error anywhere.
# `tests/test_document_share.py::test_every_place_that_teaches_a_group_gate_names_the_real_prefix`
# is what keeps them agreeing now.
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

    An empty string is treated as no actor (`... or None`): `agent/authz.require_actor` and
    `authorize_trigger` gate on `is None` / `is not None`, and nothing constrains the contextvar to
    be non-empty, so a context bound with `set_current_identity("", ...)` would otherwise return
    `""` and pass the reject-if-absent rule as an authenticated user. Fail closed here, in the one
    reader every gate shares, rather than at each of the five producers separately.
    """
    return _current_actor.get() or None


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
