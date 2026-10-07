"""The Temporal queue a connector bundle's own worker polls: one function, one spelling.

Every place that names a bundle's durable work (its decorators, its worker, the dispatch, the Helm
component `connector-worker-<name>`) calls this, so none can disagree and leave a job in a queue
nobody polls. Manifests do not declare queues (D-150): a bundle's worker serves only what its own
modules registered.
"""


def bundle_queue(connector: str) -> str:
    """The Temporal queue a bundle's own worker polls."""
    return f"connector-{connector}"


def interactive_queue(connector: str) -> str:
    """The queue a bundle's queued tool calls wait on, apart from its durable jobs.

    Separate so a backlog of hour-long jobs never stands between a chemist and a seconds-long
    answer;
    polled by `chemclaw.connectors.interactive_worker`.
    """
    return f"connector-{connector}-interactive"
