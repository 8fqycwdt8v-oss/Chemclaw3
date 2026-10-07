"""A connector-owned workflow that returns whatever it is told to, undecorated by any model.

Drives the two payload cases a well-formed `ConnectorJobResult` cannot: a result that is not the
envelope at all, and an envelope carrying a field this core does not know yet. Its own module,
importing nothing, because Temporal validates a workflow in its import sandbox.
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
