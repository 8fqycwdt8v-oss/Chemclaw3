"""The fixture connector's own Temporal workflow — what a real connector-owned workflow looks like.

Deliberately minimal, and deliberately *complete*: it takes the plain payload core forwards, and
it returns a `ConnectorJobResult` with all three parts filled in (a summary, structured data,
and a knowledge note), because those are exactly the three the wrapper acts on. A connector's
real workflow would do chemistry between those two lines; nothing else about its contract with
core differs.

Note what is *absent*, since that is the point of the seam: no import of the wrapper, no
knowledge of the PR-gate, no session id, no idempotency logic, no audit. Core owns all of it
(`durable/connector_job.py`); a connector author writes a workflow that takes a dict and
returns an envelope. The one thing it *reads* back from core is the run's memo, which is where
the requesting actor travels — deliberately beside the payload rather than inside it.
"""

from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from chemclaw.durable.connector_job import ConnectorJobResult
    from chemclaw.kg.note import Note


@workflow.defn(name="FixtureJobWorkflow")
class FixtureJobWorkflow:
    """Echo the job's subject back through the result envelope, with a note to PR-gate.

    The subject `boom` raises instead, which is what gives the wrapper's failure push-back a real
    child failure to carry.
    """

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> ConnectorJobResult:
        """Return a summary, structured data, and an agent-authored note for the PR-gate.

        `requested_by` comes off the run's **memo**, not out of `payload`: the payload is exactly
        the model-authored arguments, so the actor cannot live there without becoming something an
        LLM could fill in. Core stamps the memo on the child call, and a bundle whose backend runs
        under a shared service identity — a calculation backend — reads it here to keep the run
        attributable (`connectors/calc/workflows.py` is the real case).
        """
        subject = str(payload["subject"])
        # One reserved subject that fails, so the wrapper's *failure* path has something real to
        # run against. Raising inside the connector's own workflow is exactly how a live failure
        # arrives — `compare_solvents` died on an unknown ALPB solvent name — and the wrapper's
        # obligation is to tell the launching session before the failure propagates.
        if subject == "boom":
            raise ApplicationError("the fixture job was asked to fail", non_retryable=True)
        return ConnectorJobResult(
            summary=f"fixture job ran on {subject}",
            data={
                "subject": subject,
                "ran": True,
                "requested_by": workflow.memo_value("requested_by", ""),
            },
            note=Note(
                id=f"fixture-{subject}",
                type="job-result",
                created_by="agent",
                source="connector:fixture",
                body=f"The fixture job ran on {subject}.\n",
            ),
        )
