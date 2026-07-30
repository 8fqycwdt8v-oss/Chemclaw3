"""The Temporal queue a connector bundle's own worker polls — one function, one spelling.

A bundle's queue name is needed in four places that must all agree: the `@durable_workflow` and
`@durable_activity` decorators in the bundle's own modules, the worker that assembles them, the
`task_queue:` its `connector.yaml` declares for each job, and the Helm component
`connector-worker-<name>`. Two that disagree is a job sitting forever in a queue nobody polls —
the same class of failure `durable/registry.py` exists to catch one level up. Deriving the name
from the bundle name means the code side of that agreement cannot drift (D-118).

The manifest side still spells the name out, and nothing yet checks the two against each other;
see the entry in `docs/planning/BACKLOG.md`.
"""


def bundle_queue(connector: str) -> str:
    """The Temporal queue a bundle's own worker polls."""
    return f"connector-{connector}"
