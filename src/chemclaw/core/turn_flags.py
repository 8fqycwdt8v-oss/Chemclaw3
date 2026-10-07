"""Ambient boolean flags for the turn in flight, alongside the other turn-local ambients.

The dry-run flag: ask "what would this cost, what would you do" without doing it, a safety valve in
front of the durable job launchers. A `ContextVar` like `core.session_context` and
`core.identity_context`: per turn, never a model-supplied argument (the model must not switch a real
run to a dry one or back), and off for every non-request caller. In `core` with those ambients so
readers such as `agent/tool_authz`, connector identity stamping and the labeller can read it without
importing a tool module or the agent layer.
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
