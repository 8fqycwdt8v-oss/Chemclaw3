"""The fixture connector's own Temporal workflow — what a real connector-owned workflow looks like.

Minimal but complete: it takes the plain payload core forwards and returns a `ConnectorJobResult`
with a summary, structured data and a knowledge note — the three parts the wrapper acts on. It has
no wrapper import, session id, idempotency or audit; core owns those (`durable/connector_job.py`).
It reads the requesting actor from the run's memo, not the payload.
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
        """Return a summary, structured data, and an agent-authored knowledge note.

        `requested_by` comes from the run's memo, not `payload`: the payload is model-authored, so
        the actor cannot live there. A bundle with a shared service identity reads it here to keep
        the run attributable (`connectors/calc/workflows.py`).
        """
        subject = str(payload["subject"])
        # A reserved subject that fails, so the wrapper's failure path runs against a real raise
        # inside the connector's workflow.
        if subject == "boom":
            raise ApplicationError("the fixture job was asked to fail", non_retryable=True)
        return ConnectorJobResult(
            summary=f"fixture job ran on {subject}",
            data={
                "subject": subject,
                "ran": True,
                "requested_by": workflow.memo_value("requested_by", ""),
                # The session, off the same memo: `AwaitAnswerWorkflow._push` drops a notice without
                # one.
                "session_id": workflow.memo_value("session_id", ""),
            },
            note=Note(
                id=f"fixture-{subject}",
                type="job-result",
                created_by="agent",
                source="connector:fixture",
                body=f"The fixture job ran on {subject}.\n",
            ),
        )
