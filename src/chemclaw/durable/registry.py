"""Durable-capability registry — workflows and activities declare their own queue.

Why this exists, in the same words the tool registry uses: adding a durable capability
used to mean editing a hardcoded list inside a worker module — the *one* extension seam
left that forced an edit to infrastructure code. Every other capability in this system
declares itself at its definition site and is discovered (agent tools by `@tool`, metrics
by `@metric`, skills by folder, MCP servers and data sources by config token). A workflow
now does the same: it says which queue it belongs on, next to its definition, and the
worker assembles what it serves from the registry.

The failure this prevents is specific and was hit while building the xTB job: a workflow
that is written, tested and imported but missing from the worker's list is a workflow
that never runs, and nothing fails until someone submits one and it sits in the queue
forever.

**Queues are a capability property, not a deployment detail.** `background` is core's own —
many light workers (sync, re-index, reports, the connector-job wrapper). Each connector bundle
adds one of its own, `connector-<name>` (`chemclaw.connectors.queues.bundle_queue`), sized for that
capability alone. Which one a workflow belongs on follows from what it does, so it belongs with
the code that does it — see D-006. The set is open rather than the original two because D-006's
heavy/light split moved down one level: core's `hpc` queue existed for the single QM/DFT
workflow, and that is a bundle now (D-118).

**The isolation comes from the import boundary, not from the decorator.** A bundle's heavy
dependencies stay out of core's worker because core's worker never imports the bundle's
module — the registry is populated at *import* time, so an unimported module registers
nothing. Bundles used to leave their workflows undecorated to achieve this, which could not
work for that reason and re-created on the connector side the "written, imported, absent
from a hand-maintained list, never runs" failure this registry exists to prevent (D-118).
`tests/test_workflow_registry.py` now asserts the import boundary directly.

The shape deliberately mirrors `chemclaw.core.tool_registry`: a dict per queue keyed by the name
Temporal will advertise, insertion-ordered, with a duplicate guard. The one difference is
that re-registering the *same* definition is allowed, because Temporal's workflow sandbox
re-imports workflow modules and would otherwise trip the guard on every workflow task.
Allowed, and **ignored**: the first registration is the one kept, because the sandbox's
re-import builds a new class object for the same definition and overwriting would swap out
the object the worker modules captured at import time.
"""

import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

# A task queue name. Core owns one (D-006) and every connector bundle owns one
# (`connectors.queues.bundle_queue`), so this is an open set of strings rather than a closed
# Literal — a bundle must be able to name its queue without editing core.
Queue = str

DurableActivity = Callable[..., Any]
_WorkflowT = TypeVar("_WorkflowT", bound=type)
_ActivityT = TypeVar("_ActivityT", bound=DurableActivity)

_WORKFLOWS: dict[Queue, dict[str, type]] = defaultdict(dict)
_ACTIVITIES: dict[Queue, dict[str, DurableActivity]] = defaultdict(dict)


def temporal_name(obj: Any) -> str:
    """The name Temporal will advertise this workflow or activity under.

    Usually the Python name, but `@workflow.defn(name=...)` and `@activity.defn(name=...)`
    can override it — and the registry's whole job is to catch two capabilities claiming
    one name, so it has to key on the name Temporal actually uses rather than the one the
    source happens to spell. Falls back to `__name__` for an undecorated object.
    """
    definition = getattr(obj, "__temporal_workflow_definition", None) or getattr(
        obj, "__temporal_activity_definition", None
    )
    return str(getattr(definition, "name", None) or obj.__name__)


def _claim(existing: Any, incoming: Any, kind: str, name: str) -> None:
    """Reject a genuine name collision; allow the same definition to re-register.

    Temporal's workflow sandbox re-imports workflow modules to run them, which executes
    the decorators again against freshly created objects. That is not a collision — it is
    the same definition arriving twice — so the guard compares the defining *module*
    rather than object identity. Two different modules claiming one Temporal name is a
    real error: the worker would advertise one of them and silently drop the other.
    """
    if existing.__module__ != incoming.__module__:
        raise ValueError(
            f"durable {kind} name {name!r} is claimed by both "
            f"{existing.__module__} and {incoming.__module__}"
        )


def durable_workflow(queue: Queue) -> Callable[[_WorkflowT], _WorkflowT]:
    """Register a `@workflow.defn` class on `queue`. Apply above `workflow.defn`.

    Returns the class unchanged, so the registered object is exactly what Temporal was
    given — the same identity rule `chemclaw.core.tool_registry.tool` follows.
    """

    def register(cls: _WorkflowT) -> _WorkflowT:
        name = temporal_name(cls)
        registered = _WORKFLOWS[queue]
        existing = registered.get(name)
        if existing is not None:
            _claim(existing, cls, "workflow", name)
            # Keep the first. The re-registration is Temporal's sandbox re-importing the
            # module, which builds a *new* class object for the same definition; storing it
            # would swap out the very object the worker modules captured at import time, so
            # `registered_workflows(queue)` would stop equalling the worker's own list — the
            # equality `test_workflow_registry` asserts, and which only breaks once a Worker
            # has actually been constructed (so it passes wherever Temporal is unavailable).
            # The class is still returned unchanged, so Temporal gets the object it built.
            return cls
        registered[name] = cls
        return cls

    return register


def durable_activity(queue: Queue) -> Callable[[_ActivityT], _ActivityT]:
    """Register an `@activity.defn` function on `queue`. Apply above `activity.defn`."""

    def register(fn: _ActivityT) -> _ActivityT:
        name = temporal_name(fn)
        registered = _ACTIVITIES[queue]
        existing = registered.get(name)
        if existing is not None:
            _claim(existing, fn, "activity", name)
            return fn  # keep the first, for the reason spelled out in `durable_workflow`
        registered[name] = fn
        return fn

    return register


def registered_workflows(queue: Queue) -> list[type]:
    """Every workflow declared for `queue`, in declaration order."""
    return list(_WORKFLOWS[queue].values())


def registered_activities(queue: Queue) -> list[DurableActivity]:
    """Every activity declared for `queue`, in declaration order."""
    return list(_ACTIVITIES[queue].values())


def describe(queue: Queue) -> str:
    """One line naming what a worker on `queue` serves, for its startup log."""
    workflows = ", ".join(sorted(_WORKFLOWS[queue])) or "none"
    activities = ", ".join(sorted(_ACTIVITIES[queue])) or "none"
    return f"workflows=[{workflows}] activities=[{activities}]"
