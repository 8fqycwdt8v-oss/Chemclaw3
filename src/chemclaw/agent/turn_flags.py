"""Ambient boolean flags for the turn in flight, alongside the other turn-local ambients.

**IDEA-4.** Every expensive path is idempotent and cached, but there was no way to ask "what
would this cost, what would you do" without doing it. For a system whose production-default
autonomy is `plan_only`, an explicit dry run is the natural product primitive — and a cheap
safety valve in front of the durable job launchers.

A `ContextVar` for the same reason the ambient session and identity are (see
`chemclaw.agent.session_context`, `chemclaw.agent.identity_context`): it must be per-turn, it must
not be a model-supplied tool argument (the model must not be able to turn a real run into a dry
one or vice versa), and it must default to "off" for every non-request caller.

**Why this is its own module, not part of a tool module.** The flag used to live in
`chemclaw.agent.dialogue_tools`, which exists to register `ask_clarifying_question` into the
model-facing tool registry. Two unrelated readers — `chemclaw.agent.tool_authz` (the turn's own
authorization middleware) and `chemclaw.connectors.identity` (a connector reading the flag to
stamp a header) — therefore had to import a *tool* module just to read a turn flag, which
imported `dialogue_tools` for its side effect of registering that tool, a connector having no
business anywhere near the model's tool surface. Moving the flag here — a plain ambient with no
tool of its own — lets both read it without that side effect.
"""

from contextvars import ContextVar

# Whether the turn in flight is a dry run.
_dry_run: ContextVar[bool] = ContextVar("chemclaw_dry_run", default=False)


def set_dry_run(enabled: bool) -> object:
    """Mark the current turn as a dry run; returns a token for `reset_dry_run`."""
    return _dry_run.set(enabled)


def reset_dry_run(token: object) -> None:
    """Clear the dry-run flag at turn teardown."""
    _dry_run.reset(token)  # type: ignore[arg-type]


def is_dry_run() -> bool:
    """Whether the turn in flight is a dry run (False off the request path)."""
    return _dry_run.get()
