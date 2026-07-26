"""The fixture connector's own Temporal workflow — what a real connector-owned workflow looks like.

Deliberately minimal, and deliberately *complete*: it takes the plain payload core forwards, and
it returns a `ConnectorJobResult` with all three parts filled in (a summary, structured data,
and a knowledge note), because those are exactly the three the wrapper acts on. A connector's
real workflow would do chemistry between those two lines; nothing else about its contract with
core differs.

Note what is *absent*, since that is the point of the seam: no import of the wrapper, no
knowledge of the PR-gate, no session id, no idempotency logic, no audit. Core owns all of it
(`workflows/connector_job.py`); a connector author writes a workflow that takes a dict and
returns an envelope.
"""

from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from kg.note import Note
    from workflows.connector_job import ConnectorJobResult


@workflow.defn(name="FixtureJobWorkflow")
class FixtureJobWorkflow:
    """Echo the job's subject back through the result envelope, with a note to PR-gate."""

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> ConnectorJobResult:
        """Return a summary, structured data, and an agent-authored note for the PR-gate."""
        subject = str(payload["subject"])
        return ConnectorJobResult(
            summary=f"fixture job ran on {subject}",
            data={"subject": subject, "ran": True},
            note=Note(
                id=f"fixture-{subject}",
                type="job-result",
                created_by="agent",
                source="connector:fixture",
                body=f"The fixture job ran on {subject}.\n",
            ),
        )
