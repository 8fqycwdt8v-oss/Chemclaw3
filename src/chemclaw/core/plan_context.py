"""The ambient plan-step link for the current tool call.

A durable job records which plan step launched it, but the step must not be a model argument (it
joins to the audit trail and must not be spoofable), and launchers must never write the todo list (a
marker in a todo's `content` revokes its approval). So `agent/plan_link.py`'s middleware stamps the
step into a `contextvar` per tool call and job-launching code reads it here, as
`core.session_context` does for the session id. Imports only `contextvars`.

One `(plan_step, plan_hash)` pair rather than two vars, since a step only means something within its
plan revision. `("", "")`, the default off the graph path, means "not called from a plan step",
never an error.
"""

from contextvars import ContextVar

_current_plan_link: ContextVar[tuple[str, str]] = ContextVar(
    "chemclaw_current_plan_link", default=("", "")
)


def set_current_plan_link(plan_step: str, plan_hash: str) -> object:
    """Bind the tool call's plan link; returns a token for `reset_current_plan_link`."""
    return _current_plan_link.set((plan_step, plan_hash))


def get_current_plan_link() -> tuple[str, str]:
    """The `(plan_step, plan_hash)` of the call in flight; `("", "")` off the harness path."""
    return _current_plan_link.get()


def reset_current_plan_link(token: object) -> None:
    """Restore the previous link, undoing a `set_current_plan_link` (call teardown)."""
    _current_plan_link.reset(token)  # type: ignore[arg-type]
