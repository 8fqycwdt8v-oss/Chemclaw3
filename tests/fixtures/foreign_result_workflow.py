"""A connector-owned workflow that returns whatever it is told to, undecorated by any model.

The wrapper's contract with a bundle is a *payload* — whatever the child's image chose to return —
and every other fixture here returns a well-formed `ConnectorJobResult` built by this repository's
own class. That makes the two cases nobody could reach untestable: a foreign workflow id whose
result is not the envelope at all, and a bundle image newer than core's returning the envelope plus
one field this core has not learned yet. Both are the same wire and only a child that bypasses the
model can produce them.

In its own module, and importing nothing, because a Temporal workflow definition is validated in
the SDK's import sandbox: defined inside a test module it drags that module's whole import graph
through `prepare_workflow` and fails there.
"""

from typing import Any

from temporalio import workflow


@workflow.defn(name="ForeignResultWorkflow")
class ForeignResultWorkflow:
    """Return `payload["returns"]` verbatim, so the caller decides what crosses the wire."""

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Echo the requested result back with no validation of any kind."""
        return dict(payload["returns"])
