"""The ambient plan-step link for the current tool call (D-2026-08-27).

When a tool launches a durable job from inside a harness turn, the job should record *which plan
step* it was launched for — but the step is not something the model should pass as an argument (it
joins to the audit trail, and the model must not be able to spoof it), and the todo list itself
must never be written by a launcher (a marker in a todo's `content` revokes the approval keyed on
it — the rule `agent/state.py` records, applied twice). So `agent/plan_link.py`'s middleware
stamps the current step into a `contextvar` for the duration of each tool call, and job-launching
code reads it here — exactly the carrier and the polarity `core.session_context` established for
the session id.

Kernel material like its sibling: two bare strings, nothing imported but `contextvars`, readable
from `connectors` and `core.turn_signals` without touching the agent layer.

The value is a `(plan_step, plan_hash)` pair rather than two vars, because the two are one fact —
a step is only meaningful inside the plan revision it belongs to, and two vars could be reset out
of step with each other. `("", "")` — the default off the graph path (a template step, the CLI, a
test) — reads as "this call was not made from a plan step", never as an error.
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
