"""The Temporal queue a connector bundle's own worker polls — one function, one spelling.

A bundle's queue name is needed wherever its durable work is named: the `@durable_workflow` and
`@durable_activity` decorators in the bundle's own modules, the worker that assembles them, the
dispatch that starts one of its jobs, and the Helm component `connector-worker-<name>`. Two that
disagree is a job sitting forever in a queue nobody polls — the same class of failure
`durable/registry.py` exists to catch one level up.

Every one of them calls this function, so there is nothing left to disagree. `connector.yaml` used
to declare the queue per job as well; D-150 removed that field, because a bundle's worker serves
only what the bundle's own modules registered at import time, so a declared queue could hold
exactly one correct value and any number of unrunnable ones (D-118).
"""


def bundle_queue(connector: str) -> str:
    """The Temporal queue a bundle's own worker polls."""
    return f"connector-{connector}"
