"""Durable-capability registry — workflows and activities declare their own queue.

A workflow or activity declares its queue at its definition site and the worker assembles what it
serves from this registry, so a written-and-imported workflow cannot be missing from a worker. An
activity also declares its `ActivityClass`, which `durable/retry.py` turns into its retry policy.

`background` is core's queue; each connector bundle adds `connector-<name>`
(`chemclaw.connectors.queues.bundle_queue`). Isolation comes from the import boundary: the
registry is populated at import time, so core's worker never registers a bundle module it does not
import (`tests/test_workflow_registry.py` asserts this).

Mirrors `chemclaw.core.tool_registry`: a dict per queue keyed by the Temporal name, with a
duplicate guard. Re-registering the same definition (the workflow sandbox re-imports modules) is
allowed and ignored: the first object is kept.
"""

import logging
from collections import defaultdict
from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

# A task queue name: an open set, so a bundle can name its queue without editing core.
Queue = str

DurableActivity = Callable[..., Any]


class ActivityClass(StrEnum):
    """What an activity does to the outside world, which decides how it may be retried.

    `durable/retry.py` holds the policy of each class and the reason for it.
    """

    # Short Postgres or in-process work, safe to repeat: reads, small idempotent writes.
    IO = "io"
    # One knowledge-note write; a retry meets a contended remote, never a bad note.
    NOTE_WRITE = "note_write"
    # A long sweep or computation in the worker; a retry repeats the work.
    COMPUTE = "compute"
    # A call to a system or service outside the worker and Postgres, repeatable: a data source, a
    # mounted share, the model gateway, a labelling or embedding server, an outbound channel.
    REMOTE = "remote"
    # A call to the calculation backend, which may be full and which must not be re-run needlessly.
    CALC = "calc"
    # A governed tool call or agent turn whose re-run repeats audited side effects.
    GOVERNED = "governed"
    # A queued heavy tool call, retried against a full server until its own deadline.
    QUEUED_TOOL = "queued_tool"


_CLASS_ATTRIBUTE = "__chemclaw_activity_class__"
_WorkflowT = TypeVar("_WorkflowT", bound=type)
_ActivityT = TypeVar("_ActivityT", bound=DurableActivity)

_WORKFLOWS: dict[Queue, dict[str, type]] = defaultdict(dict)
_ACTIVITIES: dict[Queue, dict[str, DurableActivity]] = defaultdict(dict)


def temporal_name(obj: Any) -> str:
    """The name Temporal will advertise this workflow or activity under.

    Honours a `name=` override on the decorator, since collisions are on the advertised name. Falls
    back to `__name__` for an undecorated object.
    """
    definition = getattr(obj, "__temporal_workflow_definition", None) or getattr(
        obj, "__temporal_activity_definition", None
    )
    return str(getattr(definition, "name", None) or obj.__name__)


def _claim(existing: Any, incoming: Any, kind: str, name: str) -> None:
    """Reject a genuine name collision; allow the same definition to re-register.

    Compares the defining module rather than object identity, because the workflow sandbox
    re-imports modules and builds fresh objects. Two modules claiming one name is an error: the
    worker would silently drop one.
    """
    if existing.__module__ != incoming.__module__:
        raise ValueError(
            f"durable {kind} name {name!r} is claimed by both "
            f"{existing.__module__} and {incoming.__module__}"
        )


def durable_workflow(queue: Queue) -> Callable[[_WorkflowT], _WorkflowT]:
    """Register a `@workflow.defn` class on `queue`. Apply above `workflow.defn`.

    Returns the class unchanged, so the registered object is exactly what Temporal was given.
    """

    def register(cls: _WorkflowT) -> _WorkflowT:
        name = temporal_name(cls)
        registered = _WORKFLOWS[queue]
        existing = registered.get(name)
        if existing is not None:
            _claim(existing, cls, "workflow", name)
            # Keep the first: the sandbox's re-import builds a new class object, and storing it
            # would replace the object the worker modules captured at import time.
            return cls
        registered[name] = cls
        return cls

    return register


def activity_in_class(activity_class: ActivityClass) -> Callable[[_ActivityT], _ActivityT]:
    """Declare the `ActivityClass` of an activity whose queue is not fixed at its definition.

    For an activity that is served by workers built per connector, so no `durable_activity` queue
    names it. The class is stored on the function as its plain value, so it survives the workflow
    sandbox re-importing this module (two copies of the enum never compare equal).
    """

    def declare(fn: _ActivityT) -> _ActivityT:
        setattr(fn, _CLASS_ATTRIBUTE, activity_class.value)
        return fn

    return declare


def durable_activity(
    queue: Queue, activity_class: ActivityClass
) -> Callable[[_ActivityT], _ActivityT]:
    """Register an `@activity.defn` function on `queue`, in `activity_class`. Apply above it."""

    def register(fn: _ActivityT) -> _ActivityT:
        activity_in_class(activity_class)(fn)
        name = temporal_name(fn)
        registered = _ACTIVITIES[queue]
        existing = registered.get(name)
        if existing is not None:
            _claim(existing, fn, "activity", name)
            return fn  # keep the first, for the reason spelled out in `durable_workflow`
        registered[name] = fn
        return fn

    return register


def activity_class_of(activity: DurableActivity) -> ActivityClass:
    """The class an activity declared with `durable_activity`.

    Raises:
        LookupError: The function was never registered, so it has no class to be retried under.
    """
    declared = getattr(activity, _CLASS_ATTRIBUTE, None)
    if declared is None:
        raise LookupError(
            f"{getattr(activity, '__name__', activity)!r} declares no ActivityClass: decorate it "
            "with `@durable_activity(queue, ActivityClass.<class>)` or `@activity_in_class(...)`"
        )
    return ActivityClass(declared)


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
